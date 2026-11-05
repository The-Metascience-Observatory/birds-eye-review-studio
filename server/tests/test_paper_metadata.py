"""Background bibliographic enrichment (services/paper_metadata.py + the job).

Two properties carry it. First, metadata surfaces in the evidence table as an
EARLY tier — right after search, for every found DOI — so titles/authors appear
long before screening narrows the corpus. Second, the enrichment job runs in
PARALLEL: it is a pure network fetch that touches none of the resume-critical
workspace files, so it must be exempt from the one-job-per-review guard that
(correctly) serializes pipeline steps.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_meta_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from server.app.db.models import Job  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import evidence, jobs, paper_metadata  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email):
    await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Meta Test", "topic_name": "Meta Test",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


# --- the parallel-job exemption ----------------------------------------------

async def test_a_metadata_job_does_not_count_as_busy() -> None:
    """The whole point of the separate lane: a running metadata job must NOT
    block a pipeline run, and vice versa, because they never touch the same
    resume-critical files."""
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "metabusy@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="metadata", state="running"))
            await db.commit()
            # active_job (what _reject_if_busy consults) must ignore it.
            assert await jobs.active_job(db, rid) is None

            # Conversely a running PIPELINE job IS busy.
            db.add(Job(review_id=rid, kind="pipeline", state="running"))
            await db.commit()
            assert (await jobs.active_job(db, rid)) is not None


async def test_enrich_does_not_stack_duplicate_jobs() -> None:
    await init_models()
    from server.app.db.models import Review

    async with _client() as client:
        _, rid = await _setup(client, "metadup@example.com")
        async with AsyncSessionLocal() as db:
            seeded = Job(review_id=rid, kind="metadata", state="queued")
            db.add(seeded)
            await db.commit()
            review = await db.get(Review, rid)
            # A second enqueue returns the existing job, not a new one.
            existing = await jobs.enqueue(db, review, "metadata")
            assert existing is not None and existing.id == seeded.id, (
                "enqueue must hand back the job already in flight")
            n = len((await db.execute(
                select(Job).where(Job.review_id == rid)
            )).scalars().all())
            assert n == 1, "duplicate metadata job was created"


# --- the early tier -----------------------------------------------------------

async def test_metadata_surfaces_in_the_evidence_table_as_an_early_tier(monkeypatch) -> None:
    """DB metadata fills title/authors for a paper that only exists in
    search_dois so far — before any screening or extraction."""
    await init_models()
    tmp = Path(tempfile.mkdtemp())
    data = tmp / "projects" / "x" / "data"
    data.mkdir(parents=True)
    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,111\n")
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    md = {"10.1/a": {"title": "A Found Paper", "authors": "Smith J",
                     "year": "2021", "journal": "Lancet", "pmid": "111",
                     "metadata_source": "crossref"}}
    out = evidence.build_evidence(tmp, metadata=md)
    row = next(r for r in out["rows"] if r["doi"] == "10.1/a")
    assert row["title"] == "A Found Paper"
    assert row["authors"] == "Smith J"
    assert out["stage_reached"] == "search"


async def test_a_not_found_doi_is_recorded_so_reruns_skip_it(monkeypatch) -> None:
    """An unresolvable DOI must be a recorded fact, not a perpetual gap that the
    worker re-fetches on every run."""
    await init_models()

    async with _client() as client:
        _, rid = await _setup(client, "metanotfound@example.com")

        tmp = Path(tempfile.mkdtemp())
        data = tmp / "projects" / "x" / "data"
        data.mkdir(parents=True)
        (data / "search_dois.csv").write_text("doi,pmid\n10.1/gone,\n")

        monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: ["10.1/gone"])

        async with AsyncSessionLocal() as db:
            res = await paper_metadata.enrich_review(
                db, rid, tmp, fetcher=lambda doi: None)
            assert res["not_found"] == 1 and res["found"] == 0

            # Recorded as not_found.
            md = await paper_metadata.load_metadata(db, rid)
            assert md["10.1/gone"]["metadata_source"] == "not_found"

            # A second run skips it (already recorded).
            res2 = await paper_metadata.enrich_review(
                db, rid, tmp, fetcher=lambda doi: None)
            assert res2["fetched"] == 0 and res2["skipped"] == 1


async def test_found_metadata_is_upserted_and_readable(monkeypatch) -> None:
    await init_models()

    async with _client() as client:
        _, rid = await _setup(client, "metafound@example.com")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.yaml").write_text("topic:\n  name: x\n")

        monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: ["10.1/A"])

        async with AsyncSessionLocal() as db:
            res = await paper_metadata.enrich_review(
                db, rid, tmp,
                fetcher=lambda doi: {"title": "Real", "authors": "Doe J",
                                     "year": "2020", "journal": "BMJ",
                                     "source": "crossref"})
            assert res["found"] == 1
            md = await paper_metadata.load_metadata(db, rid)
            assert md["10.1/a"]["title"] == "Real"  # keyed on NORMALIZED doi


# --- the local SciSciNet tier (pass 1) ---------------------------------------

async def test_local_pass_populates_without_any_network(monkeypatch) -> None:
    """Pass 1 sweeps the local DBs and commits so the evidence table fills in
    seconds. A row complete enough locally must never reach the API fetcher —
    that's what makes the table responsive instead of network-paced."""
    await init_models()

    async with _client() as client:
        _, rid = await _setup(client, "metalocal@example.com")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.yaml").write_text("topic:\n  name: x\n")

        monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: ["10.1/A"])
        monkeypatch.setattr(paper_metadata, "_local_batch", lambda dois: {
            "10.1/a": {"title": "Local Paper", "authors": "Ada L", "year": "2019",
                       "journal": "Nature", "volume": "5", "issue": "2",
                       "pages": "1-9", "pmid": "42"},
        })

        def _boom(doi):
            raise AssertionError("the API fetcher ran despite a complete local hit")

        async with AsyncSessionLocal() as db:
            res = await paper_metadata.enrich_review(db, rid, tmp, fetcher=_boom)
            assert res["local"] == 1 and res["found"] == 1

            md = await paper_metadata.load_metadata(db, rid)
            assert md["10.1/a"]["title"] == "Local Paper"
            assert md["10.1/a"]["metadata_source"] == "local"


async def test_api_pass_tops_up_without_blanking_local_values(monkeypatch) -> None:
    """SciSciNet has no volume/issue/pages, so a local row is incomplete and the
    APIs still run — but _upsert rewrites every column, so the thinner API result
    must be merged onto the local one rather than replacing it."""
    await init_models()

    async with _client() as client:
        _, rid = await _setup(client, "metatopup@example.com")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.yaml").write_text("topic:\n  name: x\n")

        monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: ["10.1/A"])
        monkeypatch.setattr(paper_metadata, "_local_batch", lambda dois: {
            "10.1/a": {"title": "Local Paper", "authors": "Ada L", "year": "2019",
                       "journal": "Nature"},  # no volume/issue/pages
        })

        async with AsyncSessionLocal() as db:
            res = await paper_metadata.enrich_review(
                db, rid, tmp,
                # A typical thin API reply: only the biblio bits.
                fetcher=lambda doi: {"title": None, "authors": None, "journal": None,
                                     "year": None, "volume": "5", "issue": "2",
                                     "pages": "1-9", "source": "crossref"})
            assert res["found"] == 1

            md = await paper_metadata.load_metadata(db, rid)
            row = md["10.1/a"]
            assert row["volume"] == "5" and row["pages"] == "1-9"   # topped up
            assert row["title"] == "Local Paper"                     # NOT blanked
            assert row["authors"] == "Ada L" and row["journal"] == "Nature"


async def test_enrichment_is_unchanged_when_the_tier_is_off(monkeypatch) -> None:
    """With no local DBs configured, behaviour must match the pure-API path."""
    await init_models()

    async with _client() as client:
        _, rid = await _setup(client, "metanolocal@example.com")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.yaml").write_text("topic:\n  name: x\n")

        monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: ["10.1/A"])
        monkeypatch.setattr(paper_metadata, "_local_batch", lambda dois: {})

        async with AsyncSessionLocal() as db:
            res = await paper_metadata.enrich_review(
                db, rid, tmp,
                fetcher=lambda doi: {"title": "From API", "authors": "Doe J",
                                     "source": "crossref"})
            assert res["local"] == 0 and res["found"] == 1
            md = await paper_metadata.load_metadata(db, rid)
            assert md["10.1/a"]["metadata_source"] == "crossref"


# --- dump to the local cache the dashboard build reads -----------------------

async def test_dump_to_disk_feeds_the_export_backfill(monkeypatch) -> None:
    """The studio holds biblio in the DB, but export_dashboard reads it from an
    on-disk CSV. dump_to_disk bridges that gap so the dashboard build backfills
    titles LOCALLY instead of re-fetching every one over the network. Proven by
    round-tripping through the very reader the export uses."""
    await init_models()

    async with _client() as client:
        _, rid = await _setup(client, "metadump@example.com")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.yaml").write_text("topic:\n  name: x\n")

        monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: ["10.1/A", "10.1/B"])

        async with AsyncSessionLocal() as db:
            def _fetch(doi):
                # enrich_review passes the CANONICAL (normalized, lowercased) DOI.
                # One real hit, one unresolvable — the not_found row must NOT be
                # dumped (it has no title and would just be noise).
                if doi == "10.1/a":
                    return {"title": "Real Paper", "authors": "Doe J", "year": "2020",
                            "journal": "BMJ", "source": "crossref"}
                return None

            await paper_metadata.enrich_review(db, rid, tmp, fetcher=_fetch)
            n = await paper_metadata.dump_to_disk(db, rid, tmp)
            assert n == 1  # only the resolved paper, not the not_found one

        # The reader the export step actually uses must read it back as biblio.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_export_for_metadata", REPO_ROOT / "steps" / "export_dashboard.py")
        exp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exp)
        data_dir = tmp / "projects" / "x" / "data"
        enriched = data_dir / "classifications_enriched.csv"
        assert enriched.exists()

        local = exp._load_local_metadata(None, enriched)
        assert local["10.1/a"]["title"] == "Real Paper"
        assert local["10.1/a"]["journal"] == "BMJ"
        assert "10.1/b" not in local  # the not_found DOI was skipped
