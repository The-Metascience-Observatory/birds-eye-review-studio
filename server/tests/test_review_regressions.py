"""Isolated regressions; fake providers/queue and temporary data only."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from server.app.db.base import Base
from server.app.db.models import Job, Review, ReviewConfig, User, ReviewAutomation
from server.app.services import jobs, configuration, reconcile, workspace, publish_deploy, runner
from server.app.worker import _load_job_and_review


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    config = {"topic": {"name": "X", "synonyms": ["x"]}, "article_types": ["RCT"]}
    monkeypatch.setattr(workspace, "workspace_dir", lambda rid: tmp_path / rid)
    path = workspace.create_review_workspace("review", config)
    async with factory() as db:
        db.add(User(id="owner", email="fixture@example.test", password_hash="unused"))
        review = Review(id="review", owner_id="owner", title="X", slug="x", topic_name="X", workspace_path=str(path))
        db.add(review)
        db.add(ReviewConfig(review_id="review", config_json=config))
        await db.commit()
    yield factory, review, config
    await engine.dispose()


async def test_worker_sees_committed_job_and_cannot_reclaim_it(store, monkeypatch):
    factory, review, _ = store
    seen = []
    class Pool:
        async def enqueue_job(self, function, jid, **kwargs):
            async with factory() as worker:
                job, loaded_review = await _load_job_and_review(worker, jid)
                assert loaded_review.id == review.id
                seen.append(job.id)
                job.state = "succeeded"
                await worker.commit()
                assert await _load_job_and_review(worker, jid) == (None, None)
            assert kwargs["_job_id"] == jid
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(return_value=Pool()))
    async with factory() as db:
        job = await jobs.enqueue(db, review, "step", step_id="fixture")
        assert job.state == "succeeded" and seen == [job.id]


async def test_outbox_recovers_redis_outage_and_restart(store, monkeypatch):
    factory, review, _ = store
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(side_effect=ConnectionError("offline")))
    async with factory() as db:
        job = await jobs.enqueue(db, review, "step", step_id="fixture")
        assert job.state == "queued" and job.arq_job_id is None
    pool = SimpleNamespace(enqueue_job=AsyncMock())
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(return_value=pool))
    async with factory() as db:
        await jobs.dispatch_pending(db)
        await jobs.dispatch_pending(db)
        saved = await db.get(Job, job.id)
        assert saved.arq_job_id == saved.id
    assert [c.kwargs["_job_id"] for c in pool.enqueue_job.call_args_list] == [job.id, job.id]


async def test_concurrent_admission_creates_one_job(store, monkeypatch):
    factory, review, _ = store
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(return_value=SimpleNamespace(enqueue_job=AsyncMock())))
    async def enqueue():
        async with factory() as db:
            return (await jobs.enqueue(db, review, "step", step_id="fixture")).id
    first, second = await asyncio.gather(enqueue(), enqueue())
    assert first == second
    async with factory() as db:
        assert len((await db.execute(select(Job))).scalars().all()) == 1


async def test_reconciler_leaves_history_idle_and_attempts_work_once(store, monkeypatch):
    factory, review, _ = store
    monkeypatch.setattr(jobs, "get_arq_pool", AsyncMock(return_value=SimpleNamespace(enqueue_job=AsyncMock())))
    monkeypatch.setattr(reconcile.paper_metadata, "load_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(reconcile.evidence, "build_evidence", lambda *a, **kw: {"rows": [
        {"doi": "10.1000/x", "metadata_source": "found", "relevance": "yes", "screen_state": "pending", "has_fulltext": False}]})
    async with factory() as db:
        await reconcile.reconcile_once(db)
        assert not (await db.execute(select(Job))).scalars().all()
        db.add(ReviewAutomation(review_id=review.id)); await db.commit()
        await reconcile.reconcile_once(db)
        job = (await db.execute(select(Job))).scalars().one()
        assert job.kind == "pdf"
        job.state = "failed"; await db.commit()
        await reconcile.reconcile_once(db)
        assert len((await db.execute(select(Job))).scalars().all()) == 1


async def test_config_stale_draft_and_rename_are_rejected(store):
    factory, review, config = store
    async with factory() as db:
        await configuration.save_config(db, review, {"description": "new"}, merge=True)
        with pytest.raises(HTTPException) as error:
            await configuration.save_config(db, review, config, expected_version=1)
        assert error.value.status_code == 409
        await db.rollback()
        with pytest.raises(HTTPException) as error:
            await configuration.save_config(db, review, {**config, "topic": {"name": "Renamed"}})
        assert error.value.status_code == 422
    assert "new" in (Path(review.workspace_path) / "config.yaml").read_text()


async def test_failed_config_commit_preserves_yaml(store, monkeypatch):
    factory, review, _ = store
    path = Path(review.workspace_path) / "config.yaml"
    before = path.read_bytes()
    async with factory() as db:
        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=RuntimeError("database unavailable")))
        with pytest.raises(RuntimeError):
            await configuration.save_config(db, review, {"description": "not committed"}, merge=True)
    assert path.read_bytes() == before


def test_approval_checks_staged_content(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_deploy.settings, "WEBSITE_DIR", tmp_path / "site")
    ws = tmp_path / "workspace"; ws.mkdir()
    (ws / "config.yaml").write_text("topic:\n  name: X\n")
    bundle = ws / "projects/x/dashboard"; bundle.mkdir(parents=True)
    (bundle / "summary.json").write_text(json.dumps({"totals": {"n_trials": 1}}))
    (bundle / "trial_extractions.jsonl").write_text('{"paper_id":"10.1000/x"}\n')
    token = publish_deploy.confirm_token(ws, "x", None)
    (bundle / "meta_analysis.json").write_text('{"changed":true}')
    with pytest.raises(ValueError, match="changed"):
        publish_deploy.deploy(ws, "x", jobtag="fixture", expected_token=token)
    assert not publish_deploy.deployed_dir("x").exists()


async def test_subprocess_does_not_block_event_loop(tmp_path, monkeypatch):
    import time
    ticks = []
    def slow(*a, **kw):
        time.sleep(.04)
        assert ticks
        return 0, None, .04, None
    monkeypatch.setattr(runner, "run_subprocess", slow)
    async def heartbeat():
        await asyncio.sleep(.01)
        ticks.append(True)
    await asyncio.gather(runner.run_subprocess_async(None, tmp_path, "claude_cli", tmp_path / "log"), heartbeat())


async def test_config_change_is_blocked_while_artifact_job_runs(store):
    factory, review, _ = store
    async with factory() as db:
        db.add(Job(review_id=review.id, kind="pipeline", state="running")); await db.commit()
        with pytest.raises(HTTPException) as error:
            await configuration.save_config(db, review, {"description": "changed"}, merge=True)
        assert error.value.status_code == 409


async def test_publish_commit_failure_restores_live_snapshot(store, tmp_path, monkeypatch):
    from server.app.services import publishing
    factory, review, _ = store
    live = tmp_path / "live"; live.mkdir()
    (live / "feed").write_text("previous")
    monkeypatch.setattr(publishing.deploy_svc, "deployed_dir", lambda _: live)
    async def pretend_deploy(*a, **kw):
        (live / "feed").write_text("new")
        return object()
    monkeypatch.setattr(publishing, "_approve_and_deploy_locked", pretend_deploy)
    async with factory() as db:
        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
        with pytest.raises(RuntimeError):
            await publishing.approve_and_deploy(db, SimpleNamespace(review_id=review.id), None,
                confirm_token="fixture", route="x", allow_shrink=False)
    assert (live / "feed").read_text() == "previous"


async def test_worker_cancellation_reaches_subprocess_flag(tmp_path, monkeypatch):
    import time
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    cancel = tmp_path / "cancel"
    def wait_for_stop(*a, **kw):
        loop.call_soon_threadsafe(started.set)
        deadline = time.monotonic() + 2
        while not cancel.exists() and time.monotonic() < deadline:
            time.sleep(.005)
        assert cancel.exists()
        return 130, "cancelled", 0, None
    monkeypatch.setattr(runner, "run_subprocess", wait_for_stop)
    task = asyncio.create_task(runner.run_subprocess_async(None, tmp_path, "claude_cli", tmp_path / "log", cancel_path=cancel))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancel.exists()


def test_workspace_lock_fails_fast_when_another_job_holds_it(tmp_path):
    """C1: the lock never WAITS. A blocking wait inside the single-slot LLM
    worker parked that slot for the whole of another job's PDF fetch, stalling
    every other user's LLM work."""
    import fcntl
    import time

    lock = tmp_path / "_jobs/.execution.lock"
    lock.parent.mkdir()
    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(runner.WorkspaceBusy):
            with runner.workspace_lock(tmp_path):
                pass  # pragma: no cover - must not be entered
        assert time.monotonic() - started < 0.5
        fcntl.flock(holder, fcntl.LOCK_UN)
    with runner.workspace_lock(tmp_path):
        pass  # free again -> acquired


async def _queued_job(factory, review, kind="pipeline"):
    async with factory() as db:
        job = Job(review_id=review.id, kind=kind, state="queued")
        db.add(job)
        await db.commit()
        return job.id


async def test_worker_defers_a_job_whose_workspace_is_busy(store, monkeypatch):
    """The job goes BACK to queued and arq is asked to redeliver it later —
    the worker slot is released instead of held for the other job."""
    import fcntl
    from arq.worker import Retry
    from server.app import worker

    factory, review, _ = store
    job_id = await _queued_job(factory, review)
    ran = []
    monkeypatch.setattr(runner, "execute_pipeline", AsyncMock(side_effect=lambda *a, **k: ran.append(1)))
    lock = Path(review.workspace_path) / "_jobs/.execution.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(Retry):
            await worker.run_pipeline_task({"sessionmaker": factory}, job_id)
        fcntl.flock(holder, fcntl.LOCK_UN)
    assert not ran
    async with factory() as db:
        assert (await db.get(Job, job_id)).state == "queued"


async def test_worker_defers_llm_job_while_another_user_holds_the_cli(store, monkeypatch):
    """S4: the lease is checked when the job STARTS, not only at the endpoint —
    reconciler-fired jobs never pass through an endpoint."""
    from arq.worker import Retry
    from server.app import worker
    from server.app.services import llm_lease

    factory, review, _ = store
    async with factory() as db:
        db.add(User(id="other", email="other@example.test", password_hash="unused"))
        owner = await db.get(User, "owner")
        owner.llm_provider = "claude_cli"
        await db.commit()
        await llm_lease.acquire(db, "other", None)
    job_id = await _queued_job(factory, review)
    monkeypatch.setattr(runner, "execute_pipeline", AsyncMock())
    with pytest.raises(Retry):
        await worker.run_pipeline_task({"sessionmaker": factory}, job_id)
    runner.execute_pipeline.assert_not_called()
    async with factory() as db:
        assert (await db.get(Job, job_id)).state == "queued"


async def test_worker_refuses_host_login_for_non_admin_on_public_instance(store, monkeypatch):
    """PUBLIC_REQUIRE_BYO_LLM: waiting would never fix it, so the job FAILS
    with the explanation instead of deferring forever."""
    from server.app import worker
    from server.app.services import llm_access

    factory, review, _ = store
    async with factory() as db:
        owner = await db.get(User, "owner")
        owner.llm_provider = "codex_cli"
        await db.commit()
    monkeypatch.setattr(llm_access.settings, "PUBLIC_MODE", True)
    monkeypatch.setattr(llm_access.settings, "PUBLIC_REQUIRE_BYO_LLM", True)
    job_id = await _queued_job(factory, review)
    monkeypatch.setattr(runner, "execute_pipeline", AsyncMock())
    assert await worker.run_pipeline_task({"sessionmaker": factory}, job_id) == "refused"
    runner.execute_pipeline.assert_not_called()
    async with factory() as db:
        job = await db.get(Job, job_id)
        assert job.state == "failed" and "credential" in job.error


async def test_metadata_task_does_not_overwrite_a_cancel(store, monkeypatch):
    """C10: a Stop pressed while enrichment ran must stay cancelled."""
    from server.app import worker
    from server.app.services import paper_metadata

    factory, review, _ = store
    job_id = await _queued_job(factory, review, kind="metadata")

    async def enrich(session, rid, ws):
        async with factory() as other:
            row = await other.get(Job, job_id)
            row.state = "cancelled"
            await other.commit()
        return {"enriched": 0}

    monkeypatch.setattr(paper_metadata, "enrich_review", enrich)
    await worker.enrich_metadata_task({"sessionmaker": factory}, job_id)
    async with factory() as db:
        assert (await db.get(Job, job_id)).state == "cancelled"


async def test_extraction_download_scope_uses_latest_screening(store, monkeypatch):
    from ber_core.pipeline import Step
    factory, review, _ = store
    states = ["screened_out"]
    seen = []
    monkeypatch.setattr(reconcile.evidence, "build_evidence", lambda *a, **kw: {"rows": [
        {"doi": "10.1000/wanted", "screen_state": states[0]},
        {"doi": "10.1000/excluded", "screen_state": "excluded"}]})
    monkeypatch.setattr(runner.pipeline_bridge, "canonical_steps_for", lambda *a, **kw: [
        Step("3_classify", 3, "screen", "fixture", []),
        Step("9_pass2_rct", 9, "extract", "fixture", [])])
    monkeypatch.setattr(runner.llm_prefs, "resolve_for_run", AsyncMock(return_value=("claude_cli", {})))

    async def run(step, *a, **kw):
        if step.id == "3_classify":
            states[0] = "pending"
        if step.id == "5_download_pdfs":
            assert "--only-dois" in step.args
            scope = Path(step.args[step.args.index("--only-dois") + 1])
            seen.extend(scope.read_text().splitlines()[1:])
        return 0, None, 0, None

    monkeypatch.setattr(runner, "run_subprocess_async", run)
    async with factory() as db:
        job = Job(review_id=review.id, kind="pipeline", state="queued")
        db.add(job); await db.commit()
        await runner.execute_pipeline(db, job, review)
        assert job.state == "succeeded"
    assert seen == ["10.1000/wanted"]
