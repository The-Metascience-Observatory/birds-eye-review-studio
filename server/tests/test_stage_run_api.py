"""Running ONE stage, and cancelling a job.

Two hazards drive these tests.

First, selecting a stage's work by canonical step NUMBER is impossible: nums are
gappy and SHARED (num 3 covers several steps), so `only=[3]` cannot mean "run the
classify stage". Worse, `pipeline.select_steps` treats an empty `only` as "no
filter" and runs the ENTIRE pipeline — a wallet-emptying surprise for a caller
that resolved zero steps. `select_step_ids` has no such footgun, and the stage-run
endpoint must never be able to fall through to a full run.

Second, `_reject_if_busy` means one stuck job blocks EVERY run on that review. A
cancel endpoint is therefore load-bearing, not a convenience: without it a
crashed worker leaves the review permanently unusable from the UI.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_stagerun_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402

from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.schemas import RunPipelineIn  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email: str) -> tuple[dict, str]:
    await client.post("/auth/register",
                      json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login",
                          json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Run Test", "topic_name": "Run Test",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


# --- the selector contract (no HTTP needed) ----------------------------------

def test_only_and_only_ids_cannot_both_be_sent() -> None:
    """Two selectors over the same steps would silently disagree about which
    won. Reject rather than pick."""
    with pytest.raises(ValueError):
        RunPipelineIn(only=[1], only_ids=["1_search"])


def test_each_selector_alone_is_fine() -> None:
    assert RunPipelineIn(only=[1]).only == [1]
    assert RunPipelineIn(only_ids=["1_search"]).only_ids == ["1_search"]
    assert RunPipelineIn().only is None


def test_an_empty_id_selection_selects_nothing() -> None:
    """The whole reason to use select_step_ids: `select_steps` would treat an
    empty list as "no filter" and run the entire pipeline."""
    import sys

    REPO = Path(__file__).resolve().parents[2]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from ber_core.pipeline import select_step_ids, select_steps

    class _S:
        def __init__(self, i, n):
            self.id, self.num = i, n

    steps = [_S("a", 1), _S("b", 2)]
    assert select_step_ids(steps, []) == []
    # The footgun, pinned so nobody "simplifies" the endpoint to use it:
    assert len(select_steps(steps, only=[])) == 2


# --- running a stage ----------------------------------------------------------

async def test_a_checkpoint_stage_cannot_be_run() -> None:
    """`setup` produces no pipeline steps. Running it is meaningless — and an
    empty step list must never reach the runner, where it could mean "all"."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "run1@example.com")
        r = await client.post(f"/reviews/{rid}/stages/setup/run", headers=headers)
        assert r.status_code == 400, r.text
        assert "nothing to run" in r.json()["detail"]


async def test_a_blocked_stage_refuses_to_run_and_names_the_blocker() -> None:
    """Hard gate: work must not start on unreviewed upstream output."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "run2@example.com")
        # `search` is gated behind `setup`, which is unapproved on a new review.
        r = await client.post(f"/reviews/{rid}/stages/search/run", headers=headers)
        assert r.status_code == 409, r.text
        assert "setup" in r.json()["detail"].lower()


async def test_an_unknown_stage_is_404() -> None:
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "run3@example.com")
        r = await client.post(f"/reviews/{rid}/stages/nope/run", headers=headers)
        assert r.status_code == 404, r.text


async def test_another_users_review_cannot_be_run() -> None:
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "runowner@example.com")
        other, _ = await _setup(client, "runintruder@example.com")
        r = await client.post(f"/reviews/{rid}/stages/search/run", headers=other)
        assert r.status_code == 403, r.text


# --- cancel -------------------------------------------------------------------

async def test_cancelling_a_nonexistent_job_is_404() -> None:
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "cancel1@example.com")
        r = await client.post("/jobs/deadbeef/cancel", headers=headers)
        assert r.status_code == 404, r.text


async def test_cancel_unblocks_the_review() -> None:
    """The property that makes cancel load-bearing: after cancelling, the review
    accepts a new run. Without it a stuck job blocks the review forever."""
    await init_models()
    from sqlalchemy import select

    from server.app.db.models import Job
    from server.app.db.session import AsyncSessionLocal

    async with _client() as client:
        headers, rid = await _setup(client, "cancel2@example.com")

        # Simulate a stuck job directly: enqueueing would need a live redis.
        async with AsyncSessionLocal() as s:
            s.add(Job(review_id=rid, kind="pipeline", state="running"))
            await s.commit()
            job_id = (
                await s.execute(select(Job.id).where(Job.review_id == rid))
            ).scalars().first()

        # Approve setup so the GATE is open and the busy-check is what we hit.
        # (The endpoint checks the gate first — it is cheaper and its message is
        # more useful — so a blocked stage never reaches the busy check.)
        await client.post(f"/reviews/{rid}/stages/setup/approve", headers=headers)

        r = await client.post(f"/reviews/{rid}/run", headers=headers, json={})
        assert r.status_code == 409
        assert "already has a running job" in r.json()["detail"]

        r = await client.post(f"/jobs/{job_id}/cancel", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "cancelled"

        # The review is unblocked: `active_job` no longer finds anything, which
        # is exactly what `_reject_if_busy` consults. Asserted directly rather
        # than by issuing another run — enqueueing reaches for redis, whose
        # cached pool is bound to another test's event loop.
        from server.app.services.jobs import active_job

        async with AsyncSessionLocal() as s:
            assert await active_job(s, rid) is None


async def test_cancelling_a_finished_job_is_refused() -> None:
    await init_models()
    from sqlalchemy import select

    from server.app.db.models import Job
    from server.app.db.session import AsyncSessionLocal

    async with _client() as client:
        headers, rid = await _setup(client, "cancel3@example.com")
        async with AsyncSessionLocal() as s:
            s.add(Job(review_id=rid, kind="pipeline", state="succeeded"))
            await s.commit()
            job_id = (
                await s.execute(select(Job.id).where(Job.review_id == rid))
            ).scalars().first()

        r = await client.post(f"/jobs/{job_id}/cancel", headers=headers)
        assert r.status_code == 409, r.text
        assert "already succeeded" in r.json()["detail"]
