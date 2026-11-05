"""Background bibliographic enrichment: DOI → title/authors/year, in parallel.

Metadata is a pure network fetch (Crossref et al.), it touches none of the
resume-critical pipeline files, and it is the slowest thing to block the evidence
table on. So it runs as its OWN job, exempt from the one-job-per-review guard,
firing right after search — while classify/screen/extract proceed in parallel.
Rows land in the `paper_metadata` table and surface in the evidence table as
titles/authors fill in.

An unresolved DOI is RECORDED (`source="not_found"`) rather than left absent, so
a re-run skips DOIs already tried and the table can honestly say "no metadata
found" instead of looking perpetually unfinished.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import PaperMetadata

from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()

from ber_core.utils import norm_doi  # noqa: E402
from ber_core.identity import canonical_doi, paper_join_key  # noqa: E402

# KEYS vs. LOOKUPS. Rows are keyed by ``norm_doi`` because that is the key the
# evidence table (services/evidence.py) joins them back on — a different
# normalizer here would silently orphan metadata for any DOI the two disagree
# on (a trailing period, a doi.org URL). What is SENT to the fetcher is the
# shared ``paper_join_key``, which also decodes fetchpdf's ``~XX~`` stems and
# strips URL/``doi:`` prefixes so the API gets a resolvable DOI.

# Fields we keep from the fetcher's (much larger) return.
_KEEP = ("title", "authors", "year", "journal", "pmid", "volume", "issue",
         "pages", "url")


def _search_dois(workspace: Path) -> list[str]:
    """Every DOI the search found — the full set to enrich, not the narrowed
    screening subset, so metadata appears for all papers immediately."""
    from . import pipeline_bridge

    try:
        data = pipeline_bridge.build_project(workspace).data_dir
    except Exception:  # noqa: BLE001
        return []
    path = data / "search_dois.csv"
    if not path.exists():
        return []
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        return [r["doi"] for r in csv.DictReader(f) if (r.get("doi") or "").strip()]


async def load_metadata(db: AsyncSession, review_id: str) -> dict[str, dict]:
    """norm_doi -> {title, authors, ...} for every enriched paper in the review."""
    rows = (
        await db.execute(
            select(PaperMetadata).where(PaperMetadata.review_id == review_id)
        )
    ).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r.doi] = {
            "title": r.title, "authors": r.authors, "year": r.year,
            "journal": r.journal, "pmid": r.pmid, "volume": r.volume,
            "issue": r.issue, "pages": r.pages, "url": r.url,
            "metadata_source": r.source,
        }
    return out


#: Columns export_dashboard._load_local_metadata reads from
#: classifications_enriched.csv — the same columns enrich_metadata.py writes.
_ENRICHED_CSV_COLUMNS = (
    "doi", "title", "authors", "journal", "year", "volume", "issue", "url",
)


async def dump_to_disk(db: AsyncSession, review_id: str, workspace: Path) -> int:
    """Materialize this review's enriched metadata to the local cache CSV the
    export step reads, so the dashboard build backfills biblio from the DB instead
    of re-fetching every title over the network.

    The studio enriches metadata into the ``paper_metadata`` TABLE (not the on-disk
    ``enrich_metadata.py`` CSVs), so ``export_dashboard._load_local_metadata``
    finds nothing on disk and falls through to a slow serial Crossref backfill for
    titles that already exist in the DB. Writing ``classifications_enriched.csv``
    here — in exactly the shape that reader expects — makes the lookup local and
    instant. Returns the number of rows written (papers with real biblio).
    """
    from . import pipeline_bridge

    meta = await load_metadata(db, review_id)
    rows = [
        {"doi": doi, **{k: (m.get(k) or "") for k in _ENRICHED_CSV_COLUMNS if k != "doi"}}
        # Skip DOIs recorded only as "not_found"/placeholder: they carry no title
        # and would just be empty rows the reader ignores anyway.
        for doi, m in meta.items()
        if m.get("metadata_source") not in (None, "not_found") and m.get("title")
    ]
    if not rows:
        return 0

    try:
        data_dir = pipeline_bridge.build_project(workspace).data_dir
    except Exception:  # noqa: BLE001 — no workspace/config yet; nothing to dump
        return 0
    data_dir.mkdir(parents=True, exist_ok=True)

    from ber_core.utils import write_csv_atomic

    write_csv_atomic(
        data_dir / "classifications_enriched.csv", rows, list(_ENRICHED_CSV_COLUMNS)
    )
    return len(rows)


async def _upsert(db: AsyncSession, review_id: str, doi: str, meta: dict, source: str):
    """Insert-or-update one paper's metadata (composite PK review_id+doi)."""
    meta = dict(meta or {})
    # Some upstream fetchers build a doi.org URL from the `--`-escaped filename
    # form (`https://doi.org/10.13703--j…`) — a dead link. Canonicalize it to the
    # real DOI so the stored url is a working resolver link.
    u = meta.get("url")
    if isinstance(u, str) and "doi.org/" in u and "--" in u:
        fixed = canonical_doi(u)
        meta["url"] = f"https://doi.org/{fixed}" if fixed else u.replace("--", "/")
    values = {
        "review_id": review_id, "doi": norm_doi(doi), "source": source,
        "fetched_at": datetime.now(timezone.utc),
        **{k: (str(meta.get(k)) if meta.get(k) is not None else None) for k in _KEEP},
    }
    # sqlite upsert; the dev DB is sqlite and prod (postgres) has an equivalent
    # on_conflict, but keep it simple — the studio runs on sqlite today.
    stmt = sqlite_insert(PaperMetadata).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["review_id", "doi"],
        set_={k: values[k] for k in ("source", "fetched_at", *_KEEP)},
    )
    await db.execute(stmt)


async def cache_authors(db: AsyncSession, review_id: str, doi: str, authors: str) -> None:
    """Persist an on-demand author expansion WITHOUT touching other columns.

    The detail panel expands abbreviated author names on open (via the DOI
    fetcher); caching the result here means it's expanded once, not every open.
    Unlike ``_upsert``, this sets ONLY the ``authors`` column on conflict, so a
    row's already-good title/journal/year are never blanked by a partial write.
    """
    key = norm_doi(doi)
    if not key or not authors:
        return
    now = datetime.now(timezone.utc)
    stmt = sqlite_insert(PaperMetadata).values(
        review_id=review_id, doi=key, authors=authors, fetched_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["review_id", "doi"],
        set_={"authors": authors, "fetched_at": now},
    )
    await db.execute(stmt)
    await db.commit()


_COMPLETE_FIELDS = ("title", "authors", "journal", "year", "volume", "issue", "pages")


def _is_complete(meta: dict) -> bool:
    """True when a local row needs no API top-up at all."""
    return all(meta.get(k) for k in _COMPLETE_FIELDS)


def _local_batch(dois: list[str]) -> dict[str, dict]:
    """Bulk local-tier lookup, keyed by norm_doi. {} when the tier is off.

    Runs in a worker thread (sync sqlite) and swallows everything: the local tier
    is an optimization, never a dependency.
    """
    try:
        from ber_core import local_sciscinet

        if not local_sciscinet.available():
            return {}
        return local_sciscinet.lookup_batch(dois)
    except Exception:  # noqa: BLE001
        return {}


def _default_fetcher():
    """The real DOI fetcher, imported lazily (pulls `requests`, absent in the
    lean server venv / test env)."""
    from ber_core.metadata_fetcher import fetch_metadata_from_doi

    return fetch_metadata_from_doi


async def enrich_review(
    db: AsyncSession, review_id: str, workspace: Path, *, limit: int = 0,
    fetcher=None,
) -> dict:
    """Fetch metadata for every un-enriched search DOI. Returns {fetched, found,
    not_found, skipped}. Commits incrementally so the evidence table sees rows
    appear WHILE this runs.

    ``fetcher`` is injectable so tests can supply a fake without the network
    dependency the real one pulls in.
    """
    import anyio

    fetch_metadata_from_doi = fetcher or _default_fetcher()

    dois = _search_dois(workspace)
    already = set((await load_metadata(db, review_id)).keys())
    todo = [d for d in dois if norm_doi(d) not in already]
    if limit:
        todo = todo[:limit]

    # --- Pass 1: local SciSciNet sweep -------------------------------------
    # Cheap disk reads with no network, committed in batches, so the evidence
    # table fills in seconds instead of waiting on the API waterfall. Rows land
    # with source="local" and (usually) no volume/issue/pages; pass 2 tops those
    # up in the background. Skipped entirely when the tier isn't configured.
    local_hits: dict[str, dict] = {}
    if todo:
        try:
            local_hits = await anyio.to_thread.run_sync(_local_batch, todo)
        except Exception:  # noqa: BLE001 — the fast path must never break enrichment
            local_hits = {}

    for i, doi in enumerate(todo):
        meta = local_hits.get(norm_doi(doi))
        if meta:
            await _upsert(db, review_id, doi, meta, "local")
            if i % 50 == 49:
                await db.commit()
    if local_hits:
        await db.commit()

    found = not_found = 0
    for i, doi in enumerate(todo):
        # `search_dois.csv` stores DOIs in the `--` filename form
        # (`10.3346--jkms.2026.41.e279`); the fetcher interpolates its argument
        # straight into API URLs, so it MUST get the canonical `/` form or every
        # source 404s and the paper is falsely recorded `not_found`.
        canonical = paper_join_key(doi)
        local = local_hits.get(norm_doi(doi))
        if local and _is_complete(local):
            # Pass 1 already had everything — no network call needed at all.
            found += 1
            continue
        try:
            meta = await anyio.to_thread.run_sync(fetch_metadata_from_doi, canonical)
        except Exception:  # noqa: BLE001 — a single bad DOI must not kill the run
            meta = None
        if local:
            # Merge onto the local row: _upsert rewrites every _KEEP column, so a
            # thin API reply must not blank pass-1 values. Note the top-up is
            # usually biblio-ONLY (volume/issue/pages) and so has neither title nor
            # authors — it must still be written, hence no title/authors gate here.
            merged = {**{k: v for k, v in local.items() if v},
                      **{k: v for k, v in (meta or {}).items() if v}}
            source = (meta or {}).get("source") or "local"
            await _upsert(db, review_id, doi, merged, source)
            found += 1
        elif meta and (meta.get("title") or meta.get("authors")):
            await _upsert(db, review_id, doi, meta, meta.get("source") or "lookup")
            found += 1
        else:
            # Record the miss so a re-run skips it and the UI stops waiting on it.
            await _upsert(db, review_id, doi, {}, "not_found")
            not_found += 1
        # Commit every few rows so the table fills in live rather than all at once.
        if i % 10 == 9:
            await db.commit()
    await db.commit()
    return {"fetched": len(todo), "found": found, "not_found": not_found,
            "skipped": len(already), "local": len(local_hits)}
