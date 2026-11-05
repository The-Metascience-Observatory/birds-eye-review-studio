"""The live evidence table: one row per paper, enriched progressively.

The centerpiece of the papertrace-style wizard. A review's data lands in stages
— search finds DOIs, screening adds relevance + biblio, pass-1 adds eligibility,
pass-2 adds the full extraction — and each write appends to files on disk. This
module folds all of them into one row-per-paper table keyed on the normalized
DOI, so the studio can show results filling in *while the jobs that produce them
are still running*.

THE MERGE PRECEDENCE. Each artifact is a tier; later tiers overwrite earlier
fields, but a blank value NEVER erases a filled one (a half-written later tier
must not blank out a column an earlier tier populated). The tiers, low→high:

    1  search_dois.csv           doi, pmid
    1.2  search_raw.csv          title (search-time; lowest-priority title)
    2  relevance.csv             relevance, confidence
    3  classifications.csv       article_type
    4  trial_screening.csv       title/authors/journal/year, trial_type,
                                 is_excluded, exclusion_reason
    5  trial_extractions.jsonl   eligibility (pass 1)
    6  trial_extractions_pass2.jsonl (+ observational)   THE FULL ROW, via
       dashboard_summary.project_row — the SAME projection the published
       dashboard uses, so the live table and the final dashboard can never
       disagree about a study — plus a composed PICO block.
    +  converted/<stem>/ presence -> has_fulltext

Everything is read directly from the workspace, ungated: unlike the dashboard
feed (which only exists after the bundle build), the evidence table must work
mid-pipeline, so it never touches the bundle machinery.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path


from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()

# compose_pico lives in the projection module so dashboard rows and evidence
# rows share ONE PICO composition; re-exported here for existing callers/tests.
from ber_core.dashboard_summary import compose_pico, project_row  # noqa: E402

__all__ = ["build_evidence", "compose_pico", "project_row"]
from ber_core.utils import doi_to_filename, norm_doi, norm_paper_id  # noqa: E402
from ber_core.evidence import load_exclusion_dois, load_inclusion_dois  # noqa: E402
from ber_core.conversion import TABLE_BEARING_MODES, read_conversion_quality  # noqa: E402


def _data_dir(workspace: Path) -> Path | None:
    """The review's `projects/<slug>/data/` dir, or None if not set up yet."""
    from . import pipeline_bridge

    try:
        return pipeline_bridge.build_project(workspace).data_dir
    except Exception:  # noqa: BLE001 — a half-created workspace has no config
        return None


def _include_animal_studies(workspace: Path) -> bool:
    """The review's animal policy from config (default False = human-only).

    The same key `extract_pass1` reads to gate eligibility, so the live table
    and the pipeline verdict agree on which papers the animal gate drops."""
    from . import pipeline_bridge

    try:
        cfg = pipeline_bridge.build_project(workspace).config
    except Exception:  # noqa: BLE001 — half-created workspace
        return False
    return bool((cfg or {}).get("include_animal_studies", False))


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _read_jsonl(path: Path, *, ok_only: bool = False) -> list[dict]:
    """Stream a JSONL file line by line, tolerant of a run appending to it.

    A record still being written lands as one flushed line (extract_pass2
    flushes per record), so a partial file yields complete records, never a
    torn one. A malformed final line (mid-write) is skipped.
    """
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a torn final line during an active write
            if ok_only and rec.get("_status") not in (None, "ok"):
                continue
            out.append(rec)
    return out


# --- build_evidence cache ------------------------------------------------------
# The studio polls the evidence table every few seconds and the reconciler
# rebuilds it every 15s per active review; each build re-reads every artifact
# and scans the whole paper store. Rebuilding only when an input changed turns
# that into a stat() per input file.
#
# The key is every tier file's (mtime, size), the config and verdict files, a
# digest of the DB metadata passed in, and the NAMES of everything in the paper
# store down to the files in each paper folder (has_fulltext is the presence of
# body.md / XML / a failure marker). A coarse clock bucket is the backstop for
# the one change none of those see: a file rewritten IN PLACE inside a paper
# folder (a re-conversion's provenance.json).
_STORE_TTL_S = 30.0
_CACHE_MAX = 32
_cache: "OrderedDict[str, tuple[tuple, dict]]" = OrderedDict()
_cache_lock = threading.Lock()

_TIER_FILES = (
    "search_dois.csv", "search_raw.csv", "relevance.csv", "classifications.csv",
    "trial_screening.csv", "trial_extractions.jsonl",
    "trial_extractions_pass2.jsonl", "observational_extractions.jsonl",
    "user_exclusions.json", "user_inclusions.json",
)


# Below this size a file's CONTENT is part of the key: filesystem timestamps can
# be coarser than two quick same-size rewrites (a toggled verdict file), and
# reading a small file costs less than being wrong.
_HASH_BELOW = 256 * 1024


def _stat_sig(path: Path) -> tuple | None:
    try:
        st = path.stat()
        if st.st_size < _HASH_BELOW:
            return (st.st_size, hashlib.sha1(path.read_bytes()).hexdigest())
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _metadata_digest(metadata: dict | None) -> str:
    if not metadata:
        return ""
    blob = json.dumps(metadata, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def _store_signature(workspace: Path) -> str:
    """Digest of the entry names in the paper store (``<root>/<Type>/<stem>/*``)
    and the legacy ``converted/<stem>/*``: directory listings only — no
    stat, no file reads."""
    from . import pipeline_bridge

    try:
        proj = pipeline_bridge.build_project(workspace)
        roots = [pipeline_bridge.papers_root(proj), proj.project_dir / "converted"]
    except Exception:  # noqa: BLE001 — half-created workspace
        return ""
    h = hashlib.sha1()

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            h.update(f"{e.path};".encode())
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir and depth > 1:
                walk(Path(e.path), depth - 1)

    for root in roots:
        walk(root, 3)
    return h.hexdigest()


def _cache_key(workspace: Path, data: Path, metadata: dict | None) -> tuple:
    return (
        _stat_sig(workspace / "config.yaml"),
        tuple(_stat_sig(data / name) for name in _TIER_FILES),
        _metadata_digest(metadata),
        _store_signature(workspace),
        int(time.monotonic() // _STORE_TTL_S),
    )


def _copy_result(result: dict) -> dict:
    """A copy safe to hand out: callers may annotate rows; the cache must not
    see it."""
    return {
        "rows": [dict(r) for r in result["rows"]],
        "stage_reached": result["stage_reached"],
        "counts": dict(result["counts"]),
    }


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def build_evidence(workspace: Path, *, metadata: dict | None = None) -> dict:
    """(rows, stage_reached, counts) for the whole review, whatever exists so far.

    ``metadata`` is the per-DOI biblio from the ``paper_metadata`` table (the
    background enrichment worker's output). It is merged as an EARLY tier — right
    after search — so titles/authors appear for every found paper long before
    screening narrows the corpus and long before pass-2 extraction.

    Cached per workspace (see ``_cache_key``); the result is a fresh copy.
    """
    data = _data_dir(workspace)
    if data is None:
        return {"rows": [], "stage_reached": "setup", "counts": {}}
    ckey = str(workspace)
    key = _cache_key(workspace, data, metadata)
    with _cache_lock:
        hit = _cache.get(ckey)
        if hit is not None and hit[0] == key:
            _cache.move_to_end(ckey)
            return _copy_result(hit[1])
    result = _build_evidence(workspace, data, metadata)
    with _cache_lock:
        _cache[ckey] = (key, result)
        _cache.move_to_end(ckey)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return _copy_result(result)


def _build_evidence(workspace: Path, data: Path, metadata: dict | None) -> dict:
    """The uncached merge behind :func:`build_evidence`."""
    if data is None:
        return {"rows": [], "stage_reached": "setup", "counts": {}}

    # Review animal policy: human-only by default. When false, non-human papers
    # read as screened_out in the table (matching the pass-1 eligibility gate),
    # unless the user force-includes them. `_screen_state` consults this.
    include_animals = _include_animal_studies(workspace)

    by_doi: dict[str, dict] = {}
    reached = "setup"

    def merge(doi: str, patch: dict) -> None:
        key = norm_doi(doi)
        if not key:
            return
        row = by_doi.setdefault(key, {"doi": key})
        for k, v in patch.items():
            # A blank later value never erases a filled earlier one.
            if v not in (None, "", [], {}):
                row[k] = v

    # Tier 1 — search DOIs.
    for r in _read_csv(data / "search_dois.csv"):
        merge(r.get("doi", ""), {"pmid": r.get("pmid")})
        reached = "search"

    # Tier 1.2 — the search-time title from `search_raw.csv` (its `title` column;
    # `search_dois.csv` carries only doi/pmid). This is the LOWEST-priority title:
    # a real enrichment/screening title in a later tier overwrites it. It exists
    # so a paper whose metadata enrichment found nothing still shows the title the
    # search already captured, instead of a bare DOI and a row of dashes.
    for r in _read_csv(data / "search_raw.csv"):
        merge(r.get("doi", ""), {"title": r.get("title")})

    # Tier 1.5 — background metadata enrichment (from the paper_metadata table).
    # Runs in parallel with everything after search, so biblio fills in early.
    for doi, m in (metadata or {}).items():
        merge(doi, {
            "title": m.get("title"), "authors": m.get("authors"),
            "year": m.get("year"), "journal": m.get("journal"),
            "pmid": m.get("pmid"), "url": m.get("url"),
            "metadata_source": m.get("metadata_source"),
        })

    # Tier 2 — abstract relevance. Legacy corpora have a separate relevance.csv
    # (the old split relevance/type steps); the merged `quick` screen folds these
    # columns into classifications.csv, so Tier 3 also carries them now.
    for r in _read_csv(data / "relevance.csv"):
        merge(r.get("doi", ""), {
            "relevance": r.get("is_relevant"),
            "relevance_confidence": r.get("confidence"),
        })

    # Tier 3 — the merged abstract screen: article type + relevance + the
    # keep/drop decision and certainty (quick writes all of these here). Its
    # `is_excluded` IS a real exclusion — the merged screen drops off-topic and
    # wrong-type papers here — so it feeds the row's is_excluded, keeping the
    # evidence table's included set in step with PRISMA (which reads the same
    # column). A relevant, kept paper leaves is_excluded false.
    for r in _read_csv(data / "classifications.csv"):
        excluded = (r.get("is_excluded") or "").strip().lower() == "yes"
        patch = {
            "article_type": r.get("article_type"),
            "relevance": r.get("is_relevant"),
            "relevance_confidence": r.get("relevance_confidence"),
            "screen_excluded": r.get("is_excluded"),
            "screen_certainty": r.get("certainty"),
            # Abstract-level organism guess; the pass-1 screen (Tier 5) overrides it.
            "organism": r.get("organism"),
        }
        if excluded:
            patch["is_excluded"] = True
            # Blank reason on an off-topic paper is the relevance exclusion.
            patch["exclusion_reason"] = (
                r.get("exclusion_reason")
                or ("Not relevant to the review topic"
                    if (r.get("is_relevant") or "").strip().lower() != "yes"
                    else "Excluded at screening")
            )
        merge(r.get("doi", ""), patch)
        # The merged screen IS the screening step (classifications.csv carries
        # relevance + keep/drop), so reaching it advances the stage — the legacy
        # split flow only got here via trial_screening.csv below.
        if "is_relevant" in r:
            reached = "screen"

    # Tier 4 — full-text screening: biblio + the exclusion columns.
    for r in _read_csv(data / "trial_screening.csv"):
        merge(r.get("doi", ""), {
            "title": r.get("paper_title"),
            "authors": r.get("paper_authors"),
            "journal": r.get("paper_journal"),
            "year": r.get("paper_year"),
            "trial_type": r.get("trial_type"),
            "is_excluded": (r.get("is_excluded") or "").strip().lower() == "yes",
            "exclusion_reason": r.get("exclusion_reason"),
        })
        reached = "screen"

    # Tier 5 — pass-1 eligibility.
    for rec in _read_jsonl(data / "trial_extractions.jsonl"):
        scr = rec.get("screening") or {}
        # The full-text organism verdict overrides the abstract guess (Tier 3):
        # it read the methods. Shape is {verdict, reasoning}; take the verdict.
        organism = (scr.get("organism") or {})
        organism_verdict = organism.get("verdict") if isinstance(organism, dict) else None
        patch = {
            "eligible": bool(scr.get("eligible_for_pass2")),
            "is_protocol": bool(
                (scr.get("is_protocol_not_article") or {}).get("verdict")),
            "organism": organism_verdict,
        }
        # A full-text ELIGIBLE verdict clears an ABSTRACT-screen exclusion, for
        # the same reason the organism verdict above wins: pass 1 read the paper,
        # the abstract screen only read the abstract. Papers whose abstract reads
        # off-topic but whose methods show a real controlled trial were otherwise
        # extracted and published in the dashboard while the evidence table still
        # showed them "excluded" — the two disagreed by exactly these papers.
        #
        # Only the AUTOMATIC screening flag is cleared. A manual exclusion is
        # applied later (Tier 7) and still wins, which is the intended precedence:
        # the user's call beats every automatic verdict.
        if patch["eligible"]:
            patch["is_excluded"] = False
        merge(rec.get("paper_id", ""), patch)
        # `merge` deliberately ignores blank values, so the now-stale exclusion
        # reason ("Not relevant to the review topic") has to be cleared directly
        # or it would keep showing next to a row the full text just cleared.
        if patch["eligible"]:
            cleared = by_doi.get(norm_doi(rec.get("paper_id", "")))
            if cleared is not None:
                cleared.pop("exclusion_reason", None)
                cleared.pop("screen_excluded", None)

    # Tier 6 — full extraction, via the SAME projection the dashboard uses.
    for path in ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl"):
        for rec in _read_jsonl(data / path, ok_only=True):
            # project_row now carries `pico` + `follow_up_weeks` itself — the
            # evidence row is the projection plus the extracted flag.
            row = project_row(rec, csv_verdicts={}, domain_to_type={})
            row["extracted"] = True
            merge(rec.get("paper_id", ""), row)
            reached = "extract"

    # `has_fulltext`: a converted paper exists on disk for this DOI.
    _mark_fulltext(workspace, by_doi)

    # Tier 7 — the user's own manual exclusions (`user_exclusions.json`), applied
    # LAST so a hand toggle overrides the pipeline's screening verdict in both
    # directions: a DOI in the file is excluded even if screening kept it, and a
    # DOI absent from the file is NOT excluded here (the row keeps whatever the
    # pipeline decided). This makes the evidence table and PRISMA funnel — which
    # reads the same file — agree the instant the user toggles, and survive a
    # refetch. Same file the export + meta steps honor, so it propagates end to
    # end.
    user_excl = _load_user_exclusions(data)
    for key, row in by_doi.items():
        if key in user_excl:
            row["is_excluded"] = True
            row["user_excluded"] = True
            # Keep any richer pipeline reason; else label it a manual exclusion.
            if not row.get("exclusion_reason"):
                row["exclusion_reason"] = user_excl[key] or "Manually excluded"

    # Tier 8 — the user's own force-INCLUDES (`user_inclusions.json`). A manual
    # include overrides the automatic gate: the paper is included even if the
    # full-text eligibility screen said ineligible or never ran. Exclusion wins
    # over inclusion (the endpoints keep the two files disjoint, but guard anyway).
    user_incl = _load_user_inclusions(data)
    for key, row in by_doi.items():
        if key in user_incl and not row.get("is_excluded"):
            row["user_included"] = True

    # Derive one authoritative `screen_state` (and the back-compat `included`
    # bool) so the table's pill/tint, the default sort, and the PRISMA counts all
    # mean the SAME thing. See `_screen_state`.
    for row in by_doi.values():
        state = _screen_state(row, include_animals=include_animals)
        row["screen_state"] = state
        row["included"] = state == "included"

    rows = list(by_doi.values())
    counts = {
        "total": len(rows),
        "included": sum(1 for r in rows if r.get("screen_state") == "included"),
        "pending": sum(1 for r in rows if r.get("screen_state") == "pending"),
        "extracted": sum(1 for r in rows if r.get("extracted")),
        "excluded": sum(1 for r in rows if r.get("is_excluded")),
        "with_fulltext": sum(1 for r in rows if r.get("has_fulltext")),
    }
    return {"rows": rows, "stage_reached": reached, "counts": counts}


def _screen_state(row: dict, *, include_animals: bool = True) -> str:
    """The paper's authoritative screening state, one of:

    - ``"excluded"``  — excluded by the pipeline or by a manual exclude.
    - ``"included"``  — going to extraction: manually force-included, OR the
      full-text eligibility screen passed it (``eligible=True``), OR — only when
      that screen has NOT ruled — it was already extracted or passed relevance.
    - ``"pending"``   — screened IN on the abstract (relevance=yes, not excluded)
      but the full-text eligibility screen has NOT decided yet (no pass-1 record:
      the ``eligible`` key is absent). The verdict is still coming.
    - ``"screened_out"`` — did not pass relevance / organism, and isn't otherwise
      included.

    Presence-vs-absence of ``eligible`` is load-bearing: pass-1 sets it to a
    strict bool only when a record exists, so ``"eligible" in row`` means "the
    full-text screen ran for this paper", ``eligible is False`` means "ran, said
    ineligible", and absent means "hasn't run yet".

    ``include_animals`` is the review's animal policy. When False (the default,
    human-only), a paper whose organism is known to be non-human is screened_out
    up front — matching the pass-1 eligibility gate — so the default-exclude shows
    live even before the full-text screen runs. A force-include still wins. An
    unknown organism does NOT get gated (innocent until the screen decides).

    Precedence, highest first: manual exclude → manual include → animal gate →
    the pass-1 verdict → extracted → relevance. Only a MANUAL include overrides
    the pass-1 verdict; ``extracted`` sits below it and applies only when pass 1
    has not ruled (see the comment at the verdict branch).
    """
    if row.get("is_excluded"):
        return "excluded"
    if row.get("user_included"):
        return "included"

    # Animal gate (human-only reviews): a known non-human organism is screened out.
    if not include_animals:
        organism = str(row.get("organism") or "").strip().lower()
        if organism in ("mouse", "other_animal", "other"):
            return "screened_out"

    relevant = str(row.get("relevance") or "").strip().lower() == "yes"
    if "eligible" in row:
        # The full-text screen has ruled on this paper. Its verdict OUTRANKS the
        # mere presence of a pass-2 record: extraction is downstream of the
        # verdict, so an extracted-but-ineligible paper means something extracted
        # the wrong set, not that the paper became eligible. Ranking `extracted`
        # above this let a mis-targeted pass-2 run launder ineligible papers into
        # the included set, PRISMA counts, and the meta-analysis pool.
        return "included" if row.get("eligible") else "screened_out"
    # No full-text verdict yet. An existing extraction still implies inclusion —
    # reviews extracted before the eligibility pass existed have no `eligible`
    # key at all, and their rows must stay green.
    if row.get("extracted"):
        return "included"
    if relevant:
        # Screened in on the abstract, awaiting the full-text eligibility pass.
        return "pending"
    return "screened_out"


# Conversion modes whose text carries table structure. Anything else (notably
# `grobid-only`) yields text with NO tables: GROBID's tables land in an object
# the folder renderer never writes, so table-only outcomes are invisible to
# extraction. ONE shared definition (ber_core.conversion) — the copies that
# used to live here, in convert_pdfs.py and in the repair tool drifted.
# Note `docling-html` bodies embed tables as literal <table> markup, NOT pipe
# rows, so the row count below has to look for both.
_TABLE_BEARING_MODES = TABLE_BEARING_MODES


def _conversion_quality(paper_dir: Path) -> tuple[str | None, bool | None]:
    """(conversion_mode, tables_captured) for one converted paper directory.

    `provenance.json` is the only durable record of how a paper was converted,
    and nothing else in the system reads it — which is exactly how a corpus-wide
    table-free conversion went unnoticed. `tables_captured` is measured from the
    text itself (markdown pipe rows) rather than trusted from the mode, because
    even a table-bearing mode can come up empty on a paper with no tables.
    """
    prov = paper_dir / "provenance.json"
    if not prov.exists():
        return None, None
    quality = read_conversion_quality(paper_dir)
    mode = quality["mode"]
    if mode is None:
        return None, None
    if mode not in _TABLE_BEARING_MODES:
        # Structurally table-free — no need to read the body.
        return mode, False
    # The converter's own count, when it recorded one (html_convert writes
    # `tables_captured`), answers without opening the body at all.
    recorded = quality["tables_captured"]
    if isinstance(recorded, int) and not isinstance(recorded, bool):
        return mode, recorded > 0
    # Otherwise stream the body and stop at the FIRST evidence of a table: a
    # body.md can be megabytes, and this runs for every paper on every build.
    # Two representations: markdown pipe tables (PDF conversions) and embedded
    # HTML tables (html_convert). A pipe table needs header + delimiter + body
    # to be real, hence >3; one <table> element is already a table.
    body = paper_dir / "body.md"
    pipe_rows = 0
    try:
        with body.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "<table" in line.lower():
                    return mode, True
                if line.lstrip().startswith("|"):
                    pipe_rows += 1
                    if pipe_rows > 3:
                        return mode, True
    except OSError:
        return mode, None
    return mode, False


def _structured_fulltext(paper_dir: Path) -> tuple[str | None, int]:
    """(structured_source, n_supplementary_files) for one converted paper.

    Written by `install_structured.py` into the same `provenance.json` the
    conversion mode lives in, under its own `structured_*` keys — deliberately
    NOT as an `extraction_mode` value, since a mode the table gates don't
    recognize reads as degraded.

    Read separately from `_conversion_quality` rather than folded into it: that
    function's 2-tuple is pinned by tests, and the file is a few hundred bytes.
    """
    prov = paper_dir / "provenance.json"
    if not prov.exists():
        return None, 0
    try:
        data = json.loads(prov.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, 0
    if not isinstance(data, dict):
        return None, 0
    source = data.get("structured_source")
    count = data.get("supplementary_files")
    return (source if isinstance(source, str) else None,
            count if isinstance(count, int) else 0)


def _mark_fulltext(workspace: Path, by_doi: dict[str, dict]) -> None:
    """Set has_fulltext + conversion quality from each paper's folder.

    Directory existence alone used to be the whole signal, so a table-free
    `grobid-only` paper and a fully merged one both showed a green check.
    """
    from . import pipeline_bridge

    try:
        proj = pipeline_bridge.build_project(workspace)
    except Exception:  # noqa: BLE001
        return
    # One scan of the store, keyed by normalized DOI — type-folder agnostic, and
    # falling back to the legacy converted/ layout for un-migrated reviews.
    dirs = pipeline_bridge.paper_index(proj.project_dir, root=pipeline_bridge.papers_root(proj))
    if not dirs:
        return
    for key, row in by_doi.items():
        paper_dir = dirs.get(key)
        # Folder existence is NOT the signal any more: the fetch creates one the
        # moment a PDF lands, so it would flip has_fulltext true before anything
        # was converted. Full text means extracted text (body.md), publisher
        # markup, or a conversion that ran and failed — the last one included on
        # purpose, so the dock's self-healing doesn't re-fetch and re-fail that
        # paper in a loop.
        row["has_fulltext"] = paper_dir is not None and (
            (paper_dir / "body.md").exists()
            or any(paper_dir.glob("*.xml"))
            or any(paper_dir.glob("*.fulltext.html"))
            or (paper_dir / "conversion_failed.marker").exists())
        if paper_dir is None:
            continue
        mode, tables = _conversion_quality(paper_dir)
        quality = read_conversion_quality(paper_dir)
        structured, n_supplements = _structured_fulltext(paper_dir)
        # Assigned directly, not merged: merge() drops falsy values, and `False`
        # is the meaningful state for all three of these.
        row["has_xml"] = structured == "xml"
        row["has_supplement"] = n_supplements > 0
        if structured is not None:
            row["structured_source"] = structured
        if mode is not None:
            row["conversion_mode"] = mode
            # Source availability does not establish which source extraction
            # will select. Stub/garbled XML can fall back to this PDF body.
            row["conversion_degraded"] = quality["degraded"]
        row["conversion_status"] = quality["status"]
        row["conversion_requires_review"] = quality["requires_review"]
        # Converter exceptions can contain local paths or service URLs. Keep
        # diagnostics in provenance; the public evidence row carries a safe
        # summary, including in local-mode responses.
        row["conversion_warnings"] = (
            ["The converter reported incomplete output or quality warnings. Check the source tables."]
            if quality["warnings"] else [])
        if tables is not None:
            row["tables_captured"] = tables


def _strip_abstract_header(text: str) -> str:
    """Drop the leading `# <title>.` header the abstract files begin with.

    `fetch_abstracts.py` writes each file as `# {title}\n\n{body}`. The detail
    panel already shows the title prominently, so the repeated header is noise —
    remove it (and the blank line after it) so only the abstract body remains.
    """
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def paper_abstract(workspace: Path, doi: str) -> str | None:
    """The abstract text for one paper, read on demand from `abstracts/`.

    Abstracts live as `<escaped-doi>_abstract.md` files — too large to fold into
    every evidence row, so the detail panel fetches one when a paper is opened.
    The leading `# title` header is stripped (the panel shows the title itself).
    Returns None when there's no abstract file for this DOI.
    """
    from . import pipeline_bridge

    want = norm_doi(doi)
    if not want:
        return None
    try:
        proj = pipeline_bridge.build_project(workspace)
    except Exception:  # noqa: BLE001 — a half-created workspace has no config
        return None
    abstracts = proj.project_dir / "abstracts"
    if not abstracts.is_dir():
        return None
    path = _abstract_path(abstracts, doi, want)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = _strip_abstract_header(text)
    return text or None


# dir -> (dir mtime, {normalized doi: file}) — rebuilt only when files are
# added to or removed from abstracts/.
_abstract_index: dict[str, tuple[int, dict[str, Path]]] = {}
_abstract_lock = threading.Lock()


def _abstract_path(abstracts: Path, raw_doi: str, want: str) -> Path | None:
    """The `<escaped-doi>_abstract.md` file for one DOI.

    Looked up BY NAME — `fetch_abstracts.py` names each file
    `doi_to_filename(doi) + "_abstract.md"` — rather than by listing the
    directory, which on a 10k-paper review meant 10k directory entries per
    detail-panel click. The DOI's case as written may differ from the
    normalized form, so both spellings are tried; only a miss on both falls
    back to a (cached) normalized index of the directory.
    """
    for candidate in {doi_to_filename((raw_doi or "").strip()), doi_to_filename(want)}:
        if not candidate or "/" in candidate or candidate.startswith("."):
            continue
        p = abstracts / f"{candidate}_abstract.md"
        if p.is_file():
            return p
    try:
        mtime = abstracts.stat().st_mtime_ns
    except OSError:
        return None
    key = str(abstracts)
    with _abstract_lock:
        cached = _abstract_index.get(key)
        if cached is None or cached[0] != mtime:
            index: dict[str, Path] = {}
            for p in abstracts.iterdir():
                if p.name.endswith("_abstract.md"):
                    index.setdefault(norm_paper_id(p.name[: -len("_abstract.md")]), p)
            _abstract_index[key] = cached = (mtime, index)
    return cached[1].get(want)


def paper_extraction(workspace: Path, doi: str) -> list[dict]:
    """All ok pass-2 extraction records for one paper (usually exactly one).

    The full record — every outcome with per-arm results and effects, the
    verbatim ``*_quote`` fields, adverse events, demographics, ``_provenance`` —
    is far too large to fold into every evidence row, so the detail panel
    fetches it on demand, like the abstract. Multi-intervention papers can
    carry ``paper_id`` suffixes (``doi#2``), hence a list. Matching uses the
    same ``norm_paper_id`` keying as ``build_evidence``; quarantined/failed
    records are deliberately excluded (``ok_only``).
    """
    want = norm_doi(doi)
    if not want:
        return []
    data = _data_dir(workspace)
    if data is None:
        return []
    out: list[dict] = []
    for name in ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl"):
        for rec in _read_jsonl(data / name, ok_only=True):
            if norm_paper_id(rec.get("paper_id", "")) == want:
                out.append(rec)
    return out


def paper_pdf_path(workspace: Path, doi: str) -> Path | None:
    """The on-disk PDF for one paper, or None if it was never downloaded.

    The download step (`download_pdfs.py`) writes the PDF into the paper's own
    folder in the store (`papers/<Type>/<stem>/`), so the normal case is one
    index lookup. Legacy layouts — a flat `pdfs/` dump, `converted/<stem>/`, the
    `eligible_*` buckets — are still searched for reviews that have not been
    migrated. Returns the first existing path.
    """
    from . import pipeline_bridge

    want = norm_doi(doi)
    if not want:
        return None
    try:
        proj = pipeline_bridge.build_project(workspace)
    except Exception:  # noqa: BLE001 — a half-created workspace has no config
        return None

    stem = doi_to_filename(want)
    if not stem:
        return None
    fname = f"{stem}.pdf"

    # 1) The store. The folder's own name is authoritative — it may carry
    # fetchpdf's encoding rather than ours — so glob rather than assume fname.
    paper_dir = pipeline_bridge.paper_index(proj.project_dir, root=pipeline_bridge.papers_root(proj)).get(want)
    if paper_dir is not None:
        exact = paper_dir / fname
        if exact.is_file():
            return exact
        for p in sorted(paper_dir.glob("*.pdf")):
            if p.is_file():
                return p

    # 2) Legacy: a flat pdfs/ dump, plus the staged copies under converted/ and
    # eligible_*/ (recursive) that the old convert step created.
    pdfs_dir = proj.base_pdf_dir or (proj.project_dir / "pdfs")
    candidate = pdfs_dir / fname
    if candidate.is_file():
        return candidate

    for sub in ("converted", *[p.name for p in proj.project_dir.glob("eligible_*")]):
        base = proj.project_dir / sub
        if not base.is_dir():
            continue
        for p in base.rglob(fname):
            if p.is_file():
                return p
    return None


def save_uploaded_pdf(workspace: Path, doi: str, data: bytes) -> Path:
    """Write a hand-uploaded PDF into the paper's own folder in the store, under
    the canonical `<stem>.pdf` name — exactly where `download_pdfs.py` would
    have put it, so the convert step (and `paper_pdf_path` / `_mark_fulltext`)
    pick it up with no special-casing. Overwrites any prior file for this DOI.

    The paper's existing folder is reused when it has one, so an upload for a
    screened paper lands beside its other artifacts rather than starting a
    second folder in a different bucket.

    Raises ValueError on a bad DOI or a non-PDF payload (checked by the leading
    `%PDF` magic bytes — a wrong upload must not masquerade as full text)."""
    from . import pipeline_bridge

    want = norm_doi(doi)
    if not want:
        raise ValueError("A valid DOI is required to file the PDF.")
    stem = doi_to_filename(want)
    if not stem:
        raise ValueError("Could not derive a filename from the DOI.")
    if not data[:5].startswith(b"%PDF"):
        raise ValueError("That file is not a PDF (missing the %PDF header).")

    proj = pipeline_bridge.build_project(workspace)
    paper_dir = pipeline_bridge.ensure_paper_dir(proj, want)
    dest = paper_dir / f"{stem}.pdf"
    dest.write_bytes(data)
    return dest


def _abbrev_counts(authors: str | None) -> tuple[int, int]:
    """(abbreviated_authors, total_authors) in a ``"; "``-joined author string.

    Order-robust: an author is "abbreviated" if ANY of its whitespace tokens is a
    bare initial ("J", "J.", "JK", "AH"). This catches BOTH the "First Last" form
    ("J. Smith") and the "Family Initials" form the stored metadata actually uses
    ("Mortara A.", "He JK") — which ``metadata_fetcher._authors_have_abbreviations``
    misses because it only inspects the leading token. ``_is_initial_token`` (the
    one order-agnostic primitive) treats real short surnames like "Li"/"He" as
    NOT initials, so those aren't false-flagged.
    """
    try:
        from ber_core.metadata_fetcher import _is_initial_token  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return (0, 0)
    names = [n.strip() for n in (authors or "").split(";") if n.strip()]
    abbreviated = 0
    for name in names:
        tokens = [t for t in name.replace(",", " ").split() if t]
        if any(_is_initial_token(t) for t in tokens):
            abbreviated += 1
    return abbreviated, len(names)


def paper_authors_full(
    workspace: Path, doi: str, *, metadata: dict | None = None
) -> dict:
    """Best available author names for one paper, expanding initials on demand.

    The evidence row already resolves the highest-tier author string (full names
    from ``trial_screening.csv`` win over the initials in ``paper_metadata``). If
    that best string still has abbreviated names — the papers that never reached
    the full-text screen carry the "Family Initials" form ("Mortara A.") — fetch
    the full names from the DOI via the same multi-API fetcher the enrichment
    worker uses (it builds "First Last" from Crossref given/family), and keep them
    only if they are genuinely fuller. Returns ``{"authors", "expanded"}``.

    Network-free unless expansion is actually needed, and never raises: a missing
    ``requests`` (lean venv) or a failed lookup just returns the stored value.
    """
    want = norm_doi(doi)
    if not want:
        return {"authors": None, "expanded": False}

    # The cached table (see build_evidence): the detail panel is opened right
    # after the table loaded, so this is a hit, not a rebuild of every tier.
    rows = build_evidence(workspace, metadata=metadata).get("rows", [])
    row = next((r for r in rows if r.get("doi") == want), None)
    current = (row or {}).get("authors")

    cur_abbrev, cur_total = _abbrev_counts(current)
    if not current or cur_abbrev == 0:
        return {"authors": current, "expanded": False}

    try:
        from ber_core.metadata_fetcher import fetch_metadata_from_doi  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — no `requests`/fetcher: return what we have
        return {"authors": current, "expanded": False}

    try:
        fetched = (fetch_metadata_from_doi(doi) or {}).get("authors")
    except Exception:  # noqa: BLE001 — a bad lookup must not fail the request
        fetched = None

    new_abbrev, new_total = _abbrev_counts(fetched)
    # Accept only if the fetch didn't lose authors (wrong-paper guard) and has
    # strictly fewer abbreviated names than what we had.
    if fetched and new_total >= cur_total * 0.7 and new_abbrev < cur_abbrev:
        return {"authors": fetched, "expanded": True}
    return {"authors": current, "expanded": False}


class CorruptVerdictFile(ValueError):
    """A user_exclusions/user_inclusions file exists but cannot be read.

    Treating it as "no verdicts" would silently re-admit every manually
    excluded paper (and overwriting it would destroy them), so callers get
    this instead and the API answers 409 naming the file.
    """

    def __init__(self, path: Path):
        super().__init__(
            f"{path.name} in this review's data folder is not a valid JSON "
            "object, so the manual include/exclude decisions in it cannot be "
            "read. Fix or remove the file, then try again.")
        self.path = path


def _read_json_object(path: Path) -> dict:
    """The JSON object in *path*; ``{}`` if missing or blank; else raises
    :class:`CorruptVerdictFile`."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptVerdictFile(path) from exc
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise CorruptVerdictFile(path) from exc
    if not isinstance(obj, dict):
        raise CorruptVerdictFile(path)
    return obj


def _read_verdict_map(path: Path, wrapper: str) -> dict[str, str]:
    """``{norm_doi: reason}`` from a verdict file, either on-disk shape (bare,
    or the legacy ``{"<wrapper>": {...}}``), ``_``-comment keys dropped."""
    obj = _read_json_object(path)
    inner = obj.get(wrapper) if isinstance(obj.get(wrapper), dict) else obj
    out: dict[str, str] = {}
    for k, v in (inner or {}).items():
        if str(k).startswith("_"):
            continue
        key = norm_doi(k)
        if key:
            out[key] = str(v) if v else ""
    return out


def _load_user_verdicts(path: Path, wrapper: str, loader) -> dict[str, str]:
    """``{norm_doi: reason}`` for the DOIs the PIPELINE's loader says are in.

    Membership comes from ``ber_core.evidence`` — the same function
    stage_eligible, export and meta-analysis use — so the table and the
    pipeline cannot disagree about which papers a verdict file names; this
    module only adds the human-readable reason."""
    reasons = _read_verdict_map(path, wrapper)  # raises CorruptVerdictFile
    try:
        members = loader(path)
    except ValueError as exc:  # ber_core's own fail-loud error
        raise CorruptVerdictFile(path) from exc
    return {doi: reasons.get(doi, "") for doi in members}


def _load_user_exclusions(data: Path) -> dict[str, str]:
    """`{norm_doi: reason}` from `user_exclusions.json`, or empty."""
    return _load_user_verdicts(data / "user_exclusions.json", "exclusions",
                               load_exclusion_dois)


def _atomic_write_json(path: Path, obj) -> None:
    """Write *obj* as JSON via a temp file + ``os.replace``: a reader (the
    pipeline's loader, a concurrent request) sees the old file or the new one,
    never a truncated half — which ``json.loads`` would read as "no verdicts"."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_verdict_lock = threading.Lock()


def _set_user_verdict(
    workspace: Path, filename: str, dois: list[str], reason: str,
    *, present: bool, default_reason: str,
) -> int:
    """Add or remove DOIs in a `{doi: reason}` override file. Returns the
    number of rows changed. Backs both the exclusion and inclusion files, which
    share the exact same on-disk shape.

    A file still in the legacy ``{"exclusions"|"inclusions": {...}}`` wrapper
    is unwrapped (its ``_`` comment keys kept) and written back bare: editing
    the top level of a wrapped file used to add DOIs BESIDE the wrapper, where
    the loaders — which read only the wrapper when present — never saw them.
    """
    data = _data_dir(workspace)
    if data is None:
        return 0
    path = data / filename
    wrapper = "exclusions" if "exclusion" in filename else "inclusions"
    with _verdict_lock:
        return _set_user_verdict_locked(path, wrapper, dois, reason,
                                        present=present, default_reason=default_reason)


def _set_user_verdict_locked(
    path: Path, wrapper: str, dois: list[str], reason: str,
    *, present: bool, default_reason: str,
) -> int:
    # A corrupt file raises rather than being treated as empty: rewriting it
    # with just this request's DOIs would erase every decision it held.
    current = _read_json_object(path)
    if isinstance(current.get(wrapper), dict):
        comments = {k: v for k, v in current.items()
                    if str(k).startswith("_")}
        current = {**comments, **current[wrapper]}

    changed = 0
    for doi in dois:
        key = norm_doi(doi)
        if not key:
            continue
        if present and current.get(key) != (reason or default_reason):
            current[key] = reason or default_reason
            changed += 1
        elif not present and key in current:
            del current[key]
            changed += 1

    _atomic_write_json(path, current)
    return changed


def set_exclusions(
    workspace: Path, dois: list[str], reason: str, *, excluded: bool
) -> int:
    """Add or remove DOIs in `user_exclusions.json`. Returns the number changed.

    Bare `{doi: reason}` shape — the form `utils.load_exclusion_dois` reads and
    that export + meta already honor, so a single file drives exclusion end to
    end. Race-free: a tiny file rewritten whole, last-writer-wins.
    """
    return _set_user_verdict(
        workspace, "user_exclusions.json", dois, reason,
        present=excluded, default_reason="user_excluded")


def set_inclusions(
    workspace: Path, dois: list[str], reason: str, *, included: bool
) -> int:
    """Add or remove DOIs in `user_inclusions.json`. Returns the number changed.

    The positive counterpart to `set_exclusions`: a DOI here is force-staged for
    extraction by `stage_eligible` (via `utils.load_inclusion_dois`) even if the
    full-text eligibility screen said ineligible or never ran for it. Same bare
    `{doi: reason}` shape, so one loader reads it end to end.
    """
    return _set_user_verdict(
        workspace, "user_inclusions.json", dois, reason,
        present=included, default_reason="user_included")


def _load_user_inclusions(data: Path) -> dict[str, str]:
    """`{norm_doi: reason}` from `user_inclusions.json`, or empty."""
    return _load_user_verdicts(data / "user_inclusions.json", "inclusions",
                               load_inclusion_dois)
