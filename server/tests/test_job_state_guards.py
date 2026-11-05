"""Job-state guards: live-step detection and terminal-state writes.

Two regressions with zero prior coverage:

1. `_live_steps` must read the CURRENT step from the top-level
   `progress["step_id"]` the runner actually commits — it used to read only the
   nested `progress["live"]["step_id"]` shape (which exists solely in
   GET /jobs/{id} responses), so `activity.running_job_id` was null for the
   entire duration of every pipeline step.

2. `_finish_job` must not clobber an intervening cancel: `cancel_job` commits
   `state="cancelled"` from the request session while the worker session
   (expire_on_commit=False) still holds "running" in memory; the runner used to
   overwrite it with "failed" when the killed subprocess exited -15.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio

from server.app.db.models import Job, Review, User
from server.app.db.session import AsyncSessionLocal, init_models
from server.app.services.runner import _finish_job
from server.app.services.stage_view import _live_steps

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def _init_db():
    await init_models()


async def _make_review(session) -> Review:
    user = User(email=f"guards-{uuid4().hex}@t.test", password_hash="x")
    session.add(user)
    await session.flush()
    review = Review(
        owner_id=user.id, slug="guards", topic_name="t", title="t",
        workspace_path="/tmp/nonexistent-guards")
    session.add(review)
    await session.commit()
    return review


async def test_live_steps_reads_top_level_step_id():
    async with AsyncSessionLocal() as session:
        review = await _make_review(session)
        job = Job(
            review_id=review.id, kind="pipeline", state="running",
            progress={"current": 2, "total": 3, "step_id": "9_pass2_rct",
                      "step_label": "Extract pass 2 — RCTs"})
        session.add(job)
        await session.commit()

        job_id, live = await _live_steps(session, review.id)
        assert job_id == job.id
        assert "9_pass2_rct" in live


async def test_live_steps_still_accepts_nested_live_shape():
    async with AsyncSessionLocal() as session:
        review = await _make_review(session)
        job = Job(
            review_id=review.id, kind="pipeline", state="running",
            progress={"live": {"step_id": "3_classify"}})
        session.add(job)
        await session.commit()

        _, live = await _live_steps(session, review.id)
        assert "3_classify" in live


async def test_finish_job_does_not_clobber_cancel():
    async with AsyncSessionLocal() as session:
        review = await _make_review(session)
        job = Job(review_id=review.id, kind="pipeline", state="running")
        session.add(job)
        await session.commit()
        job_id = job.id

        # A cancel lands from ANOTHER session (the API request) while this
        # session still holds state="running" in memory.
        async with AsyncSessionLocal() as other:
            cancelled = await other.get(Job, job_id)
            cancelled.state = "cancelled"
            cancelled.error = "Cancelled by user."
            await other.commit()

        assert job.state == "running"  # stale in-memory value, by construction
        await _finish_job(session, job, error="step x exited -15")

    async with AsyncSessionLocal() as check:
        row = await check.get(Job, job_id)
        assert row.state == "cancelled"
        assert row.error == "Cancelled by user."


async def test_finish_job_normal_failure_and_success():
    async with AsyncSessionLocal() as session:
        review = await _make_review(session)
        job = Job(review_id=review.id, kind="step", state="running")
        session.add(job)
        await session.commit()
        await _finish_job(session, job, error="step x exited 1")
        assert job.state == "failed" and job.error == "step x exited 1"
        assert job.ended_at is not None

        job2 = Job(review_id=review.id, kind="step", state="running")
        session.add(job2)
        await session.commit()
        await _finish_job(session, job2)
        assert job2.state == "succeeded" and job2.ended_at is not None
