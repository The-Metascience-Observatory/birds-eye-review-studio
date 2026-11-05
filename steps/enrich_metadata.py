#!/usr/bin/env python3
"""Enrich any CSV or JSONL with publication metadata from DOI lookups.

Consolidates enrich_classifications.py, enrich_screening_metadata.py,
enrich_pass2_metadata.py, and fetch_all_metadata.py into one script.

Project-aware: defaults --input to projects/{slug}/data/classifications.csv
where {slug} is derived from topic.name in the review config.

Usage (every run names its review config):
    python steps/enrich_metadata.py --config configs/config_x.yaml
    python steps/enrich_metadata.py --config ... --input path/to/file.csv
    python steps/enrich_metadata.py --config ... --input trial_screening.csv --prefix paper_
    python steps/enrich_metadata.py --config ... --input extractions.jsonl --doi-column paper_id
    python steps/enrich_metadata.py --config ... --input data.csv --local-cache classifications_enriched.csv
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.environment import load_local_env as load_dotenv

from ber_core.metadata_fetcher import fetch_metadata_from_doi
from ber_core.utils import (load_config, write_csv_atomic, log_progress, slugify, norm_doi,
                   read_jsonl, redact_secrets, RED, YELLOW, RESET)

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(BASE_DIR / ".env.local")

PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))


logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# Canonical metadata fields (unprefixed). Prefix is added at output time.
META_FIELDS = ["title", "journal", "year", "authors", "volume", "issue", "pages", "url"]


# ---------------------------------------------------------------------------
# Local cache loading
# ---------------------------------------------------------------------------

def load_local_cache(cache_path: Path, doi_column: str = "doi") -> dict[str, dict]:
    """Read a CSV and build doi -> {field: value} dict for META_FIELDS.

    Looks for columns matching META_FIELDS directly (e.g. 'title') or with
    common prefixes like 'paper_' (e.g. 'paper_title'). Returns unprefixed keys.
    """
    index: dict[str, dict] = {}
    if not cache_path.exists():
        print(f"  {YELLOW}WARN: Cache file not found: {cache_path}{RESET}",
              file=sys.stderr, flush=True)
        return index

    with open(cache_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return index

        # Build mapping: source_column -> canonical META_FIELD
        col_map: dict[str, str] = {}
        for field in META_FIELDS:
            for candidate in [field, f"paper_{field}"]:
                if candidate in reader.fieldnames:
                    col_map[candidate] = field
                    break

        for row in reader:
            doi = row.get(doi_column, "").strip()
            if not doi:
                continue
            meta = {}
            for src_col, canon in col_map.items():
                val = row.get(src_col, "").strip()
                if val:
                    meta[canon] = val
            # Only store if we got at least title or authors
            if meta.get("title") or meta.get("authors"):
                index[doi] = meta

    return index


# ---------------------------------------------------------------------------
# DOI fetch wrapper
# ---------------------------------------------------------------------------

def fetch_one(doi: str, enable_core: bool = False) -> tuple[str, dict | None]:
    """Fetch metadata for a single DOI via the multi-API fetcher.

    ``enable_core`` opts into the CORE (core.ac.uk) tier, which is off by
    default in the fetcher because it matches by TITLE similarity rather than
    DOI — slow, and timeout-prone on a large corpus. Worth switching on for a
    small set of DOIs the cheaper sources could not resolve. Needs COREAPIKEY.
    """
    try:
        raw = fetch_metadata_from_doi(doi, enable_core=enable_core)
        if raw:
            meta = {}
            for field in META_FIELDS:
                val = raw.get(field)
                if val and str(val).strip() not in ("", "NaN", "None"):
                    meta[field] = str(val).strip()
            if meta.get("title") or meta.get("authors"):
                return doi, meta
    except Exception as e:
        print(f"  {RED}ERROR: {doi}: {redact_secrets(e)}{RESET}", file=sys.stderr, flush=True)
    return doi, None


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def prefixed(field: str, prefix: str) -> str:
    """Return prefixed field name: 'title' -> 'paper_title' (if prefix='paper_')."""
    return f"{prefix}{field}" if prefix else field


def passes_inclusion(row: dict, skip_relevance: set[str],
                     keep_types: set[str] | None) -> bool:
    """Mirror download_pdfs.py's first-pass inclusion filter.

    Drops rows whose is_relevant is in *skip_relevance* (e.g. {'no'} or
    {'no','unknown'}), and — if *keep_types* is given — rows whose article_type
    is not in it. Rows lacking an is_relevant column (e.g. JSONL extractions
    that were already filtered upstream) always pass.
    """
    if "is_relevant" not in row:
        return True
    if row.get("is_relevant", "").strip().lower() in skip_relevance:
        return False
    if keep_types is not None and row.get("article_type") not in keep_types:
        return False
    return True


def row_has_metadata(row: dict, prefix: str) -> bool:
    """Check if a row already has title or authors."""
    return bool(row.get(prefixed("title", prefix)) or
                row.get(prefixed("authors", prefix)))


def apply_metadata(row: dict, meta: dict, prefix: str) -> None:
    """Write metadata fields into row with prefix."""
    for field, val in meta.items():
        row[prefixed(field, prefix)] = val


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    """Load all records from a JSONL file — strictly.

    This script REWRITES the file it read, so a line it could not parse would
    be silently deleted from it (it used to be). A malformed line is fatal
    here instead: repair or remove it, then re-run.
    """
    try:
        return read_jsonl(path, on_error="raise")
    except ValueError as exc:
        print(f"  {RED}ERROR: {exc}\n  Refusing to rewrite a file with an "
              f"unreadable record (it would be lost).{RESET}", file=sys.stderr)
        sys.exit(1)


def write_jsonl_atomic(path: Path, records: list[dict]) -> None:
    """Write records to JSONL atomically (tmp + rename)."""
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Enrich any CSV or JSONL with publication metadata from DOI lookups."
    )
    parser.add_argument("--config", required=True,
                        help="Path to the review's YAML config (drives the project slug)")
    parser.add_argument("--input", default=None,
                        help="Input file (CSV or JSONL). Default: projects/{slug}/data/classifications.csv")
    parser.add_argument("--doi-column", default="doi",
                        help="Column/field containing DOIs (default: 'doi')")
    parser.add_argument("--prefix", default=None,
                        help="Prefix for added metadata columns (e.g. 'paper_'). "
                             "Default: auto-detect from existing columns, else ''")
    parser.add_argument("--local-cache", action="append", default=[],
                        help="CSV file(s) with pre-existing metadata to check first "
                             "(can be specified multiple times)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of concurrent API workers (default: 5)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit API fetches (0 = all)")
    parser.add_argument("--flush-every", type=int, default=25,
                        help="Write to disk every N API fetches (default: 25)")
    parser.add_argument("--all-rows", action="store_true",
                        help="Enrich every row, ignoring classification filters "
                             "(default: only rows that passed the first inclusion filter, "
                             "i.e. is_relevant != 'no')")
    parser.add_argument("--relevant-only", action="store_true",
                        help="Also drop is_relevant=unknown rows (keeps only 'yes'). "
                             "Mirrors download_pdfs.py --relevant-only.")
    parser.add_argument("--types", default=None,
                        help="Comma-separated article_type values to keep "
                             "(default: no type filter). Mirrors download_pdfs.py --types.")
    parser.add_argument("--enable-core", action="store_true",
                        help="Also query CORE (core.ac.uk) for metadata. Off by "
                             "default: CORE matches by title similarity, not "
                             "DOI, so it is slow and timeout-prone at corpus "
                             "scale. Useful as a last resort for DOIs the other "
                             "sources missed. Requires COREAPIKEY in .env.local")
    parser.add_argument("--only-dois", default=None,
                        help="Path to a CSV/text file of DOIs (one per line, or a "
                             "'doi' column). Restrict enrichment to just these DOIs "
                             "-- used by incremental updates to avoid re-enriching "
                             "the whole corpus. Missing file = no restriction.")
    args = parser.parse_args()

    # --- Resolve project-aware default path -----------------------------------
    config = load_config(args.config)
    topic_name = config.get("topic", {}).get("name", "default")
    slug = slugify(topic_name)
    project_dir = PROJECTS_DIR / slug

    if args.input:
        input_path = Path(args.input)
    else:
        input_path = project_dir / "data" / "classifications.csv"

    print(f"Project: {topic_name!r} -> {project_dir}", file=sys.stderr, flush=True)
    print(f"Input:   {input_path}", file=sys.stderr, flush=True)

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    is_jsonl = input_path.suffix.lower() in (".jsonl", ".ndjson")
    is_csv = input_path.suffix.lower() == ".csv"
    if not is_jsonl and not is_csv:
        print(f"ERROR: Unsupported file type: {input_path.suffix} (expected .csv or .jsonl)",
              file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Load input
    # ------------------------------------------------------------------
    if is_csv:
        with open(input_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            orig_fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
    else:
        rows = load_jsonl(input_path)
        orig_fieldnames = []  # not used for JSONL

    print(f"Loaded {len(rows)} records from {input_path}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    # Determine prefix
    # ------------------------------------------------------------------
    if args.prefix is not None:
        prefix = args.prefix
    else:
        # Auto-detect: if any row already has 'paper_title' or 'paper_authors', use 'paper_'
        if is_csv:
            has_prefixed = any(f.startswith("paper_") for f in orig_fieldnames
                               if f in [f"paper_{m}" for m in META_FIELDS])
        else:
            has_prefixed = any(f"paper_{m}" in rows[0] for m in META_FIELDS) if rows else False
        prefix = "paper_" if has_prefixed else ""

    print(f"Using metadata prefix: '{prefix}' (columns: {prefixed('title', prefix)}, ...)",
          file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    # Build output fieldnames (CSV only)
    # ------------------------------------------------------------------
    if is_csv:
        out_fieldnames = list(orig_fieldnames)
        for field in META_FIELDS:
            col = prefixed(field, prefix)
            if col not in out_fieldnames:
                out_fieldnames.append(col)

    # ------------------------------------------------------------------
    # Load local caches
    # ------------------------------------------------------------------
    cache_index: dict[str, dict] = {}
    for cache_path_str in args.local_cache:
        cache_path = Path(cache_path_str)
        print(f"Loading local cache: {cache_path}...", file=sys.stderr, flush=True)
        cache = load_local_cache(cache_path, doi_column=args.doi_column)
        print(f"  {len(cache)} DOIs with metadata", file=sys.stderr, flush=True)
        # Earlier caches take priority — don't overwrite
        for doi, meta in cache.items():
            if doi not in cache_index:
                cache_index[doi] = meta

    # ------------------------------------------------------------------
    # Pass 1: fill from local cache + mark already-enriched
    # ------------------------------------------------------------------
    # First-pass inclusion filter (skip unless --all-rows). Mirrors download.
    if args.all_rows:
        skip_relevance: set[str] = set()
        keep_types = None
    else:
        skip_relevance = {"no", "unknown"} if args.relevant_only else {"no"}
        keep_types = ({t.strip() for t in args.types.split(",") if t.strip()}
                      if args.types else None)

    # Optional restriction to a specific DOI set (incremental updates). Normalize
    # both sides to canonical "/" form so the stored "--" convention also matches.
    only_dois: set[str] | None = None
    if args.only_dois:
        op = Path(args.only_dois)
        if op.exists():
            only_dois = set()
            with open(op, newline="", encoding="utf-8", errors="replace") as f:
                rdr = csv.reader(f)
                for r in rdr:
                    if not r:
                        continue
                    val = r[0].strip()
                    if val.lower() in ("doi", ""):
                        continue
                    only_dois.add(norm_doi(val))
            print(f"--only-dois: restricting to {len(only_dois)} DOIs from {op}",
                  file=sys.stderr, flush=True)
        else:
            print(f"--only-dois: file not found ({op}); no restriction applied",
                  file=sys.stderr, flush=True)

    already_enriched = 0
    filled_local = 0
    excluded = 0
    skipped_scope = 0
    need_api: list[int] = []

    for i, row in enumerate(rows):
        doi = row.get(args.doi_column, "").strip()

        # Restrict to the requested DOI set, if any
        if only_dois is not None and norm_doi(doi) not in only_dois:
            skipped_scope += 1
            continue

        # Skip if already enriched
        if row_has_metadata(row, prefix):
            already_enriched += 1
            continue

        # Skip rows that did not pass the first-pass inclusion filter
        if not passes_inclusion(row, skip_relevance, keep_types):
            excluded += 1
            continue

        # Try local cache
        if doi and doi in cache_index:
            apply_metadata(row, cache_index[doi], prefix)
            filled_local += 1
        elif doi:
            need_api.append(i)
        # No DOI at all — skip silently

    if args.all_rows:
        filt_desc = "all rows (--all-rows)"
    else:
        filt_desc = f"skip is_relevant in {sorted(skip_relevance)}"
        if keep_types is not None:
            filt_desc += f", types in {sorted(keep_types)}"
    print(f"Inclusion filter: {filt_desc} -> excluded {excluded} rows",
          file=sys.stderr, flush=True)
    if only_dois is not None:
        print(f"Out of scope (--only-dois): skipped {skipped_scope} rows",
              file=sys.stderr, flush=True)
    print(f"Already enriched: {already_enriched}, filled from local cache: {filled_local}, "
          f"need API: {len(need_api)}", file=sys.stderr, flush=True)

    # Write after local enrichment
    if filled_local > 0:
        if is_csv:
            write_csv_atomic(input_path, rows, out_fieldnames)
        else:
            write_jsonl_atomic(input_path, rows)
        print(f"Wrote {filled_local} locally enriched records to {input_path}",
              file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    # Pass 2: API fetches
    # ------------------------------------------------------------------
    if args.limit > 0:
        need_api = need_api[:args.limit]

    if not need_api:
        print("No API fetches needed.", file=sys.stderr, flush=True)
    else:
        print(f"Fetching {len(need_api)} DOIs with {args.workers} workers...",
              file=sys.stderr, flush=True)

        total = len(need_api)
        completed = 0
        fetched = 0
        failed = 0
        unflushed = 0
        start = time.monotonic()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(fetch_one, rows[idx].get(args.doi_column, "").strip(),
                            args.enable_core): idx
                for idx in need_api
            }
            try:
                for future in as_completed(futures):
                    idx = futures[future]
                    doi, meta = future.result()
                    completed += 1

                    if meta:
                        apply_metadata(rows[idx], meta, prefix)
                        # If no url was fetched, add a default DOI url
                        url_col = prefixed("url", prefix)
                        if not rows[idx].get(url_col) and doi:
                            rows[idx][url_col] = f"https://doi.org/{doi}"
                        fetched += 1
                        unflushed += 1
                    else:
                        failed += 1

                    # Flush to disk periodically
                    if unflushed >= args.flush_every:
                        if is_csv:
                            write_csv_atomic(input_path, rows, out_fieldnames)
                        else:
                            write_jsonl_atomic(input_path, rows)
                        unflushed = 0

                    if completed % 50 == 0:
                        log_progress(completed, total, start_time=start,
                                     extra=f"fetched: {fetched}  failed: {failed}")

            except KeyboardInterrupt:
                print(f"\n  {YELLOW}Interrupted! Flushing to disk...{RESET}",
                      file=sys.stderr, flush=True)
                pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.monotonic() - start
        print(f"  API done: {fetched} fetched, {failed} failed in {elapsed:.0f}s",
              file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    # Final write
    # ------------------------------------------------------------------
    if is_csv:
        write_csv_atomic(input_path, rows, out_fieldnames)
    else:
        write_jsonl_atomic(input_path, rows)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    title_col = prefixed("title", prefix)
    authors_col = prefixed("authors", prefix)
    journal_col = prefixed("journal", prefix)

    has_title = sum(1 for r in rows if r.get(title_col))
    has_authors = sum(1 for r in rows if r.get(authors_col))
    has_journal = sum(1 for r in rows if r.get(journal_col))

    print(
        f"\nDone. {len(rows)} records written to {input_path}\n"
        f"  {title_col:20s} {has_title}/{len(rows)}\n"
        f"  {authors_col:20s} {has_authors}/{len(rows)}\n"
        f"  {journal_col:20s} {has_journal}/{len(rows)}",
        file=sys.stderr, flush=True,
    )


if __name__ == "__main__":
    main()
