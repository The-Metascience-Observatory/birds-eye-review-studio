"""Server correctness fixes: delete, config gate, stage activity, drafts,
verdict files, job admission, reconcile, evidence reads."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402

from server.app.config import settings  # noqa: E402
from server.app.db.models import (  # noqa: E402
    AgentMessage,
    Job,
    PublishedDashboard,
    Review,
    ReviewConfig,
    StageState,
)
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import (  # noqa: E402
    agent as agent_svc,
    evidence,
    jobs,
    reconcile,
    review_lifecycle,
)
from server.app.services import taxonomy_draft as tax  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _review(client, email, topic):
    r = await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=h, json={
        "title": "C", "topic_name": topic, "synonyms": ["x"],
        "description": "d", "not_about": []})
    assert r.status_code == 201, r.text
    return h, r.json()["id"]


@pytest.fixture(autouse=True)
async def _db():
    await init_models()
    yield


@pytest.fixture
def no_queue(monkeypatch):
    pool = SimpleNamespace(enqueue_job=AsyncMock())
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(return_value=pool))
    return pool


# --- C5: deleting a review ----------------------------------------------------------

async def test_delete_review_removes_rows_stops_jobs_and_keeps_the_workspace(no_queue):
    async with _client() as c:
        h, rid = await _review(c, "c5@example.com", "C5 delete")
        async with AsyncSessionLocal() as db:
            review = await db.get(Review, rid)
            workspace = Path(review.workspace_path)
            (workspace / "marker.txt").write_text("keep me")
            running = Job(review_id=rid, kind="pipeline", state="running")
            db.add_all([
                running,
                StageState(review_id=rid, stage="setup"),
                AgentMessage(review_id=rid, role="user", content="hi"),
                PublishedDashboard(route="c5-delete", slug="c5_delete",
                                   review_id=rid, source="studio"),
            ])
            await db.commit()
            job_id = running.id
        r = await c.delete(f"/reviews/{rid}", headers=h)
        assert r.status_code == 204, r.text
        assert (await c.get(f"/reviews/{rid}", headers=h)).status_code == 404

    async with AsyncSessionLocal() as db:
        assert await db.get(Job, job_id) is None
        assert (await db.execute(select(StageState).where(StageState.review_id == rid))).first() is None
        assert (await db.execute(select(AgentMessage).where(AgentMessage.review_id == rid))).first() is None
        assert await db.get(ReviewConfig, rid) is None
        # The live dashboard keeps protecting its route, detached.
        pub = await db.get(PublishedDashboard, "c5-delete")
        assert pub.review_id is None and pub.source == "orphaned"
    # The workspace was moved aside, not erased.
    assert not workspace.exists()
    moved = list(review_lifecycle.deleted_dir().glob(f"{rid}-*"))
    assert len(moved) == 1 and (moved[0] / "marker.txt").read_text() == "keep me"
    # …and the running job was told to stop.
    assert (moved[0] / "_jobs" / f"{job_id}.cancel").exists()


# --- C6: config edits during a long background job --------------------------------

async def test_display_and_policy_keys_may_change_while_a_job_runs():
    async with _client() as c:
        h, rid = await _review(c, "c6@example.com", "C6 config")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="pdf", state="running"))
            await db.commit()
        r = await c.patch(f"/reviews/{rid}/config", headers=h,
                          json={"config_json": {"include_animal_studies": True}})
        assert r.status_code == 200, r.text
        r = await c.patch(f"/reviews/{rid}/config", headers=h,
                          json={"config_json": {"dashboard": {"layout": "grid"}}})
        assert r.status_code == 200, r.text
        # A key a running script reads is still refused.
        r = await c.patch(f"/reviews/{rid}/config", headers=h,
                          json={"config_json": {"fulltext": {"supplementary": True}}})
        assert r.status_code == 409


# --- C3: last_job_state --------------------------------------------------------------

async def test_stage_activity_reports_the_last_job_state():
    async with _client() as c:
        h, rid = await _review(c, "c3@example.com", "C3 activity")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="pipeline", state="failed",
                       params={"only_ids": ["1_search"]}))
            await db.commit()
        stages = (await c.get(f"/reviews/{rid}/stages", headers=h)).json()["stages"]
    by_slug = {s["slug"]: s for s in stages}
    assert by_slug["search"]["activity"]["last_job_state"] == "failed"
    assert by_slug["classify"]["activity"]["last_job_state"] is None


# --- C7: taxonomy drafting fills only what is empty ---------------------------------

async def test_taxonomy_draft_never_overwrites_a_filled_vocabulary(monkeypatch):
    monkeypatch.setattr(tax, "_draft_blocking", lambda **kw: {
        "intervention_categories": ["model_a", "model_b"],
        "outcome_domains": ["model_outcome"], "provider": "codex_cli"})
    async with _client() as c:
        h, rid = await _review(c, "c7@example.com", "C7 tax")
        r = await c.patch(f"/reviews/{rid}/config", headers=h, json={"config_json": {
            "intervention_categories": ["mine_1", "mine_2"]}})
        assert r.status_code == 200
        r = await c.post(f"/reviews/{rid}/taxonomies/draft", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["intervention_categories"] == ["mine_1", "mine_2"]
    assert body["outcome_domains"] == ["model_outcome"]


# --- C14: verdict files ---------------------------------------------------------------

def _data_workspace(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    (w / "projects" / "v" / "data").mkdir(parents=True)
    (w / "config.yaml").write_text("topic:\n  name: V\n")
    return w


def test_verdict_write_unwraps_the_legacy_wrapper(tmp_path):
    w = _data_workspace(tmp_path)
    f = w / "projects" / "v" / "data" / "user_exclusions.json"
    f.write_text(json.dumps({"_comment": "hand-kept", "exclusions": {"10.1/old": "dup"}}))
    evidence.set_exclusions(w, ["10.1/NEW"], "", excluded=True)
    data = json.loads(f.read_text())
    # Written back bare, comment kept, old AND new entries both visible to
    # the pipeline's loader.
    assert "exclusions" not in data and data["_comment"] == "hand-kept"
    from ber_core.evidence import load_exclusion_dois
    assert load_exclusion_dois(f) == {"10.1/old", "10.1/new"}
    assert evidence._load_user_exclusions(f.parent) == {"10.1/old": "dup", "10.1/new": "user_excluded"}
    # No temp files left behind by the atomic write.
    assert [p.name for p in f.parent.iterdir()] == ["user_exclusions.json"]


async def test_a_corrupt_verdict_file_is_a_409_not_silently_empty():
    """Reading a broken user_exclusions.json as "no decisions" re-admitted every
    excluded paper, and the next exclude click overwrote (erased) it."""
    async with _client() as c:
        h, rid = await _review(c, "c14-corrupt@example.com", "C14 corrupt")
        async with AsyncSessionLocal() as db:
            ws_path = Path((await db.get(Review, rid)).workspace_path)
        f = ws_path / "projects" / "c14_corrupt" / "data" / "user_exclusions.json"
        f.write_text("{not json")
        r = await c.get(f"/reviews/{rid}/evidence", headers=h)
        assert r.status_code == 409 and "user_exclusions.json" in r.json()["detail"]
        r = await c.post(f"/reviews/{rid}/evidence/exclude", headers=h,
                         json={"dois": ["10.1/x"]})
        assert r.status_code == 409
        assert f.read_text() == "{not json"  # left alone for a human to fix


# --- C14: job admission returns only the SAME kind ----------------------------------

async def test_enqueue_never_returns_a_different_kind_of_job(no_queue):
    async with _client() as c:
        _, rid = await _review(c, "c14-kind@example.com", "C14 kind")
    async with AsyncSessionLocal() as db:
        db.add(Job(review_id=rid, kind="pipeline", state="running"))
        await db.commit()
        review = await db.get(Review, rid)
        with pytest.raises(HTTPException) as err:
            await jobs.enqueue(db, review, "synthesis", params={"depth": "brief"})
        assert err.value.status_code == 409
        # Background kinds coexist with it.
        job = await jobs.enqueue(db, review, "pdf")
        assert job.kind == "pdf" and job.queue == jobs.PDF_QUEUE_NAME


async def test_metadata_runs_on_the_non_llm_queue(no_queue):
    async with _client() as c:
        _, rid = await _review(c, "c1-meta@example.com", "C1 meta")
    async with AsyncSessionLocal() as db:
        review = await db.get(Review, rid)
        job = await jobs.enqueue(db, review, "metadata")
    assert job.queue == jobs.PDF_QUEUE_NAME
    assert no_queue.enqueue_job.call_args.kwargs["_queue_name"] == jobs.PDF_QUEUE_NAME
    from server.app import worker
    names = {f.name for f in worker.PdfWorkerSettings.functions}
    assert "enrich_metadata_task" in names and "run_pdf_task" in names


async def test_review_patch_rejects_unknown_provider():
    async with _client() as c:
        h, rid = await _review(c, "c14-prov@example.com", "C14 prov")
        r = await c.patch(f"/reviews/{rid}", headers=h, json={"llm_default_provider": "evil"})
        assert r.status_code == 422
        r = await c.patch(f"/reviews/{rid}", headers=h, json={"llm_default_provider": "openrouter"})
        assert r.status_code == 200


# --- C14: reconcile settles queued rows the queue dropped ---------------------------

async def test_reconcile_fails_a_queued_job_whose_arq_job_already_finished(monkeypatch):
    from arq.jobs import JobStatus

    async with _client() as c:
        _, rid = await _review(c, "c14-rec@example.com", "C14 rec")
    async with AsyncSessionLocal() as db:
        stuck = Job(review_id=rid, kind="pipeline", state="queued", arq_job_id="x")
        fresh = Job(review_id=rid, kind="pdf", state="queued", arq_job_id="y")
        db.add_all([stuck, fresh])
        await db.commit()
        ids = (stuck.id, fresh.id)

    statuses = {"x": JobStatus.complete, "y": JobStatus.queued}

    class FakeArqJob:
        def __init__(self, job_id, pool, _queue_name=None):
            self.job_id = job_id

        async def status(self):
            return statuses[self.job_id]

    import arq.jobs
    monkeypatch.setattr(arq.jobs, "Job", FakeArqJob)
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(return_value=SimpleNamespace()))
    async with AsyncSessionLocal() as db:
        await reconcile._sweep_orphans(db)
    async with AsyncSessionLocal() as db:
        assert (await db.get(Job, ids[0])).state == "failed"
        assert (await db.get(Job, ids[1])).state == "queued"


# --- C14: agent review idempotency is not limited to recent history -----------------

async def test_prior_review_is_found_beyond_the_recent_window():
    async with _client() as c:
        _, rid = await _review(c, "c14-agent@example.com", "C14 agent")
    async with AsyncSessionLocal() as db:
        db.add(AgentMessage(review_id=rid, role="assistant", content="old check",
                            meta={"kind": "review", "job_id": "job-old"}))
        await db.commit()
        db.add_all([AgentMessage(review_id=rid, role="user", content=f"m{i}")
                    for i in range(250)])
        await db.commit()
        prior = await agent_svc.prior_review(db, rid, "job-old")
        assert prior is not None and prior.content == "old check"
        assert await agent_svc.prior_review(db, rid, "job-new") is None


# --- C2: evidence reads ----------------------------------------------------------------

def test_abstract_is_found_by_name_without_listing_the_directory(tmp_path, monkeypatch):
    w = _data_workspace(tmp_path)
    abstracts = w / "projects" / "v" / "abstracts"
    abstracts.mkdir()
    (abstracts / "10.1000--ABC.123_abstract.md").write_text("# T\n\nBody text")
    calls = []
    real_iterdir = Path.iterdir

    def spy(self):
        calls.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", spy)
    assert evidence.paper_abstract(w, "10.1000/ABC.123") == "Body text"
    assert not [p for p in calls if p == abstracts]
    # A spelling that differs only in case still resolves (via the index).
    assert evidence.paper_abstract(w, "10.1000/abc.123") == "Body text"


def test_conversion_quality_trusts_a_recorded_table_count(tmp_path):
    paper = tmp_path / "p"
    paper.mkdir()
    (paper / "provenance.json").write_text(json.dumps(
        {"extraction_mode": "docling-html", "tables_captured": 2}))
    # No body.md at all: the recorded count answers without reading it.
    assert evidence._conversion_quality(paper) == ("docling-html", True)


def test_evidence_cache_sees_a_changed_input(tmp_path):
    w = _data_workspace(tmp_path)
    data = w / "projects" / "v" / "data"
    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,\n")
    assert evidence.build_evidence(w)["counts"]["total"] == 1
    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,\n10.1/b,\n")
    assert evidence.build_evidence(w)["counts"]["total"] == 2
    # Results are copies: a caller annotating rows does not poison the cache.
    first = evidence.build_evidence(w)
    first["rows"][0]["doi"] = "mutated"
    assert evidence.build_evidence(w)["rows"][0]["doi"] != "mutated"


def test_deleted_dir_lives_under_data_root():
    assert review_lifecycle.deleted_dir().parent == Path(settings.DATA_ROOT)
