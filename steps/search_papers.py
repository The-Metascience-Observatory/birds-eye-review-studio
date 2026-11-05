#!/usr/bin/env python3
"""Search for papers across multiple literature APIs.

Queries four APIs (PubMed, OpenAlex, Europe PMC, Semantic Scholar),
deduplicates results, and outputs a clean CSV of DOIs and PMIDs.

Usage (every run names its review config):
    python steps/search_papers.py --config configs/config_x.yaml                  # all sources
    python steps/search_papers.py --config ... --sources pubmed,openalex          # subset
    python steps/search_papers.py --config ... --max-per-query 500                # limit (testing)
"""

import argparse
import concurrent.futures
import csv
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import requests
from Bio import Entrez, Medline

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.environment import load_local_env as load_dotenv

from ber_core.identity import canonical_doi
from ber_core.utils import load_config, slugify, doi_to_filename, redact_secrets, write_csv_atomic
from ber_core.storage import atomic_text

# --- Concurrency primitives (used when --parallel is on) -------------------
STATE_LOCK = threading.Lock()   # guards dedup sets, RAW_CSV append, PROGRESS_FILE
PRINT_LOCK = threading.Lock()   # serializes stdout so lines aren't interleaved
_thread_local = threading.local()


def _set_source(name: str | None) -> None:
    """Tag the current thread with a source name for log prefixing."""
    _thread_local.source = name


def _log(msg: str) -> None:
    """Print with a [source] prefix when running inside a source thread.

    Every message is redacted: ``requests`` exceptions carry the full URL, and
    OpenAlex / NCBI keys travel as query parameters.
    """
    msg = redact_secrets(msg)
    src = getattr(_thread_local, "source", None)
    with PRINT_LOCK:
        if src:
            print(f"[{src}] {msg}", flush=True)
        else:
            print(msg, flush=True)

# =============================================================================
# Section 1: Configuration
# =============================================================================

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(BASE_DIR / ".env.local")

PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

# Per-project paths -- populated from config in main()
PROJECT_DIR = None
DATA_DIR = None
PROGRESS_DIR = None
ABSTRACTS_DIR = None
PROGRESS_FILE = None
RAW_CSV = None
FINAL_CSV = None
ABSTRACTS_LOG_CSV = None


def configure_project_paths(topic_name: str) -> None:
    """Set the global per-project paths based on topic name from config."""
    global PROJECT_DIR, DATA_DIR, PROGRESS_DIR, ABSTRACTS_DIR
    global PROGRESS_FILE, RAW_CSV, FINAL_CSV, ABSTRACTS_LOG_CSV
    slug = slugify(topic_name)
    PROJECT_DIR = PROJECTS_DIR / slug
    DATA_DIR = PROJECT_DIR / "data"
    PROGRESS_DIR = PROJECT_DIR / "progress"
    ABSTRACTS_DIR = PROJECT_DIR / "abstracts"
    PROGRESS_FILE = PROGRESS_DIR / "search_progress.json"
    RAW_CSV = DATA_DIR / "search_raw.csv"
    FINAL_CSV = DATA_DIR / "search_dois.csv"
    ABSTRACTS_LOG_CSV = DATA_DIR / "abstracts_log.csv"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    ABSTRACTS_DIR.mkdir(parents=True, exist_ok=True)

# NCBI asks every E-utilities client for a contact address (and OpenAlex's
# polite pool uses one). No default: main() refuses PubMed without it.
ENTREZ_EMAIL = os.environ.get("EMAIL", "").strip()
NCBI_API_KEY = os.environ.get("ENTREZ_EUTILS_API_KEY", "")
OPENALEX_API_KEY = os.environ.get("OPENALEXAPIKEY", "")
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

Entrez.email = ENTREZ_EMAIL
if NCBI_API_KEY:
    Entrez.api_key = NCBI_API_KEY
    NCBI_DELAY = 0.11  # 10 req/sec with API key
else:
    NCBI_DELAY = 0.34  # 3 req/sec without

OPENALEX_DELAY = 0.1   # 10 req/s — OpenAlex polite-pool ceiling (0.05 = 20/s -> 429s)
EUROPEPMC_DELAY = 0.2
S2_DELAY = 1.05
CYBERLENINKA_DELAY = 0.5
LESSWRONG_DELAY = 0.3

MAX_PER_QUERY = 500_000  # effectively unlimited; overridden by CLI --max-per-query

# Incremental-update controls (set from CLI in main(), read-only in worker
# threads thereafter).
#   SINCE_DATE: ISO "YYYY-MM-DD". When set, each source restricts to records on
#     or after this date. PubMed (edat) and Europe PMC (CREATION_DATE) window on
#     *index/entry* date, so late-indexed older papers are still caught. OpenAlex
#     (free tier; from_created_date is paywalled) and Semantic Scholar window on
#     *publication* date; the periodic --fresh full rescan backstops those.
#   IGNORE_PROGRESS: when True (--fresh or --since), re-issue every query rather
#     than skipping ones already recorded in search_progress.json.
SINCE_DATE = None
IGNORE_PROGRESS = False

FIELDNAMES = ["doi", "pmid", "title", "source_api", "source_query"]

DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)

# Query lists -- populated from config in main()
PUBMED_QUERIES = []
OPENALEX_TITLE_SEARCHES = []
OPENALEX_ABSTRACT_SEARCHES = []
EUROPEPMC_QUERIES = []
SEMANTIC_SCHOLAR_QUERIES = []
CYBERLENINKA_QUERIES = []
# LessWrong/AlignmentForum config block: {forums, tags, keywords, min_karma}
LESSWRONG_CFG = {}


def normalize_doi(doi: str) -> str:
    """A DOI in the stored ``--`` format (lowercase), or "" if it is not a DOI.

    Through :func:`ber_core.identity.canonical_doi`: strips URL/``doi:``
    prefixes, URL-decodes, drops trailing punctuation, and VALIDATES — a
    malformed value an API returns (``10.1234``, free text) is dropped rather
    than stored as a paper id.
    """
    canonical = canonical_doi(doi)
    return doi_to_filename(canonical) if canonical else ""


# =============================================================================
# Section 2: Progress / resume helpers
# =============================================================================

from ber_core.search import QueryLedger
SEARCH_RUN = QueryLedger()


def load_progress():
    """The query checkpoint. A torn or unreadable file is a WARNING, not a crash:
    it is a cache of completed queries, so losing it only re-runs them."""
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("completed_queries", [])
                return data
            raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            print(f"WARN: {PROGRESS_FILE.name} is unreadable ({e}); starting "
                  f"without the checkpoint (completed queries re-run, deduped)")
    return {"completed_queries": []}


def _write_progress(progress) -> None:
    """Atomic: a crash mid-write must not leave a torn checkpoint."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_text(PROGRESS_FILE, json.dumps(progress, indent=2))


def save_progress(progress):
    with STATE_LOCK:
        _write_progress(progress)


def _mark_completed(progress: dict, key: str) -> None:
    """Atomically record a completed query and persist progress.

    No-op when IGNORE_PROGRESS is set: a --fresh/--since re-run must not rewrite
    the full-crawl baseline (and date-windowed runs would otherwise pollute it
    with keys that don't represent a complete crawl of that query)."""
    if not SEARCH_RUN.complete(key) or IGNORE_PROGRESS:
        return
    with STATE_LOCK:
        if key in progress["completed_queries"]:
            return
        progress["completed_queries"].append(key)
        progress.setdefault("query_watermarks", {})[key] = time.strftime("%Y-%m-%d")
        _write_progress(progress)


def _should_skip(progress: dict, key: str) -> bool:
    """True when a query was already completed and we are not forcing a re-run."""
    skipped = (not IGNORE_PROGRESS) and key in progress["completed_queries"]
    SEARCH_RUN.begin(key, skipped)
    return skipped


def _pubmed_date(since: str) -> str:
    """ISO 'YYYY-MM-DD' -> PubMed 'YYYY/MM/DD'."""
    return since.replace("-", "/")


# =============================================================================
# Section 3: Deduplication engine
# =============================================================================

seen_dois = set()
seen_pmids = set()
seen_titles = set()

# DOIs newly appended during this run (guarded by STATE_LOCK). Written to
# data/new_dois.csv at the end of an incremental run so downstream steps can
# scope themselves to just this cycle's new papers.
NEW_DOIS_THIS_RUN: set[str] = set()


def _normalize_title(title):
    if not title:
        return ""
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def load_seen_from_csv():
    if not RAW_CSV.exists():
        return 0
    count = 0
    with open(RAW_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            _mark_seen(row)
            count += 1
    return count


def _mark_seen(row):
    doi = (row.get("doi") or "").strip().lower()
    pmid = (row.get("pmid") or "").strip()
    title = _normalize_title(row.get("title", ""))
    if doi:
        seen_dois.add(doi)
    if pmid:
        seen_pmids.add(pmid)
    if title and len(title) > 20:
        seen_titles.add(title)


def _is_duplicate(row):
    doi = (row.get("doi") or "").strip().lower()
    if doi and doi in seen_dois:
        return True
    pmid = (row.get("pmid") or "").strip()
    if pmid and pmid in seen_pmids:
        return True
    title = _normalize_title(row.get("title", ""))
    if title and len(title) > 20 and title in seen_titles:
        return True
    return False


def dedup_and_append(rows, source_api, source_query):
    with STATE_LOCK:
        new_rows = []
        for row in rows:
            row["source_api"] = source_api
            row["source_query"] = source_query
            dup = _is_duplicate(row)
            if not dup:
                new_rows.append(row)
                _mark_seen(row)
            # Track this update's working set. For --since we record EVERY DOI the
            # windowed query returned (the full window set) — stable across re-runs,
            # so a second --since run doesn't produce an empty new_dois.csv just
            # because the papers are already in the corpus. For --fresh (no window)
            # we record only genuinely-new DOIs.
            if IGNORE_PROGRESS:
                doi = (row.get("doi") or "").strip()
                if doi and (SINCE_DATE or not dup):
                    NEW_DOIS_THIS_RUN.add(doi)

        skipped = len(rows) - len(new_rows)
        if not new_rows:
            _log(f"    -> 0 new rows (all {len(rows)} were duplicates)")
            return 0

        write_header = not RAW_CSV.exists()
        with open(RAW_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if write_header:
                writer.writeheader()
            for row in new_rows:
                writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

        _log(f"    -> appended {len(new_rows)} new rows ({skipped} duplicates skipped)")
        return len(new_rows)


# =============================================================================
# Section 4: HTTP request helper with retry
# =============================================================================

def _request_with_retry(url, params=None, headers=None, max_retries=5,
                        base_delay=5, timeout=60):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.Timeout:
            wait = min(base_delay * (2 ** attempt), 60)
            _log(f"    Timeout on attempt {attempt + 1}/{max_retries}. "
                 f"Waiting {wait}s...")
            time.sleep(wait)
            continue
        except requests.exceptions.RequestException as e:
            _log(f"    Request error: {e}")
            return None

        if r.status_code == 200:
            return r
        if r.status_code == 429:
            wait = min(base_delay * (2 ** attempt), 60)
            _log(f"    Rate limited (429). Waiting {wait}s before retry "
                 f"{attempt + 1}/{max_retries}")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = min(base_delay * (2 ** attempt), 60)
            _log(f"    Server error ({r.status_code}). Waiting {wait}s "
                 f"before retry")
            time.sleep(wait)
            continue
        _log(f"    HTTP {r.status_code} error for {url}")
        return None

    _log(f"    Max retries exceeded for {url}")
    return None


# =============================================================================
# Section 5: PubMed search
# =============================================================================

def search_pubmed(query, max_results=None):
    """Post query to PubMed history server, return (web_env, query_key, count)."""
    limit = max_results or MAX_PER_QUERY
    esearch_kwargs = dict(db="pubmed", term=query, retmax=0, usehistory="y")
    if SINCE_DATE:
        # edat = Entrez date (when the record was added), so late-indexed older
        # papers are still returned. maxdate open-ended.
        esearch_kwargs.update(datetype="edat",
                              mindate=_pubmed_date(SINCE_DATE),
                              maxdate="3000/12/31")
    handle = Entrez.esearch(**esearch_kwargs)
    record = Entrez.read(handle)
    handle.close()
    total = int(record.get("Count", 0))
    fetch_count = min(total, limit)
    if total > limit:
        SEARCH_RUN.fail("result cap reached before all results were fetched")
    _log(f"  PubMed: {total} total results for: {query[:80]}...")
    return record["WebEnv"], record["QueryKey"], fetch_count


def _extract_doi_from_medline(rec):
    """Extract DOI from a Medline record, checking AID, LID, SO fields."""
    for aid in rec.get("AID", []):
        if aid.endswith("[doi]"):
            return aid.replace(" [doi]", "").strip()
    for field in ["LID", "SO"]:
        val = rec.get(field)
        if not val:
            continue
        entries = val if isinstance(val, list) else [val]
        for text in entries:
            m = DOI_RE.search(text)
            if m:
                return m.group(1)
    return ""


def fetch_pubmed_metadata(web_env, query_key, count, batch_size=500):
    """Fetch metadata from PubMed using history server (web_env/query_key).

    Returns ``(records, complete)``. ``complete`` is False when ANY batch
    failed, so the caller can decline to mark the query permanently done.
    A per-batch error is still swallowed (one bad page shouldn't discard the
    other 39), but it can no longer masquerade as a finished crawl: the query
    used to be written to search_progress.json regardless, and `_should_skip`
    then skipped it forever, silently losing every paper in the failed batches.
    """
    records = []
    failed_batches = 0
    for start in range(0, count, batch_size):
        this_batch = min(batch_size, count - start)
        try:
            handle = Entrez.efetch(
                db="pubmed", WebEnv=web_env, query_key=query_key,
                retstart=start, retmax=this_batch,
                rettype="medline", retmode="text"
            )
            for rec in Medline.parse(handle):
                doi = _extract_doi_from_medline(rec)
                records.append({
                    "pmid": rec.get("PMID", ""),
                    "doi": normalize_doi(doi),
                    "title": rec.get("TI", ""),
                })
            handle.close()
        except Exception as e:
            failed_batches += 1
            _log(f"    Error fetching batch at offset {start}: {e}")
        time.sleep(NCBI_DELAY)
        if start > 0 and (start // batch_size) % 10 == 0:
            _log(f"    fetched {start}/{count} PubMed records")
    if failed_batches:
        _log(f"    WARN: {failed_batches} batch(es) failed; query stays "
             f"incomplete and will be retried on the next run")
    _log(f"    fetched {len(records)}/{count} PubMed records total")
    return records, failed_batches == 0


def run_pubmed_searches(progress):
    _set_source("pubmed")
    total = 0
    for query in PUBMED_QUERIES:
        key = f"pubmed:{query}"
        if _should_skip(progress, key):
            _log(f"  [skip] {key[:80]}...")
            continue
        try:
            web_env, query_key, count = search_pubmed(query)
            time.sleep(NCBI_DELAY)
            complete = True
            if count > 0:
                records, complete = fetch_pubmed_metadata(web_env, query_key, count)
                total += dedup_and_append(records, "pubmed", query)
            # Only a clean crawl earns a permanent completion marker. Marking a
            # partially-fetched query done put it in search_progress.json, and
            # `_should_skip` then skipped it on every later run — so papers in
            # the failed batches were never searched again without --fresh.
            if complete:
                _mark_completed(progress, key)
        except Exception as e:
            _log(f"  ERROR in PubMed query: {e}")
    _log(f"  PubMed total new rows: {total}")
    return total


# =============================================================================
# Section 6: OpenAlex search
# =============================================================================

def _reconstruct_abstract(inverted_index):
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def _extract_openalex_pmid(work):
    ids = work.get("ids") or {}
    pmid = ids.get("pmid") or ""
    if pmid:
        pmid = pmid.replace("https://pubmed.ncbi.nlm.nih.gov/", "")
    return pmid


def _parse_openalex_works(works):
    records = []
    for w in works:
        doi_raw = (w.get("doi") or "").replace("https://doi.org/", "")
        records.append({
            "pmid": _extract_openalex_pmid(w),
            "doi": normalize_doi(doi_raw),
            "title": w.get("title") or "",
        })
    return records


def _openalex_paginate(params, label, max_results=None):
    """Generic OpenAlex cursor-paginated fetch.

    Returns ``(results, complete)``. ``complete`` is False when pagination
    stopped because a request failed rather than because the result set ended —
    `_request_with_retry` returns None once its retries are exhausted, which is
    otherwise indistinguishable from "last page". The caller must not mark a
    query permanently completed on an aborted crawl.
    """
    limit = max_results or MAX_PER_QUERY
    base_url = "https://api.openalex.org/works"
    # Send the premium api_key (verified to have budget) for higher search limits,
    # with mailto as a free polite-pool fallback. If the key's budget drains it
    # 429s "Insufficient budget" — drop the api_key line and mailto keeps search
    # working on the free pool (10 req/s / 100k per day).
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    params.setdefault("mailto", ENTREZ_EMAIL)
    params.setdefault("per_page", 200)
    params.setdefault("cursor", "*")
    params.setdefault("select", "id,doi,title,ids")

    results = []
    pages = 0
    complete = True
    while True:
        r = _request_with_retry(base_url, params=params)
        if not r:
            # Request failed after retries — this is an ABORTED crawl, not the
            # end of the result set.
            complete = False
            _log(f"    WARN: OpenAlex {label} aborted after {pages} page(s); "
                 f"query stays incomplete and will be retried next run")
            break
        data = r.json()
        works = data.get("results", [])
        if not works:
            break
        results.extend(_parse_openalex_works(works))
        pages += 1
        total_available = data.get("meta", {}).get("count", "?")
        if pages % 50 == 0:
            _log(f"    page {pages}: {len(results)} fetched so far "
                 f"(of {total_available})")
        if len(results) >= limit:
            SEARCH_RUN.fail("result cap reached; full coverage not established")
            break
        next_cursor = data.get("meta", {}).get("next_cursor")
        if not next_cursor:
            break
        params["cursor"] = next_cursor
        time.sleep(OPENALEX_DELAY)

    _log(f"  OpenAlex {label}: {len(results)} results ({pages} pages)")
    return results, complete


def run_openalex_searches(progress):
    _set_source("openalex")
    total = 0

    # Title searches
    for keyword in OPENALEX_TITLE_SEARCHES:
        key = f"openalex_title:{keyword}"
        if _should_skip(progress, key):
            _log(f"  [skip] {key}")
            continue
        try:
            flt = f"type:article,title.search:{keyword}"
            if SINCE_DATE:
                flt += f",from_publication_date:{SINCE_DATE}"
            params = {"filter": flt}
            records, complete = _openalex_paginate(params, f"title '{keyword}'")
            total += dedup_and_append(records, "openalex", f"title:{keyword}")
            if complete:
                _mark_completed(progress, key)
        except Exception as e:
            _log(f"  ERROR in OpenAlex title '{keyword}': {e}")

    # Abstract searches
    for keyword in OPENALEX_ABSTRACT_SEARCHES:
        key = f"openalex_abstract:{keyword}"
        if _should_skip(progress, key):
            _log(f"  [skip] {key}")
            continue
        try:
            flt = f"type:article,abstract.search:{keyword}"
            if SINCE_DATE:
                flt += f",from_publication_date:{SINCE_DATE}"
            params = {"filter": flt}
            records, complete = _openalex_paginate(params, f"abstract '{keyword}'")
            total += dedup_and_append(records, "openalex",
                                      f"abstract:{keyword}")
            if complete:
                _mark_completed(progress, key)
        except Exception as e:
            _log(f"  ERROR in OpenAlex abstract '{keyword}': {e}")

    _log(f"  OpenAlex total new rows: {total}")
    return total


# =============================================================================
# Section 7: Europe PMC search
# =============================================================================

def search_europepmc(query, max_results=None):
    limit = max_results or MAX_PER_QUERY
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    if SINCE_DATE:
        # CREATION_DATE = when Europe PMC first indexed the record.
        query = f"({query}) AND (CREATION_DATE:[{SINCE_DATE} TO 3000-12-31])"
    results = []
    cursor = "*"

    while True:
        params = {
            "query": query,
            "format": "json",
            "pageSize": 1000,
            "cursorMark": cursor,
            "resultType": "core",
        }
        r = _request_with_retry(base_url, params=params)
        if not r:
            SEARCH_RUN.fail("request failed after retries")
            break
        data = r.json()
        result_list = data.get("resultList", {}).get("result", [])
        if not result_list:
            break

        for item in result_list:
            doi_raw = item.get("doi", "") or ""
            results.append({
                "pmid": item.get("pmid", "") or "",
                "doi": normalize_doi(doi_raw),
                "title": item.get("title", ""),
            })

        if len(results) >= limit:
            SEARCH_RUN.fail("result cap reached; full coverage not established")
            break
        next_cursor = data.get("nextCursorMark")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(EUROPEPMC_DELAY)

    _log(f"  Europe PMC: {len(results)} results for: {query[:60]}...")
    return results


def run_europepmc_searches(progress):
    _set_source("europepmc")
    total = 0
    for query in EUROPEPMC_QUERIES:
        key = f"europepmc:{query}"
        if _should_skip(progress, key):
            _log(f"  [skip] {key[:80]}...")
            continue
        try:
            records = search_europepmc(query)
            total += dedup_and_append(records, "europepmc", query)
            _mark_completed(progress, key)
        except Exception as e:
            _log(f"  ERROR in Europe PMC query: {e}")
    _log(f"  Europe PMC total new rows: {total}")
    return total


# =============================================================================
# Section 8: Semantic Scholar search
# =============================================================================

S2_BULK_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"


def search_semantic_scholar(query, max_results=None):
    limit = max_results or MAX_PER_QUERY
    headers = {}
    if S2_API_KEY:
        headers["x-api-key"] = S2_API_KEY

    params = {
        "query": query,
        "fields": "externalIds,title",
    }
    if SINCE_DATE:
        # S2 has no index-date filter; fall back to publication date from SINCE
        # onward. The DOI dedupe drops anything already seen.
        params["publicationDateOrYear"] = f"{SINCE_DATE}:"

    results = []
    pages = 0
    while True:
        r = _request_with_retry(S2_BULK_URL, params=params, headers=headers)
        if not r:
            SEARCH_RUN.fail("request failed after retries")
            break
        data = r.json()
        papers = data.get("data", [])
        if not papers:
            break

        for p in papers:
            ext_ids = p.get("externalIds") or {}
            doi_raw = ext_ids.get("DOI", "") or ""
            pmid = str(ext_ids.get("PubMed", "") or "")
            results.append({
                "pmid": pmid if pmid != "None" else "",
                "doi": normalize_doi(doi_raw),
                "title": p.get("title") or "",
            })

        pages += 1
        if pages % 10 == 0:
            total_available = data.get("total", "?")
            _log(f"    page {pages}: {len(results)} fetched so far "
                 f"(of {total_available})")
        if len(results) >= limit:
            SEARCH_RUN.fail("result cap reached; full coverage not established")
            break
        next_token = data.get("token")
        if not next_token:
            break
        params["token"] = next_token
        time.sleep(S2_DELAY)

    _log(f"  Semantic Scholar: {len(results)} results for: {query}")
    return results


def run_semantic_scholar_searches(progress):
    _set_source("s2")
    total = 0
    for query in SEMANTIC_SCHOLAR_QUERIES:
        key = f"s2:{query}"
        if _should_skip(progress, key):
            _log(f"  [skip] {key}")
            continue
        try:
            records = search_semantic_scholar(query)
            total += dedup_and_append(records, "semantic_scholar", query)
            _mark_completed(progress, key)
        except Exception as e:
            _log(f"  ERROR in Semantic Scholar query '{query}': {e}")
    _log(f"  Semantic Scholar total new rows: {total}")
    return total


# =============================================================================
# Section 8b: CyberLeninka search (Russian-language open-access papers)
# =============================================================================

CYBERLENINKA_SEARCH_URL = "https://cyberleninka.ru/api/search"
CYBERLENINKA_BASE = "https://cyberleninka.ru"

ABSTRACTS_LOG_FIELDS = ["doi", "pmid", "source", "status", "title", "chars"]
# Per-DOI guard so reruns don't append duplicate log rows.
_abstracts_logged: set[str] = set()


def _backfill_cyberleninka_prep() -> None:
    """One-time migration: drop cached CyberLeninka query progress and stale
    'no_abstract' log rows so a re-run can capture annotations cleanly."""
    # 1. Strip cyberleninka:* entries from completed_queries so the queries
    #    actually re-execute.
    if PROGRESS_FILE.exists():
        try:
            progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            before = len(progress.get("completed_queries", []))
            progress["completed_queries"] = [
                k for k in progress.get("completed_queries", [])
                if not k.startswith("cyberleninka:")
            ]
            after = len(progress["completed_queries"])
            _write_progress(progress)
            print(f"  backfill: cleared {before - after} cyberleninka entries "
                  f"from search_progress.json")
        except Exception as e:
            print(f"  backfill: WARN reading progress: {e}")

    # 2. Drop stale rows for cyberleninka:* DOIs from abstracts_log.csv so the
    #    rewrite has a clean slate. This also resets _abstracts_logged below.
    if ABSTRACTS_LOG_CSV and ABSTRACTS_LOG_CSV.exists():
        try:
            with open(ABSTRACTS_LOG_CSV, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            kept = [r for r in rows
                    if not (r.get("doi") or "").startswith("cyberleninka:")]
            write_csv_atomic(ABSTRACTS_LOG_CSV, kept, ABSTRACTS_LOG_FIELDS)
            print(f"  backfill: dropped {len(rows) - len(kept)} stale "
                  f"cyberleninka rows from abstracts_log.csv "
                  f"({len(kept)} rows remain)")
        except Exception as e:
            print(f"  backfill: WARN rewriting abstracts_log.csv: {e}")


def _load_existing_abstract_log() -> None:
    """Populate _abstracts_logged from any pre-existing abstracts_log.csv."""
    if not ABSTRACTS_LOG_CSV or not ABSTRACTS_LOG_CSV.exists():
        return
    try:
        with open(ABSTRACTS_LOG_CSV, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                doi = (row.get("doi") or "").strip()
                if doi:
                    _abstracts_logged.add(doi)
    except Exception as e:
        _log(f"  WARN: could not read existing abstracts_log.csv: {e}")


def _write_synthetic_abstract(doi: str, title: str, text: str,
                              source: str) -> None:
    """Write a step-2-compatible abstract MD file + log row for a
    synthetic-ID article (CyberLeninka, LessWrong, ...).

    Idempotent: skips writing the MD if a non-placeholder file already exists,
    and skips appending to abstracts_log.csv if `doi` was already logged this
    run or in a prior run. `source` is recorded in the log row.
    """
    if not doi or not ABSTRACTS_DIR or not ABSTRACTS_LOG_CSV:
        return
    stem = doi_to_filename(doi)
    md_path = ABSTRACTS_DIR / f"{stem}_abstract.md"
    title_str = (title or "").strip() or "(no title available)"
    body = (text or "").strip()
    abstract_str = body or "*Abstract not available.*"

    # Write MD if missing, OR if the existing file is a step-2 "no abstract"
    # placeholder that we can now upgrade with real body text.
    PLACEHOLDER = "*Abstract not available.*"
    should_write = True
    if md_path.exists():
        try:
            existing = md_path.read_text(encoding="utf-8")
            should_write = (PLACEHOLDER in existing) and bool(body)
        except OSError:
            should_write = False
    if should_write:
        try:
            md_path.write_text(f"# {title_str}\n\n{abstract_str}\n",
                               encoding="utf-8")
        except OSError as e:
            _log(f"    WARN: could not write {md_path.name}: {e}")
            return

    # Append to abstracts_log.csv at most once per DOI (across runs).
    with STATE_LOCK:
        if doi in _abstracts_logged:
            return
        _abstracts_logged.add(doi)
        log_exists = (ABSTRACTS_LOG_CSV.exists()
                      and ABSTRACTS_LOG_CSV.stat().st_size > 0)
        with open(ABSTRACTS_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ABSTRACTS_LOG_FIELDS)
            if not log_exists:
                writer.writeheader()
            writer.writerow({
                "doi": doi,
                "pmid": "",
                "source": source if body else "none",
                "status": "ok" if body else "no_abstract",
                "title": title or "",
                "chars": len(body),
            })


def _write_cyberleninka_abstract(doi: str, title: str, annotation: str) -> None:
    """CyberLeninka-specific wrapper around _write_synthetic_abstract."""
    _write_synthetic_abstract(doi, title, annotation, "cyberleninka")


def _cyberleninka_id_from_link(link: str) -> str:
    """Extract a stable slug from a CyberLeninka article link.

    Links look like '/article/n/some-article-slug' (relative) or absolute.
    Returns 'cyberleninka:<slug>' for use as a synthetic DOI.
    """
    if not link:
        return ""
    s = link.strip()
    if s.startswith("http"):
        s = s.split("cyberleninka.ru", 1)[-1]
    s = s.lstrip("/")
    parts = [p for p in s.split("/") if p]
    slug = parts[-1] if parts else ""
    return f"cyberleninka:{slug}" if slug else ""


def search_cyberleninka(query, max_results=None):
    """Search CyberLeninka via their JSON search endpoint.

    The API has no DOI/PMID for most articles, so we synthesize an ID from
    the article link slug and store it in the `doi` column as
    'cyberleninka:<slug>'. Downstream metadata lookups (Crossref, etc.) will
    not resolve these — they remain Russian-source records flagged for review.
    """
    limit = max_results or MAX_PER_QUERY
    results = []
    page_size = 100
    offset = 0

    while True:
        try:
            r = requests.post(
                CYBERLENINKA_SEARCH_URL,
                json={"mode": "articles", "q": query,
                      "size": page_size, "from": offset},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            _log(f"    CyberLeninka request error: {e}")
            SEARCH_RUN.fail("incomplete search response")
            break

        if r.status_code != 200:
            _log(f"    CyberLeninka HTTP {r.status_code}; stopping")
            SEARCH_RUN.fail("incomplete search response")
            break

        try:
            data = r.json()
        except ValueError:
            _log("    CyberLeninka returned non-JSON; stopping")
            SEARCH_RUN.fail("incomplete search response")
            break

        articles = data.get("articles") or []
        if not articles:
            break

        for a in articles:
            link = a.get("link") or ""
            synthetic_id = _cyberleninka_id_from_link(link)
            title = a.get("name") or a.get("title") or ""
            annotation = a.get("annotation") or ""
            # Strip HTML tags from CyberLeninka's bolded query matches.
            title = re.sub(r"<[^>]+>", "", title).strip()
            annotation = re.sub(r"<[^>]+>", "", annotation).strip()
            if not synthetic_id and not title:
                continue
            results.append({
                "pmid": "",
                "doi": synthetic_id,
                "title": title,
            })
            # Save the annotation as a step-2-compatible abstract file +
            # log row so the abstracts pipeline counts this paper as fetched.
            if synthetic_id:
                _write_cyberleninka_abstract(synthetic_id, title, annotation)

        if len(results) >= limit:
            SEARCH_RUN.fail("result cap reached; full coverage not established")
            break
        if len(articles) < page_size:
            break
        offset += page_size
        time.sleep(CYBERLENINKA_DELAY)

    _log(f"  CyberLeninka: {len(results)} results for: {query[:60]}...")
    return results


def run_cyberleninka_searches(progress):
    _set_source("cyberleninka")
    total = 0
    for query in CYBERLENINKA_QUERIES:
        key = f"cyberleninka:{query}"
        if _should_skip(progress, key):
            _log(f"  [skip] {key[:80]}...")
            continue
        try:
            records = search_cyberleninka(query)
            total += dedup_and_append(records, "cyberleninka", query)
            _mark_completed(progress, key)
        except Exception as e:
            _log(f"  ERROR in CyberLeninka query '{query}': {e}")
    _log(f"  CyberLeninka total new rows: {total}")
    return total


# =============================================================================
# Section 8c: LessWrong / AlignmentForum search (forum posts as "articles")
# =============================================================================
#
# LessWrong (LW) and AlignmentForum (AF) share one database/codebase
# (ForumMagnum); an AF post is just an LW post with af=true, cross-posted, and
# the same `_id` on both sites. We use that `_id` as a synthetic DOI
# ('lesswrong:<id>') so AF/LW cross-posts dedup to one record.
#
# Posts have no DOI/PMID, so this mirrors the CyberLeninka pattern: synthetic
# IDs + a step-2-compatible abstract MD file holding the full post body.
#
# Search backend: ForumMagnum exposes an unauthenticated, Algolia-shaped
# search proxy at POST <base>/api/search. It serves BOTH match modes:
#   * keyword  -> {"query": "<text>"}
#   * tag      -> {"query": "", "params": {"facetFilters": [["tags.slug:<slug>"]]}}
# The search `body` field is plaintext, so we hydrate true markdown per post via
# the GraphQL `post` query and strip images.

LESSWRONG_FORUMS = {
    "lesswrong": "https://www.lesswrong.com",
    "alignmentforum": "https://www.alignmentforum.org",
}
LESSWRONG_UA = "metascience-observatory-research/1.0 (+birds-eye-review)"
LESSWRONG_PAGE_SIZE = 100
# ElasticSearch's default deep-pagination window. Stop before page*size exceeds
# it (the backend errors past this), and log that coverage was capped.
LESSWRONG_SEARCH_WINDOW = 10_000

_LW_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LW_IMG_HTML = re.compile(r"<img\b[^>]*>", re.I)


def _lesswrong_id(post_id: str) -> str:
    """Synthetic DOI for an LW/AF post: 'lesswrong:<_id>' (forum-independent)."""
    return f"lesswrong:{post_id}" if post_id else ""


def _strip_markdown_images(md: str) -> str:
    """Remove markdown image embeds and <img> tags from a post body."""
    if not md:
        return ""
    md = _LW_IMG_MD.sub("", md)
    md = _LW_IMG_HTML.sub("", md)
    return md


def _fetch_lesswrong_markdown(base_url: str, post_id: str) -> str:
    """Hydrate a post's full markdown body via GraphQL; '' on any failure.

    Post ids are alphanumeric, so direct interpolation is safe here.
    """
    query = ('{ post(input: {selector: {_id: "%s"}}) '
             '{ result { contents { markdown } } } }' % post_id)
    try:
        r = requests.post(f"{base_url}/graphql", json={"query": query},
                          headers={"User-Agent": LESSWRONG_UA}, timeout=60)
    except requests.exceptions.RequestException as e:
        _log(f"    LessWrong GraphQL error for {post_id}: {e}")
        return ""
    if r.status_code != 200:
        _log(f"    LessWrong GraphQL HTTP {r.status_code} for {post_id}")
        return ""
    try:
        md = r.json()["data"]["post"]["result"]["contents"]["markdown"] or ""
    except (ValueError, TypeError, KeyError):
        return ""
    return _strip_markdown_images(md).strip()


def search_lesswrong(base_url, query="", tag_slug=None, max_results=None):
    """Query the ForumMagnum /api/search backend and normalize the hits.

    Handles both keyword search (`query`) and tag search (`tag_slug` ->
    facetFilters). Returns a list of {pmid, doi, title} records and writes the
    full-markdown abstract file for each post as it goes. Hydration of the
    markdown body is skipped when the abstract MD already exists on disk, so
    reruns and cross-query duplicates don't refetch.
    """
    limit = max_results or MAX_PER_QUERY
    min_karma = LESSWRONG_CFG.get("min_karma", 0) or 0
    facet = [[f"tags.slug:{tag_slug}"]] if tag_slug else []
    label = f"tag:{tag_slug}" if tag_slug else f"kw:{query[:50]}"
    host = base_url.split("//", 1)[-1]
    results = []
    page = 0

    while True:
        # NB: the search string must live INSIDE `params` (a top-level "query"
        # field is silently ignored and the backend returns a karma-sorted feed).
        params = {"query": query, "hitsPerPage": LESSWRONG_PAGE_SIZE, "page": page}
        if facet:
            params["facetFilters"] = facet
        payload = [{"indexName": "posts", "params": params}]
        try:
            r = requests.post(f"{base_url}/api/search", json=payload,
                              headers={"User-Agent": LESSWRONG_UA}, timeout=60)
        except requests.exceptions.RequestException as e:
            _log(f"    LessWrong search request error: {e}")
            SEARCH_RUN.fail("incomplete search response")
            break
        if r.status_code != 200:
            _log(f"    LessWrong search HTTP {r.status_code}; stopping")
            SEARCH_RUN.fail("incomplete search response")
            break
        try:
            result = r.json()[0]
        except (ValueError, IndexError, KeyError):
            _log("    LessWrong search returned unexpected payload; stopping")
            SEARCH_RUN.fail("incomplete search response")
            break

        hits = result.get("hits") or []
        if not hits:
            break

        for h in hits:
            post_id = h.get("_id") or h.get("objectID") or ""
            if not post_id:
                continue
            if min_karma and (h.get("baseScore") or 0) < min_karma:
                continue
            doi = _lesswrong_id(post_id)
            title = (h.get("title") or "").strip()
            results.append({"pmid": "", "doi": doi, "title": title})

            # Hydrate full markdown only if we don't already have this post's
            # abstract file (bounds cost on reruns and cross-query repeats).
            stem = doi_to_filename(doi)
            md_path = ABSTRACTS_DIR / f"{stem}_abstract.md" if ABSTRACTS_DIR else None
            if md_path is not None and not md_path.exists():
                body = _fetch_lesswrong_markdown(base_url, post_id)
                if not body:  # fall back to the search index's plaintext body
                    body = _strip_markdown_images(h.get("body") or "").strip()
                _write_synthetic_abstract(doi, title, body, "lesswrong")
                time.sleep(LESSWRONG_DELAY)
            else:
                # Abstract already present; keep the log idempotent.
                _write_synthetic_abstract(doi, title, "", "lesswrong")

            if len(results) >= limit:
                break

        if len(results) >= limit:
            SEARCH_RUN.fail("result cap reached; full coverage not established")
            break
        nb_pages = result.get("nbPages") or 0
        page += 1
        if page >= nb_pages:
            break
        if page * LESSWRONG_PAGE_SIZE >= LESSWRONG_SEARCH_WINDOW:
            _log(f"    LessWrong: capping at {page * LESSWRONG_PAGE_SIZE} hits "
                 f"(search-window limit; {result.get('nbHits')} matched {label})")
            break
        time.sleep(LESSWRONG_DELAY)

    _log(f"  LessWrong[{host}]: {len(results)} results for {label}")
    return results


def run_lesswrong_searches(progress):
    _set_source("lesswrong")
    forums = LESSWRONG_CFG.get("forums") or ["lesswrong"]
    tags = LESSWRONG_CFG.get("tags") or []
    keywords = LESSWRONG_CFG.get("keywords") or []
    total = 0
    for forum in forums:
        base_url = LESSWRONG_FORUMS.get(forum)
        if not base_url:
            _log(f"  unknown forum '{forum}'; skipping "
                 f"(valid: {', '.join(LESSWRONG_FORUMS)})")
            continue
        searches = ([("tag", t) for t in tags]
                    + [("kw", k) for k in keywords])
        for kind, term in searches:
            key = f"lesswrong:{forum}:{kind}:{term}"
            if _should_skip(progress, key):
                _log(f"  [skip] {key[:80]}...")
                continue
            try:
                if kind == "tag":
                    records = search_lesswrong(base_url, tag_slug=term)
                else:
                    records = search_lesswrong(base_url, query=term)
                total += dedup_and_append(records, f"lesswrong:{forum}",
                                          f"{kind}:{term}")
                _mark_completed(progress, key)
            except Exception as e:
                _log(f"  ERROR in LessWrong {kind} '{term}' ({forum}): {e}")
    _log(f"  LessWrong total new rows: {total}")
    return total


# =============================================================================
# Section 9: PMID-to-DOI enrichment
# =============================================================================

def _batch_pmid_to_doi_pmc_converter(pmids):
    """Use NCBI PMC ID Converter to get DOIs for PMIDs."""
    url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    results = {}
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i + 200]
        params = {
            "ids": ",".join(batch),
            "idtype": "pmid",
            "format": "json",
            "tool": "literature_search",
            "email": ENTREZ_EMAIL,
        }
        r = _request_with_retry(url, params=params, timeout=30)
        if r and r.status_code == 200:
            for record in r.json().get("records", []):
                pmid = str(record.get("pmid", ""))
                doi = record.get("doi", "")
                if pmid and doi:
                    results[pmid] = normalize_doi(doi)
        time.sleep(NCBI_DELAY)
        if i > 0 and (i // 200) % 10 == 0:
            print(f"    PMC converter: processed {i}/{len(pmids)}")
    return results


def _batch_pmid_to_doi_entrez(pmids):
    """Fallback: extract DOIs from Medline records via regex."""
    results = {}
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i + 200]
        try:
            handle = Entrez.efetch(db="pubmed", id=",".join(batch),
                                   rettype="medline", retmode="text")
            for rec in Medline.parse(handle):
                pmid = rec.get("PMID", "")
                for field in ["AID", "LID", "SO"]:
                    val = rec.get(field)
                    if not val:
                        continue
                    entries = val if isinstance(val, list) else [val]
                    for text in entries:
                        m = DOI_RE.search(text)
                        if m:
                            results[pmid] = normalize_doi(m.group(1))
                            break
                    if pmid in results:
                        break
            handle.close()
        except Exception as e:
            _log(f"    Entrez DOI lookup error at offset {i}: {e}")
        time.sleep(NCBI_DELAY)
    return results


def enrich_missing_dois():
    """Find rows with PMID but no DOI in raw CSV and attempt to fill them in."""
    if not RAW_CSV.exists():
        return

    rows = []
    with open(RAW_CSV, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    missing = [r for r in rows if r.get("pmid") and not r.get("doi")]
    if not missing:
        print("  No rows need DOI enrichment.")
        return

    pmids_to_enrich = [r["pmid"] for r in missing]
    print(f"  Enriching DOIs for {len(pmids_to_enrich)} rows with PMID "
          f"but no DOI...")

    # Strategy 1: PMC ID Converter
    pmc_results = _batch_pmid_to_doi_pmc_converter(pmids_to_enrich)
    found_count = len(pmc_results)
    print(f"    PMC converter found {found_count} DOIs")

    # Strategy 2: Entrez fallback for remaining
    remaining = [p for p in pmids_to_enrich if p not in pmc_results]
    entrez_results = {}
    if remaining:
        entrez_results = _batch_pmid_to_doi_entrez(remaining)
        print(f"    Entrez fallback found {len(entrez_results)} more DOIs")

    all_results = {**pmc_results, **entrez_results}

    # Update rows in place
    updated = 0
    for row in rows:
        pmid = row.get("pmid", "")
        if pmid and not row.get("doi") and pmid in all_results:
            row["doi"] = all_results[pmid]
            updated += 1

    # Rewrite the CSV atomically: it is the cumulative search ledger, and a
    # crash mid-rewrite used to truncate it.
    write_csv_atomic(RAW_CSV, rows, FIELDNAMES)

    print(f"  Enriched {updated} rows with DOIs "
          f"(of {len(pmids_to_enrich)} missing)")


# =============================================================================
# Section 10: Final dedup & clean CSV export
# =============================================================================

def group_records(rows: list[dict]) -> list[list[dict]]:
    """Group rows that share a DOI, a PMID or a long normalized title.

    A real union-find: the old grouping re-scanned every key map on each merge,
    which is quadratic and took minutes on a 100k-row corpus. Groups come back
    in order of their first row, rows within a group in file order — so "the
    first DOI in the group" is the first one the search recorded.
    """
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_seen: dict[tuple[str, str], int] = {}
    for i, row in enumerate(rows):
        doi = (row.get("doi") or "").strip().lower()
        pmid = (row.get("pmid") or "").strip()
        title = _normalize_title(row.get("title", ""))
        keys = []
        if doi:
            keys.append(("doi", doi))
        if pmid:
            keys.append(("pmid", pmid))
        if title and len(title) > 20:
            keys.append(("title", title))
        for k in keys:
            if k in first_seen:
                union(i, first_seen[k])
            else:
                first_seen[k] = i

    groups: dict[int, list[dict]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[find(i)].append(row)
    return [groups[root] for root in sorted(groups)]


def produce_final_csv():
    """Read raw CSV, deduplicate with union-find grouping, produce final CSV."""
    if not RAW_CSV.exists():
        print("  No raw CSV found.")
        return

    with open(RAW_CSV, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    groups = group_records(rows)

    # Merge each group: pick best identifiers
    final_rows = []
    for group in groups:
        doi = ""
        pmid = ""
        for r in group:
            if not doi:
                doi = (r.get("doi") or "").strip()
            if not pmid:
                pmid = (r.get("pmid") or "").strip()
        if doi or pmid:
            final_rows.append({"doi": doi, "pmid": pmid})

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(FINAL_CSV, final_rows, ["doi", "pmid"])

    print(f"\nFinal deduplicated CSV: {len(final_rows)} unique papers "
          f"-> {FINAL_CSV}")

    # Stats
    with_doi = sum(1 for r in final_rows if r["doi"])
    with_pmid = sum(1 for r in final_rows if r["pmid"])
    with_both = sum(1 for r in final_rows if r["doi"] and r["pmid"])
    doi_only = with_doi - with_both
    pmid_only = with_pmid - with_both
    print(f"  {with_both} with both DOI and PMID")
    print(f"  {doi_only} with DOI only")
    print(f"  {pmid_only} with PMID only")


# =============================================================================
# Section 12: Main
# =============================================================================

def main(args):
    global MAX_PER_QUERY, SINCE_DATE, IGNORE_PROGRESS, SEARCH_RUN
    SEARCH_RUN = QueryLedger()
    SINCE_DATE = None
    global PUBMED_QUERIES, OPENALEX_TITLE_SEARCHES, OPENALEX_ABSTRACT_SEARCHES
    global EUROPEPMC_QUERIES, SEMANTIC_SCHOLAR_QUERIES, CYBERLENINKA_QUERIES
    global LESSWRONG_CFG

    MAX_PER_QUERY = args.max_per_query

    # Incremental-update mode. --since restricts to records added on/after a date;
    # --fresh re-issues every query. Either implies ignoring the progress baseline.
    if args.since:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
            print(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}")
            return 2
        SINCE_DATE = args.since
    IGNORE_PROGRESS = bool(args.fresh or args.since)
    run_started = time.strftime("%Y-%m-%d")

    # --- Load config and configure per-project directories ---
    config = load_config(args.config)
    topic_name = config.get("topic", {}).get("name", "default")
    configure_project_paths(topic_name)
    print(f"Project: {topic_name!r} -> {PROJECT_DIR}")

    if args.backfill_cyberleninka_abstracts:
        _backfill_cyberleninka_prep()
        # Force the rest of main() to run only the cyberleninka source.
        args.sources = "cyberleninka"

    search_cfg = config.get("search", {})

    PUBMED_QUERIES = search_cfg.get("pubmed", [])
    OPENALEX_TITLE_SEARCHES = search_cfg.get("openalex_title", [])
    OPENALEX_ABSTRACT_SEARCHES = search_cfg.get("openalex_abstract", [])
    EUROPEPMC_QUERIES = search_cfg.get("europepmc", [])
    SEMANTIC_SCHOLAR_QUERIES = search_cfg.get("semantic_scholar", [])
    CYBERLENINKA_QUERIES = search_cfg.get("cyberleninka", [])
    LESSWRONG_CFG = search_cfg.get("lesswrong", {}) or {}

    # Strip YAML multi-line whitespace artifacts from queries
    PUBMED_QUERIES = [q.strip() for q in PUBMED_QUERIES]
    OPENALEX_TITLE_SEARCHES = [q.strip() for q in OPENALEX_TITLE_SEARCHES]
    OPENALEX_ABSTRACT_SEARCHES = [q.strip() for q in OPENALEX_ABSTRACT_SEARCHES]
    EUROPEPMC_QUERIES = [q.strip() for q in EUROPEPMC_QUERIES]
    SEMANTIC_SCHOLAR_QUERIES = [q.strip() for q in SEMANTIC_SCHOLAR_QUERIES]
    CYBERLENINKA_QUERIES = [q.strip() for q in CYBERLENINKA_QUERIES]
    if LESSWRONG_CFG:
        LESSWRONG_CFG["forums"] = [str(f).strip()
                                   for f in (LESSWRONG_CFG.get("forums")
                                             or ["lesswrong"])]
        LESSWRONG_CFG["tags"] = [str(t).strip()
                                 for t in (LESSWRONG_CFG.get("tags") or [])]
        LESSWRONG_CFG["keywords"] = [str(k).strip()
                                     for k in (LESSWRONG_CFG.get("keywords") or [])]
    LW_QUERY_COUNT = (len(LESSWRONG_CFG.get("tags", []))
                      + len(LESSWRONG_CFG.get("keywords", [])))

    # Validate that we have at least some queries
    total_queries = (len(PUBMED_QUERIES) + len(OPENALEX_TITLE_SEARCHES)
                     + len(OPENALEX_ABSTRACT_SEARCHES)
                     + len(EUROPEPMC_QUERIES)
                     + len(SEMANTIC_SCHOLAR_QUERIES)
                     + len(CYBERLENINKA_QUERIES)
                     + LW_QUERY_COUNT)
    if total_queries == 0:
        print("ERROR: No search queries found in config. Check your config "
              f"file: {args.config}")
        print("  Expected keys under 'search': pubmed, openalex_title, "
              "openalex_abstract, europepmc, semantic_scholar, cyberleninka, "
              "lesswrong")
        return 2

    if args.sources is None:
        # Opt-in sources run whenever the config actually supplies queries for
        # them, so a pipeline run (which passes no --sources) doesn't silently
        # skip configured CyberLeninka / LessWrong searches.
        default = ["pubmed", "openalex", "europepmc", "s2"]
        if CYBERLENINKA_QUERIES:
            default.append("cyberleninka")
        if any(LESSWRONG_CFG.get(k) for k in ("tags", "keywords")):
            default.append("lesswrong")
        args.sources = ",".join(default)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    if not sources or set(sources) - {"pubmed", "openalex", "europepmc", "s2", "cyberleninka", "lesswrong"}:
        print("ERROR: unrecognized or empty source selection")
        return 2
    if "pubmed" in sources and PUBMED_QUERIES and not ENTREZ_EMAIL:
        print("ERROR: EMAIL is not set. NCBI requires a contact address for "
              "PubMed searches (and the PMID->DOI enrichment).\n"
              "  Set EMAIL=you@example.org in .env.local (see .env.example), or "
              "run without pubmed: --sources openalex,europepmc,s2")
        return 2

    print(f"Config: {args.config}")
    print(f"Max per query: {MAX_PER_QUERY}")
    print(f"Sources: {args.sources}")
    print(f"Parallel: {'yes' if args.parallel else 'no'}")
    if SINCE_DATE:
        print(f"Incremental: only records indexed since {SINCE_DATE} "
              "(re-running all queries, deduped against existing rows)")
    elif IGNORE_PROGRESS:
        print("Full rescan (--fresh): re-running all queries, ignoring progress")
    print(f"NCBI API key: {'yes' if NCBI_API_KEY else 'no'}")
    print(f"OpenAlex API key: {'yes' if OPENALEX_API_KEY else 'no'}")
    print(f"Semantic Scholar API key: {'yes' if S2_API_KEY else 'no'}")
    print(f"Queries loaded: {len(PUBMED_QUERIES)} PubMed, "
          f"{len(OPENALEX_TITLE_SEARCHES)} OA-title, "
          f"{len(OPENALEX_ABSTRACT_SEARCHES)} OA-abstract, "
          f"{len(EUROPEPMC_QUERIES)} EuroPMC, "
          f"{len(SEMANTIC_SCHOLAR_QUERIES)} S2, "
          f"{len(CYBERLENINKA_QUERIES)} CyberLeninka, "
          f"{LW_QUERY_COUNT} LessWrong "
          f"({len(LESSWRONG_CFG.get('forums', []))} forum(s))")

    progress = load_progress()

    # Pre-populate the in-memory "already logged in abstracts_log.csv" set
    # so a CyberLeninka rerun doesn't append duplicate log rows.
    _load_existing_abstract_log()

    # Load existing rows into dedup tracker
    existing = load_seen_from_csv()
    if existing:
        print(f"Loaded {existing} existing rows into dedup tracker "
              f"({len(seen_dois)} DOIs, {len(seen_pmids)} PMIDs, "
              f"{len(seen_titles)} titles)")

    grand_total = existing

    src_to_fn = [
        ("pubmed",       run_pubmed_searches),
        ("openalex",     run_openalex_searches),
        ("europepmc",    run_europepmc_searches),
        ("s2",           run_semantic_scholar_searches),
        ("cyberleninka", run_cyberleninka_searches),
        ("lesswrong",    run_lesswrong_searches),
    ]
    todo = [(s, fn) for s, fn in src_to_fn if s in sources]

    if args.parallel and len(todo) > 1:
        print(f"\n=== Running {len(todo)} sources in parallel: "
              f"{', '.join(s for s, _ in todo)} ===")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(todo)) as ex:
            futures = {ex.submit(fn, progress): s for s, fn in todo}
            for fut in concurrent.futures.as_completed(futures):
                src = futures[fut]
                try:
                    grand_total += fut.result()
                except Exception as e:
                    SEARCH_RUN.begin(src)
                    SEARCH_RUN.fail(redact_secrets(e))
                    print(redact_secrets(f"[{src}] FATAL: {e}"), flush=True)
    else:
        for src, fn in todo:
            print(f"\n=== {src} Searches ===")
            try:
                grand_total += fn(progress)
            except Exception as exc:
                SEARCH_RUN.begin(src)
                SEARCH_RUN.fail(redact_secrets(exc))
                print(redact_secrets(f"[{src}] FATAL: {exc}"), flush=True)
        # Clear thread-local source tag now that source phase is over.
        _set_source(None)

    print("\n=== DOI Enrichment ===")
    enrich_missing_dois()

    print("\n=== Producing Final CSV ===")
    produce_final_csv()

    # Record when this search completed so the GUI/CLI can auto-fill --since on
    # the next incremental update.
    configured = {name for name, queries in {
        "pubmed": PUBMED_QUERIES, "openalex": OPENALEX_TITLE_SEARCHES + OPENALEX_ABSTRACT_SEARCHES,
        "europepmc": EUROPEPMC_QUERIES, "s2": SEMANTIC_SCHOLAR_QUERIES,
        "cyberleninka": CYBERLENINKA_QUERIES, "lesswrong": LW_QUERY_COUNT,
    }.items() if queries}
    progress["last_run_coverage"] = dict(SEARCH_RUN.results)
    watermarks = progress.setdefault("query_watermarks", {})
    for key, coverage in SEARCH_RUN.results.items():
        if coverage == "complete":
            watermarks[key] = run_started
    # A resumed baseline only covers the dates at which its cached queries
    # actually ran. Legacy caches without dates require a fresh/incremental scan.
    if (not SEARCH_RUN.incomplete and configured.issubset(set(sources))
            and SEARCH_RUN.results and all(k in watermarks for k in SEARCH_RUN.results)):
        progress["last_run_date"] = min(watermarks[k] for k in SEARCH_RUN.results)
    save_progress(progress)

    # In incremental mode, write this cycle's new DOIs so downstream steps can
    # scope themselves to just the new papers (avoids re-enriching/re-downloading
    # the whole cumulative corpus). Overwritten each run.
    if IGNORE_PROGRESS:
        new_dois_path = DATA_DIR / "new_dois.csv"
        write_csv_atomic(new_dois_path, [{"doi": d} for d in sorted(NEW_DOIS_THIS_RUN)],
                         ["doi"])
        print(f"Wrote {len(NEW_DOIS_THIS_RUN)} new DOIs this run -> {new_dois_path}")

    print(f"\nDone. Total rows in raw CSV: ~{grand_total}")
    if SEARCH_RUN.incomplete:
        print("Incomplete search; checkpoint retained:", SEARCH_RUN.incomplete)
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Search for papers across multiple literature APIs"
    )
    parser.add_argument(
        "--config", type=str, required=True,
        help="Path to the review's YAML config"
    )
    parser.add_argument(
        "--max-per-query", type=int, default=500_000,
        help="Max results per query (default: 500000, effectively unlimited)"
    )
    parser.add_argument(
        "--sources", type=str, default=None,
        help="Comma-separated list of sources "
             "(default: pubmed,openalex,europepmc,s2, plus cyberleninka / "
             "lesswrong when the config has queries for them; "
             "available: pubmed,openalex,europepmc,s2,cyberleninka,lesswrong)"
    )
    parser.add_argument(
        "--parallel", action="store_true",
        help="Run the requested sources concurrently (one thread per source). "
             "Each source's queries still run sequentially within its thread."
    )
    parser.add_argument(
        "--since", type=str, default=None, metavar="YYYY-MM-DD",
        help="Incremental update: only fetch records on or after this date. "
             "PubMed (edat) and Europe PMC (CREATION_DATE) window on index date "
             "so late-indexed older papers are still caught; OpenAlex and "
             "Semantic Scholar window on publication date (free-tier limitation). "
             "Re-runs all queries (deduped)."
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore search_progress.json and re-issue every query (full "
             "rescan). New DOIs are deduped against existing rows. Use "
             "periodically to catch papers that were back-indexed."
    )
    parser.add_argument(
        "--backfill-cyberleninka-abstracts", action="store_true",
        help="One-shot migration for existing projects: clear cached "
             "CyberLeninka query progress, drop stale 'no_abstract' rows for "
             "cyberleninka:* DOIs from abstracts_log.csv, then re-run the "
             "CyberLeninka source so annotations are captured this time."
    )
    args = parser.parse_args()
    raise SystemExit(main(args))
