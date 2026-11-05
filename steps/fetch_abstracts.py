#!/usr/bin/env python3
"""Fetch abstracts for the DOIs/PMIDs found in step 1.

Reads projects/{slug}/data/search_dois.csv, fetches an abstract for each
paper, and writes one markdown file per paper to projects/{slug}/abstracts/.

Source fallback order per paper: PubMed -> Europe PMC -> Crossref -> OpenAlex.
PubMed is fetched in bulk first (batched efetch), the rest run per-paper in a
thread pool. Resumable: papers with an existing abstract file are skipped.

A paper no source has an abstract for gets a title-only stub
(``*Abstract not available.*``). Two kinds are told apart in the log
(``abstracts_log.csv``: ``status``, ``reason``, ``fetched_at``):

* ``no_abstract`` — every source answered and none has one. Permanent.
* ``error`` — a source could not be reached (network, 429/5xx after retries).
  The stub is provisional: the paper is fetched again on later runs (up to
  ``MAX_TRANSIENT_ATTEMPTS``), and ``classify.py quick`` re-screens it as
  soon as a real abstract replaces the stub.

Usage:
    python steps/fetch_abstracts.py --config configs/config_x.yaml
    python steps/fetch_abstracts.py --config ... --workers 12 --limit 100
    python steps/fetch_abstracts.py --config ... --sources pubmed,europepmc
"""

import argparse
import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from Bio import Entrez, Medline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.environment import load_local_env as load_dotenv

from ber_core.utils import load_config, slugify, doi_to_filename, write_csv_atomic

# =============================================================================
# Section 1: Configuration & per-project paths
# =============================================================================

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(BASE_DIR / ".env.local")

PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

# Per-project paths -- populated from config in main()
PROJECT_DIR = None
DATA_DIR = None
ABSTRACTS_DIR = None
INPUT_CSV = None
LOG_CSV = None


def configure_project_paths(topic_name: str) -> None:
    global PROJECT_DIR, DATA_DIR, ABSTRACTS_DIR, INPUT_CSV, LOG_CSV
    slug = slugify(topic_name)
    PROJECT_DIR = PROJECTS_DIR / slug
    DATA_DIR = PROJECT_DIR / "data"
    ABSTRACTS_DIR = PROJECT_DIR / "abstracts"
    INPUT_CSV = DATA_DIR / "search_dois.csv"
    LOG_CSV = DATA_DIR / "abstracts_log.csv"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ABSTRACTS_DIR.mkdir(parents=True, exist_ok=True)


# NCBI and Crossref ask every client for a contact address. There is no
# default: main() refuses to query them without one (see _require_email).
ENTREZ_EMAIL = os.environ.get("EMAIL", "").strip()
NCBI_API_KEY = os.environ.get("ENTREZ_EUTILS_API_KEY", "")
OPENALEX_API_KEY = os.environ.get("OPENALEXAPIKEY", "")

Entrez.email = ENTREZ_EMAIL
if NCBI_API_KEY:
    Entrez.api_key = NCBI_API_KEY
    NCBI_DELAY = 0.11
else:
    NCBI_DELAY = 0.34

EUROPEPMC_DELAY = 0.15
CROSSREF_DELAY = 0.05
OPENALEX_DELAY = 0.05

MIN_ABSTRACT_CHARS = 50  # below this, treat as missing and try the next source
LOG_FIELDS = ["doi", "pmid", "source", "status", "title", "chars",
              "reason", "fetched_at"]

ABSTRACT_STUB = "*Abstract not available.*"
#: Second line of a stub written after a TRANSIENT failure. The first line
#: keeps the marker classify recognises; this one says the stub is not final.
TRANSIENT_NOTE = "<!-- abstract fetch failed transiently; retried on the next run -->"
#: A paper whose fetch keeps failing transiently stops being retried after this
#: many attempts (``--redo`` still forces it).
MAX_TRANSIENT_ATTEMPTS = 5

# Per-source outcomes. FOUND: usable abstract. ABSENT: the source answered and
# has none (404, or a record without an abstract). ERROR: we could not get an
# answer (network failure, or 429/5xx through every retry).
FOUND, ABSENT, ERROR = "found", "absent", "error"


# =============================================================================
# Section 2: HTTP helpers
# =============================================================================

def _request(url, params=None, headers=None, max_retries=3, base_delay=2,
             timeout=30):
    """GET with retries. Returns ``(response | None, outcome)``.

    ``outcome`` is ERROR when no answer could be had (network failure, or a
    429/5xx on every attempt, or an auth rejection) and ABSENT for a definitive
    "no such record" (404 and other 4xx), so callers can tell "this paper has
    no abstract" from "we could not ask".
    """
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
                continue
            return None, ERROR
        if r.status_code == 200:
            return r, FOUND
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
            continue
        if r.status_code in (401, 403):
            return None, ERROR      # a key problem, not a fact about the paper
        return None, ABSENT
    return None, ERROR


def _request_with_retry(url, params=None, headers=None,
                        max_retries=3, base_delay=2, timeout=30):
    """Back-compatible wrapper: the response, or None."""
    return _request(url, params=params, headers=headers, max_retries=max_retries,
                    base_delay=base_delay, timeout=timeout)[0]


def _strip_jats(text: str) -> str:
    """Strip JATS/HTML tags and collapse whitespace (Crossref returns JATS)."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _reconstruct_inverted_index(idx) -> str:
    if not idx:
        return ""
    pairs = [(pos, word) for word, positions in idx.items() for pos in positions]
    pairs.sort()
    return " ".join(w for _, w in pairs)


# =============================================================================
# Section 3: Source-specific fetchers
# =============================================================================

def fetch_pubmed_bulk(pmids: list[str]) -> dict[str, tuple[str, str]]:
    """Batch-fetch (title, abstract) for a list of PMIDs from PubMed.

    Returns dict mapping pmid -> (title, abstract). Missing PMIDs are absent.
    """
    if not pmids:
        return {}
    results = {}
    batch_size = 200
    n = len(pmids)
    print(f"  Fetching {n} PubMed records in batches of {batch_size}...",
          file=sys.stderr, flush=True)
    for start in range(0, n, batch_size):
        batch = pmids[start:start + batch_size]
        try:
            handle = Entrez.efetch(db="pubmed", id=",".join(batch),
                                   rettype="medline", retmode="text")
            for rec in Medline.parse(handle):
                pmid = (rec.get("PMID") or "").strip()
                title = (rec.get("TI") or "").strip()
                abstract = (rec.get("AB") or "").strip()
                if pmid:
                    results[pmid] = (title, abstract)
            handle.close()
        except Exception as e:
            print(f"    PubMed batch at offset {start} error: {e}",
                  file=sys.stderr, flush=True)
            results[_PUBMED_FAILED_KEY] = ("", "")
        time.sleep(NCBI_DELAY)
        if start > 0 and (start // batch_size) % 10 == 0:
            print(f"    fetched {start}/{n}", file=sys.stderr, flush=True)
    got = sum(1 for k, v in results.items() if v[1] and k != _PUBMED_FAILED_KEY)
    print(f"  PubMed bulk: {got}/{n} have abstracts", file=sys.stderr, flush=True)
    return results


def fetch_europepmc(doi: str = "", pmid: str = "") -> tuple[str, str]:
    """Look up one paper in Europe PMC. Returns (title, abstract)."""
    return fetch_europepmc_outcome(doi=doi, pmid=pmid)[:2]


def fetch_europepmc_outcome(doi: str = "", pmid: str = "") -> tuple[str, str, str]:
    """Europe PMC lookup -> (title, abstract, outcome)."""
    if not (doi or pmid):
        return "", "", ABSENT
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    if pmid:
        query = f'EXT_ID:"{pmid}" AND SRC:MED'
    else:
        canonical = doi.replace("--", "/")
        query = f'DOI:"{canonical}"'
    params = {"query": query, "format": "json",
              "resultType": "core", "pageSize": 1}
    r, outcome = _request(base_url, params=params, timeout=20)
    if not r:
        return "", "", outcome
    try:
        results = r.json().get("resultList", {}).get("result", [])
        if not results:
            return "", "", ABSENT
        item = results[0]
        abstract = (item.get("abstractText") or "").strip()
        return (item.get("title") or "").strip(), abstract, FOUND if abstract else ABSENT
    except Exception:
        return "", "", ERROR


def fetch_crossref(doi: str) -> tuple[str, str]:
    """Look up one paper in Crossref. Returns (title, abstract)."""
    return fetch_crossref_outcome(doi)[:2]


def fetch_crossref_outcome(doi: str) -> tuple[str, str, str]:
    """Crossref lookup -> (title, abstract, outcome)."""
    if not doi:
        return "", "", ABSENT
    canonical = doi.replace("--", "/")
    url = f"https://api.crossref.org/works/{canonical}"
    headers = {
        "User-Agent": f"birds_eye_review/1.0 (mailto:{ENTREZ_EMAIL})",
    }
    r, outcome = _request(url, headers=headers, timeout=20)
    if not r:
        return "", "", outcome
    try:
        msg = r.json().get("message", {})
        title_list = msg.get("title") or []
        title = (title_list[0] if title_list else "").strip()
        abstract = _strip_jats(msg.get("abstract") or "")
        return title, abstract, FOUND if abstract else ABSENT
    except Exception:
        return "", "", ERROR


def fetch_openalex(doi: str = "", pmid: str = "") -> tuple[str, str]:
    """Look up one paper in OpenAlex. Returns (title, abstract)."""
    return fetch_openalex_outcome(doi=doi, pmid=pmid)[:2]


def fetch_openalex_outcome(doi: str = "", pmid: str = "") -> tuple[str, str, str]:
    """OpenAlex lookup -> (title, abstract, outcome).

    OpenAlex stores abstracts as inverted indices; we reconstruct. Its key is
    a query parameter (the only form the API documents); request errors here
    are never logged with the URL.
    """
    if not (doi or pmid):
        return "", "", ABSENT
    if doi:
        canonical = doi.replace("--", "/")
        url = f"https://api.openalex.org/works/doi:{canonical}"
    else:
        url = f"https://api.openalex.org/works/pmid:{pmid}"
    params = {}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    r, outcome = _request(url, params=params, timeout=20)
    if not r:
        return "", "", outcome
    try:
        data = r.json()
        title = (data.get("title") or "").strip()
        abstract = _reconstruct_inverted_index(data.get("abstract_inverted_index"))
        return title, abstract, FOUND if abstract else ABSENT
    except Exception:
        return "", "", ERROR


# =============================================================================
# Section 4: Per-paper worker
# =============================================================================

def _fetch_one(doi: str, pmid: str, pubmed_data: dict, sources: list[str]
               ) -> tuple[str, str, str]:
    """``(source_name, title, abstract)`` — see :func:`_fetch_with_status`."""
    return _fetch_with_status(doi, pmid, pubmed_data, sources)[:3]


def _fetch_with_status(doi: str, pmid: str, pubmed_data: dict, sources: list[str]
                       ) -> tuple[str, str, str, str]:
    """Try sources in order until one yields a usable abstract.

    Returns ``(source_name, title, abstract, failure)``. ``source_name`` is
    "none" if all failed; ``failure`` is "" on success, "no_abstract" when
    every source ANSWERED without one, and "error" when at least one could not
    be reached — only the last is worth retrying. Title from any source is kept
    as a fallback even if abstract was empty.
    """
    title = ""
    errored = False

    # Local SciSciNet tier first: a hit costs a disk read instead of a network
    # round-trip. No-op when BER_SCISCINET_DIR is unset.
    if "local" in sources:
        try:
            from ber_core import local_sciscinet

            hit = local_sciscinet.lookup_abstract(doi=doi, pmid=pmid)
            if hit:
                t, a = hit
                if a and len(a) >= MIN_ABSTRACT_CHARS:
                    return "local", t or title, a, ""
                if t and not title:
                    title = t
        except Exception:  # noqa: BLE001 -- the fast path must never break the run
            pass

    if "pubmed" in sources and pmid:
        if pmid in pubmed_data:
            t, a = pubmed_data[pmid]
            if a and len(a) >= MIN_ABSTRACT_CHARS:
                return "pubmed", t, a, ""
            if t:
                title = t
        elif pubmed_data.get(_PUBMED_FAILED_KEY):
            errored = True      # its bulk batch failed: unknown, not absent

    for name, fetch, delay, needs_doi in (
            ("europepmc", fetch_europepmc_outcome, EUROPEPMC_DELAY, False),
            ("crossref", fetch_crossref_outcome, CROSSREF_DELAY, True),
            ("openalex", fetch_openalex_outcome, OPENALEX_DELAY, False)):
        if name not in sources or (needs_doi and not doi):
            continue
        if needs_doi:
            t, a, outcome = fetch(doi)
        else:
            t, a, outcome = fetch(doi=doi, pmid=pmid)
        time.sleep(delay)
        if a and len(a) >= MIN_ABSTRACT_CHARS:
            return name, t or title, a, ""
        if outcome == ERROR:
            errored = True
        if t and not title:
            title = t

    return "none", title, "", "error" if errored else "no_abstract"


#: Set in the PubMed bulk map when a batch failed, so a PMID missing from it is
#: read as "unknown" rather than "PubMed has no abstract".
_PUBMED_FAILED_KEY = "__batch_failed__"


def _save_local_abstract(doi: str, pmid: str, source: str, title: str,
                         abstract: str) -> None:
    """Write an API-fetched abstract back to the local tier so the next run reads
    it from disk. No-op unless write-back is enabled (see local_sciscinet)."""
    if source in ("local", "none") or not abstract:
        return
    try:
        from ber_core import local_sciscinet

        if local_sciscinet.writable():
            local_sciscinet.save_abstract(doi=doi, pmid=pmid, title=title,
                                          abstract=abstract)
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
# Section 5: Output
# =============================================================================

def _write_abstract_file(stem: str, title: str, abstract: str,
                         transient: bool = False) -> None:
    """Write a markdown abstract file. Always writes, even if abstract empty.

    A stub written after a TRANSIENT failure carries :data:`TRANSIENT_NOTE`, so
    a reader can tell it is provisional. Written atomically: classify may be
    reading the directory concurrently.
    """
    from ber_core.storage import atomic_text
    path = ABSTRACTS_DIR / f"{stem}_abstract.md"
    title_str = title.strip() or "(no title available)"
    abstract_str = abstract.strip() or ABSTRACT_STUB
    note = f"\n{TRANSIENT_NOTE}\n" if (transient and not abstract.strip()) else ""
    atomic_text(path, f"# {title_str}\n\n{abstract_str}\n{note}")


def _ensure_log_schema(log_csv: Path) -> None:
    """Upgrade an existing log to the current header (adds reason/fetched_at)."""
    if not log_csv.exists() or log_csv.stat().st_size == 0:
        return
    with open(log_csv, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        if header == LOG_FIELDS:
            return
        rows = list(reader)
    write_csv_atomic(log_csv, rows, LOG_FIELDS)


def transient_failures(log_csv: Path) -> dict[str, int]:
    """stem -> consecutive transient failures, for papers whose LATEST log row
    is a transient error (a later success or confirmed absence resets it)."""
    streak: dict[str, int] = {}
    if not log_csv.exists():
        return streak
    with open(log_csv, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip()
            pmid = (row.get("pmid") or "").strip()
            if not (doi or pmid):
                continue
            stem = doi_to_filename(doi) if doi else f"pmid_{pmid}"
            if (row.get("status") or "") == "error":
                streak[stem] = streak.get(stem, 0) + 1
            else:
                streak.pop(stem, None)
    return streak


def _existing_stems() -> set[str]:
    if not ABSTRACTS_DIR.exists():
        return set()
    return {p.name[:-len("_abstract.md")]
            for p in ABSTRACTS_DIR.glob("*_abstract.md")}


# =============================================================================
# Section 6: Main
# =============================================================================

def _require_email(sources: list[str]) -> None:
    """NCBI (PubMed) and Crossref require a contact address; refuse without one."""
    needs = [s for s in ("pubmed", "crossref") if s in sources]
    if needs and not ENTREZ_EMAIL:
        print(f"ERROR: EMAIL is not set, and {' and '.join(needs)} require a "
              f"contact address with every request.\n"
              f"  Set EMAIL=you@example.org in .env.local (see .env.example), or\n"
              f"  drop those sources with --sources local,europepmc,openalex.",
              file=sys.stderr, flush=True)
        sys.exit(2)


def main(args):
    config = load_config(args.config)
    topic_name = config.get("topic", {}).get("name", "default")
    configure_project_paths(topic_name)
    print(f"Project: {topic_name!r} -> {PROJECT_DIR}", file=sys.stderr, flush=True)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    _require_email(sources)

    if not INPUT_CSV.exists():
        print(f"ERROR: Input CSV not found: {INPUT_CSV}\n"
              f"  Run search_papers.py first.", file=sys.stderr, flush=True)
        sys.exit(1)

    # Read input DOI/PMID list
    rows = []
    with open(INPUT_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    print(f"Loaded {len(rows)} papers from {INPUT_CSV}",
          file=sys.stderr, flush=True)

    # Determine which papers still need fetching. A stub left by a TRANSIENT
    # failure does not count as fetched: it is retried (up to a cap), where it
    # used to be a permanent "*Abstract not available.*" nobody ever revisited.
    _ensure_log_schema(LOG_CSV)
    retry = {stem for stem, n in transient_failures(LOG_CSV).items()
             if n < MAX_TRANSIENT_ATTEMPTS}
    existing = set() if args.redo else _existing_stems() - retry

    # Skip DOIs already processed in a prior run. classifications.csv is the
    # durable "already pulled & filtered" ledger. When a project is imported
    # with classifications but WITHOUT abstract files (e.g. the long_covid
    # corpus migrated from the legacy pull_long_covid_papers pipeline, whose
    # abstract .md files no longer exist on disk), this stops us re-fetching
    # ~100k abstracts we would never re-classify. DOIs are normalized to the
    # `--` stem form so the `/`-vs-`--` convention mismatch doesn't matter.
    classified: set[str] = set()
    if not args.redo and not args.no_skip_classified:
        cls_csv = DATA_DIR / "classifications.csv"
        if cls_csv.exists():
            with open(cls_csv, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = (row.get("doi") or "").strip().lower()
                    if d:
                        classified.add(doi_to_filename(d))
            if classified:
                print(f"  {len(classified):,} already classified -- will skip "
                      "(use --no-skip-classified to refetch their abstracts)",
                      file=sys.stderr, flush=True)

    todo = []
    skipped_no_id = 0
    skipped_classified = 0
    for row in rows:
        doi = (row.get("doi") or "").strip()
        pmid = (row.get("pmid") or "").strip()
        if not (doi or pmid):
            skipped_no_id += 1
            continue
        stem = doi_to_filename(doi) if doi else f"pmid_{pmid}"
        if stem in existing:
            continue
        if classified and stem.lower() in classified and stem not in retry:
            skipped_classified += 1
            continue
        todo.append((doi, pmid, stem))

    if skipped_no_id:
        print(f"  {skipped_no_id} rows have no DOI or PMID -- skipped",
              file=sys.stderr, flush=True)
    if existing:
        print(f"  {len(existing)} already fetched -- skipped (use --redo to refetch)",
              file=sys.stderr, flush=True)
    if skipped_classified:
        print(f"  {skipped_classified:,} already classified -- skipped",
              file=sys.stderr, flush=True)

    if args.limit > 0:
        todo = todo[:args.limit]

    if not todo:
        print("Nothing to fetch.", file=sys.stderr, flush=True)
        return

    n_retry = sum(1 for _, _, stem in todo if stem in retry)
    print(f"\nTo fetch: {len(todo)} papers"
          + (f" ({n_retry} retrying an earlier transient failure)" if n_retry else ""),
          file=sys.stderr, flush=True)
    print(f"Sources (in order): {sources}", file=sys.stderr, flush=True)

    # Phase 1: bulk PubMed fetch (cheap, single batched call path)
    pubmed_data = {}
    if "pubmed" in sources:
        print("\n=== Phase 1: PubMed bulk fetch ===", file=sys.stderr, flush=True)
        pmids = sorted({pmid for _, pmid, _ in todo if pmid})
        pubmed_data = fetch_pubmed_bulk(pmids)

    # Phase 2: per-paper fallback in thread pool
    print(f"\n=== Phase 2: Per-paper fetch (workers={args.workers}) ===",
          file=sys.stderr, flush=True)

    log_lock = threading.Lock()
    counts = {"pubmed": 0, "europepmc": 0, "crossref": 0, "openalex": 0, "none": 0}
    n_transient = 0
    log_exists = LOG_CSV.exists() and LOG_CSV.stat().st_size > 0
    start_time = time.monotonic()

    with open(LOG_CSV, "a", newline="", encoding="utf-8") as logf:
        log_writer = csv.DictWriter(logf, fieldnames=LOG_FIELDS)
        if not log_exists:
            log_writer.writeheader()
            logf.flush()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_fetch_with_status, doi, pmid, pubmed_data, sources):
                (doi, pmid, stem)
                for doi, pmid, stem in todo
            }
            try:
                for i, future in enumerate(as_completed(futures), 1):
                    doi, pmid, stem = futures[future]
                    try:
                        source, title, abstract, failure = future.result()
                    except Exception as exc:  # noqa: BLE001 — one paper, not the run
                        source, title, abstract, failure = "none", "", "", "error"
                        print(f"  ERROR {doi or pmid}: {type(exc).__name__}: {exc}",
                              file=sys.stderr, flush=True)

                    transient = failure == "error"
                    n_transient += transient
                    # A transient failure never overwrites a file that has text.
                    stub_ok = not (transient and (ABSTRACTS_DIR / f"{stem}_abstract.md").exists())
                    if abstract or stub_ok:
                        _write_abstract_file(stem, title, abstract, transient=transient)
                    _save_local_abstract(doi, pmid, source, title, abstract)
                    counts[source] = counts.get(source, 0) + 1

                    with log_lock:
                        log_writer.writerow({
                            "doi": doi,
                            "pmid": pmid,
                            "source": source,
                            "status": "ok" if abstract else failure,
                            "title": title[:200],
                            "chars": len(abstract),
                            "reason": ("" if abstract else
                                       "a source could not be reached"
                                       if transient else
                                       "no source has an abstract"),
                            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                        time.gmtime()),
                        })
                        logf.flush()

                    if i % 50 == 0:
                        elapsed = time.monotonic() - start_time
                        rate = i / elapsed if elapsed > 0 else 0
                        eta = (len(todo) - i) / rate if rate > 0 else 0
                        summary = ", ".join(
                            f"{k}={v}" for k, v in counts.items() if v
                        )
                        print(f"  [{i}/{len(todo)}] {rate:.1f}/s, "
                              f"eta {eta/60:.1f}m | {summary}",
                              file=sys.stderr, flush=True)
            except KeyboardInterrupt:
                print("\nInterrupted! Shutting down workers...",
                      file=sys.stderr, flush=True)
                pool.shutdown(wait=False, cancel_futures=True)

    # Drain any queued local write-backs before we exit.
    try:
        from ber_core import local_sciscinet

        written = local_sciscinet.flush()
        if written:
            print(f"\nWrote {written} record(s) back to the local SciSciNet DB",
                  file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass

    # Final summary
    print("\n=== Summary ===", file=sys.stderr, flush=True)
    total = sum(counts.values())
    for src in ("local", "pubmed", "europepmc", "crossref", "openalex", "none"):
        n = counts.get(src, 0)
        pct = (n / total * 100) if total else 0
        print(f"  {src:10s}: {n:6d}  ({pct:5.1f}%)",
              file=sys.stderr, flush=True)
    if n_transient:
        print(f"\n  {n_transient} paper(s) failed TRANSIENTLY (network/rate limit); "
              f"they are retried on the next run.", file=sys.stderr, flush=True)
    elapsed = time.monotonic() - start_time
    print(f"\nElapsed: {elapsed/60:.1f} min", file=sys.stderr, flush=True)
    print(f"Abstracts written to: {ABSTRACTS_DIR}", file=sys.stderr, flush=True)
    print(f"Log: {LOG_CSV}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch abstracts for the DOIs/PMIDs from step 1.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", required=True,
                        help="Path to the review's YAML config")
    parser.add_argument("--workers", type=int, default=8,
                        help="Thread pool size for per-paper fetches (default: 8)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap on papers to process this run (0 = all)")
    parser.add_argument("--sources",
                        default="local,pubmed,europepmc,crossref,openalex",
                        help="Comma-separated source order "
                             "(default: pubmed,europepmc,crossref,openalex)")
    parser.add_argument("--redo", action="store_true",
                        help="Refetch papers that already have abstract files")
    parser.add_argument("--no-skip-classified", action="store_true",
                        help="Do not skip DOIs already present in "
                             "classifications.csv (default: skip them, since "
                             "their abstracts are no longer needed)")
    args = parser.parse_args()
    main(args)
