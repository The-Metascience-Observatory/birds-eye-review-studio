"""Versioned evidence retrieval for INSPECT-SR. Failures never mean no notices."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import tempfile
import os
from urllib.parse import quote
from html.parser import HTMLParser

from ber_core.identity import paper_join_key

import requests

RW_URL = "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv"
PUBPEER_URL = "https://pubpeer.com/v3/publications"
PUBPEER_SOURCE = "https://github.com/PubPeerFoundation/PubPeerBrowserExtensions/blob/master/js/contentScript/pubpeer.js"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


def paper_key(value):
    """The shared paper join key (``ber_core.identity.paper_join_key``)."""
    return paper_join_key(value or "")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Different reports can retrieve the same registry concurrently. Unique
    # temporary files make each replacement atomic without racing a shared .tmp.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name, suffix=".tmp", delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            f.flush()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def fetch_json(cache, name, url, *, payload=None, offline=False, refresh=False):
    """Cache raw successful responses with their retrieval provenance."""
    path = Path(cache) / (name + ".json")
    if path.exists() and not refresh:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("status") == "ok" or offline:
            return saved
    # Never persist an API key into the evidence cache.
    evidence = {"url": re.sub(r"(devkey|api_key|key)=[^&]+", r"\1=REDACTED", url),
                "retrieved_at": now(), "status": "unavailable"}
    if offline:
        evidence["error"] = "Not present in offline cache"
        return evidence
    try:
        for attempt in range(4):
            if payload is None:
                r = requests.get(url, timeout=30, headers={"User-Agent": "BirdsEyeReview/1.0"})
            else:
                r = requests.post(url, json=payload, timeout=30)
            if r.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                break
            time.sleep(min(30, 2 ** (attempt + 1)))
        r.raise_for_status()
        evidence.update(status="ok", data=r.json(), sha256=digest(r.content))
    except (requests.RequestException, ValueError) as exc:
        evidence["error"] = type(exc).__name__
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            evidence["http_status"] = exc.response.status_code
    write_json(path, evidence)
    return evidence


def download_retractions(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(RW_URL, timeout=120)
    r.raise_for_status()
    content = r.content
    if not content.startswith(b"Record ID,") or b"OriginalPaperDOI" not in content[:1500]:
        raise ValueError("Retraction Watch response is not the expected CSV")
    tmp = path.with_suffix(".download")
    tmp.write_bytes(content)
    # Validate before replacing a working cache.
    RetractionIndex(tmp)
    tmp.replace(path)
    write_json(path.with_suffix(".source.json"), {
        "url": RW_URL, "retrieved_at": now(), "sha256": digest(content),
    })
    return path


def author_key(name):
    # Full names only. Initial-only matches are deliberately not treated as identity.
    words = re.findall(r"[^\W\d_]+", str(name).casefold(), flags=re.U)
    return " ".join(sorted(words)) if len(words) >= 2 and all(len(w) > 1 for w in words) else ""


class RetractionIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.by_doi, self.by_author, self.by_title = defaultdict(list), defaultdict(list), defaultdict(list)
        self.sha256 = digest(self.path.read_bytes())
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"OriginalPaperDOI", "RetractionNature", "Record ID"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("Retraction Watch CSV is missing required columns")
            self.count = 0
            for row in reader:
                self.count += 1
                for doi in row.get("OriginalPaperDOI", "").split(";"):
                    if paper_key(doi).startswith("10."):
                        self.by_doi[paper_key(doi)].append(row)
                title = re.sub(r"\W+", "", row.get("Title", "").casefold())
                if title:
                    self.by_title[title].append(row)
                for author in row.get("Author", "").split(";"):
                    key = author_key(author)
                    if key:
                        self.by_author[key].append(row)
        if not self.count:
            raise ValueError("Retraction Watch CSV is empty")

    def lookup(self, doi, title="", authors=()):
        matches = self.by_doi.get(paper_key(doi), [])
        title_matches = self.by_title.get(re.sub(r"\W+", "", title.casefold()), [])
        candidates = {}
        for author in authors:
            for row in self.by_author.get(author_key(author), []):
                if row not in matches:
                    candidates[row["Record ID"]] = row
        return {"status": "ok", "source": str(self.path), "source_sha256": self.sha256,
                "records_searched": self.count, "doi_matches": matches,
                "title_candidates": [r for r in title_matches if r not in matches],
                "author_candidates": list(candidates.values()),
                "authors_searched": list(authors),
                "limitation": "Author/title matches need identity confirmation; corresponding author coverage may be incomplete. No match does not rule out unindexed publisher notices."}


def pubpeer_lookup(ids, cache, *, offline=False, refresh=False):
    """Look up PubPeer discussions (the request/response of PubPeer's public API).

    Requires a PubPeer developer key in ``PUBPEER_DEVKEY``; without one every
    paper is recorded as ``unavailable`` rather than silently "no comments".
    """
    ids = sorted(set(paper_key(i) for i in ids))
    out = {}
    devkey = os.environ.get("PUBPEER_DEVKEY", "").strip()
    if not devkey:
        for pid in ids:
            out[pid] = {"status": "unavailable", "retrieved_at": now(), "url": PUBPEER_URL,
                        "api_source": PUBPEER_SOURCE, "feedbacks": [],
                        "limitation": "PubPeer lookup disabled: set PUBPEER_DEVKEY to enable it."}
        return out
    request_url = f"{PUBPEER_URL}?devkey={quote(devkey)}"
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        payload = {"version": "1.6.2", "browser": "Chrome", "urls": [],
                   "dois": [i for i in batch if i.startswith("10.")],
                   "pmids": [i[5:] for i in batch if i.startswith("pmid_")]}
        response = fetch_json(cache, "pubpeer-" + digest(payload)[:20], request_url,
                              payload=payload, offline=offline, refresh=refresh)
        data = response.get("data") or {}
        valid = response["status"] == "ok" and data.get("status") == "good" and isinstance(data.get("feedbacks"), list)
        feedbacks = data.get("feedbacks", []) if valid else []
        for pid in batch:
            # The extension returns the queried identifier in `id`, not a PubPeer slug.
            matches = [f for f in feedbacks if paper_key(f.get("id")) in {pid, pid.removeprefix("pmid_")}
                       or paper_key(f.get("doi")) == pid]
            out[pid] = {"status": "ok" if valid else "unavailable",
                        "retrieved_at": response["retrieved_at"], "url": PUBPEER_URL,
                        "api_source": PUBPEER_SOURCE, "feedbacks": matches,
                        "limitation": "Comment presence is not a trustworthiness verdict. Read the linked discussion and verify its evidence."}
    return out


def crossref_lookup(pid, cache, **options):
    if not pid.startswith("10."):
        return {"status": "not_applicable", "reason": "No DOI"}
    return fetch_json(cache, "crossref-" + digest(pid)[:20],
                      "https://api.crossref.org/works/" + quote(pid, safe=""), **options)


def retraction_notices(pid, matches, cache, **options):
    """Check notice identity using publisher-deposited Crossref metadata.

    A Retraction Watch-supplied relation is not an independent publisher check.
    Some publishers deposit formal retraction notices as generic errata; their
    explicit notice title plus publisher-supplied original-DOI link identifies it.
    """
    notices = []
    for row in matches:
        doi = paper_key(row.get("RetractionDOI"))
        if row.get("RetractionNature", "").casefold() != "retraction" or not doi.startswith("10."):
            continue
        response = crossref_lookup(doi, cache, **options)
        m = (response.get("data") or {}).get("message") or {}
        title = " ".join(m.get("title") or [])
        updates = m.get("update-to") or []
        linked = any(paper_key(u.get("DOI")) == paper_key(pid) and u.get("source") == "publisher" for u in updates)
        confirmed = response.get("status") == "ok" and paper_key(m.get("DOI")) == doi and linked and bool(re.match(r"retraction\b|retracted\b", title, re.I))
        notices.append({"notice_doi": doi, "original_doi": pid, "title": title,
                        "status": "publisher_metadata_confirmed" if confirmed else "requires_notice_verification",
                        "url": "https://doi.org/" + doi, "retrieved_at": response.get("retrieved_at"),
                        "update_to": updates, "published": m.get("published"),
                        "verification_scope": "Publication status and linked report only; notice reasons and underlying allegations require substantive review."})
    return notices


def registration_lookup(nct, cache, **options):
    if not re.fullmatch(r"NCT\d{8}", nct):
        raise ValueError("Invalid NCT ID")
    result = fetch_json(cache, "registry-" + nct,
                        "https://clinicaltrials.gov/api/v2/studies/" + nct, **options)
    return {**result, "history_status": "not_retrieved",
            "limitation": "Current registry snapshot only; original and prerecruitment versions require review of record history."}


def pubpeer_discussion(feedback, cache, *, offline=False, refresh=False):
    """Read the public discussion embedded in the page linked by the extension.

    Read-only: no authentication, challenge bypass, posting or correspondence.
    """
    url = feedback.get("url", "")
    if not re.fullmatch(r"https://(?:www\.)?pubpeer\.com/publications/[A-Fa-f0-9]+", url):
        return {"status": "unavailable", "reason": "Not a recognized public PubPeer publication URL"}
    path = Path(cache) / ("pubpeer-discussion-" + digest(url)[:20] + ".json")
    if path.exists() and not refresh:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("status") == "ok" or offline:
            return saved
    result = {"status": "unavailable", "url": url, "retrieved_at": now(),
              "limitation": "Public commentary, not independently verified facts or an integrity verdict."}
    if offline:
        result["reason"] = "Not in offline cache"
        return result

    class CommentsParser(HTMLParser):
        comments = None

        def handle_starttag(self, tag, attrs):
            if tag == "comment-timeline":
                raw = dict(attrs).get(":data-comments")
                if raw:
                    self.comments = json.loads(raw)
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        parser = CommentsParser(convert_charrefs=True)
        parser.feed(r.text)
        if not isinstance(parser.comments, list):
            raise ValueError("No readable public discussion; browser review required")
        result.update(status="ok", comments=parser.comments, sha256=digest(r.content))
    except (requests.RequestException, ValueError) as exc:
        result["error"] = type(exc).__name__
    write_json(path, result)
    return result
