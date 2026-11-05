"""Background full-text eligibility screen (pass 1 + stage) as a `fulltext_screen` job.

The screen runs on the LLM queue (it's a sonnet step) but is still a BACKGROUND
job — like metadata + the PDF fetch, it must be EXEMPT from the one-job-per-review
guard so it can auto-fire while the user works. These tests pin the exemption, the
no-stacking idempotency, and that the runner builds the two off-range scripts
(extract_pass1 → stage_eligible) without needing them in canonical_steps().
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_ftscreen_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from server.app.db.models import Job, Review  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import jobs, runner  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email):
    await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "FT Screen", "topic_name": "FT Screen",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


async def test_a_fulltext_screen_job_does_not_count_as_busy() -> None:
    """A running full-text screen must NOT block a pipeline run: it writes
    trial_extractions.jsonl + the eligible_*/ buckets — files no canonical step
    writes (pass-1 left the range) — and is resumable."""
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "ftbusy@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="fulltext_screen", state="running"))
            await db.commit()
            assert await jobs.active_job(db, rid) is None

            # A running PIPELINE job still IS busy.
            db.add(Job(review_id=rid, kind="pipeline", state="running"))
            await db.commit()
            assert (await jobs.active_job(db, rid)) is not None


async def test_screen_does_not_stack_duplicate_jobs() -> None:
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "ftdup@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="fulltext_screen", state="queued"))
            await db.commit()
            review = await db.get(Review, rid)
            await jobs.enqueue(db, review, "fulltext_screen")
            n = len((await db.execute(
                select(Job).where(Job.review_id == rid))).scalars().all())
            assert n == 1, "duplicate fulltext_screen job was created"


def test_fulltext_screen_steps_are_pass1_then_stage(tmp_path) -> None:
    """The runner builds the two off-canonical scripts in order: the pass-1
    eligibility screen, then staging — no dependency on canonical_steps() (they
    left the range). stage_eligible declares no produces (empty buckets are OK)."""
    ws = tmp_path
    (ws / "config.yaml").write_text("topic:\n  name: FT Screen\n", encoding="utf-8")
    steps = runner._fulltext_screen_steps(ws)
    assert [s.id for s in steps] == ["8_extract_pass1", "stage_eligible"]
    assert steps[0].script == "extract_pass1.py"
    assert steps[1].script == "stage_eligible.py"
    assert steps[0].produces[0].endswith("trial_extractions.jsonl")
    assert steps[1].produces == []  # empty buckets are legitimate
    # stage_eligible runs with --execute so it actually creates the symlinks.
    assert "--execute" in steps[1].args
