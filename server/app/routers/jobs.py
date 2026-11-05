"""Job endpoints: enqueue step/pipeline runs, read jobs, stream logs.

All endpoints are owner-checked (admin bypass) through ``deps.owned_review`` /
``deps.owned_job``. Enqueue goes through :mod:`services.jobs`, which uses a
lazily-cached arq pool (NOT ``app.state``) so it works under ASGITransport tests
too.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.models import Job, Review, ReviewConfig, User
from ..deps import get_current_user, get_db, owned_job, owned_review
from ..ratelimit import llm_limit
from ..schemas import JobOut, RunPipelineIn
from ..services import jobs as jobs_svc
from ..services import llm_access
from ..services import redact
from ..services import stage_gates as gates_svc
from ..services.pipeline_bridge import preflight
from ..services.stage_view import find_stage, resolved_with_gates

router = APIRouter(tags=["jobs"])

_TERMINAL = {"succeeded", "failed", "cancelled"}


async def reject_if_busy(db: AsyncSession, review_id: str) -> None:
    """409 when the review already has a queued/running job.

    Concurrent runs on one review share a workspace and corrupt each other's
    resume logic — the second run sees the first's half-written CSVs and skips
    work it should have done. There is no safe interleaving, so refuse rather
    than queue.
    """
    existing = await jobs_svc.active_job(db, review_id)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This review already has a {existing.state} job ({existing.id}). "
            "Wait for it to finish or cancel it before starting another — two "
            "runs on one review overwrite each other's outputs.",
        )


@router.post(
    "/reviews/{review_id}/steps/{step_id}/run",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(llm_limit)],
)
async def run_step(
    step_id: str,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    await reject_if_busy(db, review.id)
    await llm_access.ensure_allowed(db, review, requester=user)
    return await jobs_svc.enqueue(db, review, "step", step_id=step_id)


@router.post(
    "/reviews/{review_id}/run",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(llm_limit)],
)
async def run_pipeline(
    payload: RunPipelineIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    # `only=[]` is falsy in pipeline.select_steps, which treats it as "no filter"
    # and would run the ENTIRE pipeline — an expensive, wallet-emptying surprise
    # for a caller that meant "run these zero selected stages". Reject it here so
    # the guard doesn't depend on every client remembering.
    if payload.only is not None and not payload.only:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "`only` is an empty list — that selects no stages. Omit `only` to "
            "run the whole pipeline, or name the canonical step numbers to run.",
        )
    await reject_if_busy(db, review.id)
    await llm_access.ensure_allowed(db, review, requester=user)
    return await jobs_svc.enqueue(db, review, "pipeline", params=payload.model_dump())


@router.post(
    "/reviews/{review_id}/stages/{slug}/run",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(llm_limit)],
)
async def run_stage(
    slug: str,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Run exactly one wizard stage.

    Selects by canonical step ID, never by number: nums are gappy and SHARED
    between steps, so they cannot name a stage's work.
    """
    resolved = await resolved_with_gates(db, review)
    stage = find_stage(resolved, slug)

    ok, reason = gates_svc.can_run(stage)
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)

    if not stage["step_ids"]:
        # Either a checkpoint stage (setup/triage/papers/meta) or one skipped for
        # this review. Running it is meaningless — and an empty selection must
        # never fall through to "run everything".
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{stage['title']!r} runs no pipeline steps for this review — "
            f"there is nothing to run. Approve it instead.",
        )

    cfg = await db.get(ReviewConfig, review.id)
    issues = preflight(
        Path(review.workspace_path),
        (cfg.config_json if cfg else {}) or {},
        stage_slug=slug,
    )
    errors = [i for i in issues if i.get("level") == "error"]
    if errors:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Fix the configuration problems first: "
            + "; ".join(i.get("message", "") for i in errors[:3]),
        )

    await reject_if_busy(db, review.id)
    await llm_access.ensure_allowed(db, review, requester=user)
    return await jobs_svc.enqueue(
        db, review, "pipeline", params={"only_ids": list(stage["step_ids"])})


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job: Job = Depends(owned_job),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Stop a queued or running job.

    Necessary, not a convenience: `reject_if_busy` means one stuck job blocks
    EVERY run on that review, so without this a crashed worker leaves the review
    permanently unusable from the UI.
    """
    if job.state in _TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Job is already {job.state}; there is nothing to cancel.")
    await jobs_svc.stop_job(db, job, "Cancelled by user.")
    await db.refresh(job)
    return job


@router.get("/reviews/{review_id}/jobs", response_model=list[JobOut])
async def list_jobs(
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> list[Job]:
    result = await db.execute(
        select(Job)
        .where(Job.review_id == review.id)
        .order_by(Job.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job: Job = Depends(owned_job)) -> JobOut:
    out = JobOut.model_validate(job)
    # Overlay the live current-step progress (tqdm/[i/N]) the worker writes to a
    # sidecar file while a step runs — the DB row only updates between steps.
    if job.state == "running" and job.log_path:
        side = Path(job.log_path).with_suffix(".progress.json")
        try:
            live = json.loads(side.read_text(encoding="utf-8"))
            prog = dict(out.progress or {})
            prog["live"] = live
            out.progress = prog
        except (OSError, ValueError):
            pass
    return out


@router.get("/jobs/{job_id}/logs")
async def stream_job_logs(job: Job = Depends(owned_job)) -> StreamingResponse:
    """Server-Sent Events: tail the job log, then emit a terminal ``end`` event.

    Re-fetches the Job each poll from a fresh session (the running worker mutates
    it in another process/session), so terminal state is observed correctly.

    A ``: ping`` comment goes out every ``SSE_PING_SECONDS`` of silence so an
    idle stream (a long LLM step that prints nothing) is not cut by a proxy, and
    the stream ends after ``SSE_MAX_SECONDS`` with an ``event: timeout`` — the
    client reconnects — so an abandoned tab cannot hold a server task forever.
    """
    job_id = job.id
    log_path = job.log_path

    async def _events():
        nonlocal log_path
        pos = 0
        started = last_sent = time.monotonic()

        def _read_new() -> list[str]:
            nonlocal pos
            if not log_path:
                return []
            p = Path(log_path)
            if not p.exists():
                return []
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            # Lines are scrubbed as they are written; scrub again on the way
            # out so a log written before that existed cannot hand out a key.
            return [redact.redact(line) for line in chunk.splitlines()]

        while True:
            for line in _read_new():
                last_sent = time.monotonic()
                yield f"data: {line}\n\n"
            # Re-read job state from a fresh session (worker owns the row). Also
            # refresh log_path: a stream opened while the job is still QUEUED has
            # log_path=None until the runner sets it — without this refresh such a
            # stream would never emit a single log line.
            from ..db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as s:
                cur = await s.get(Job, job_id)
                state = cur.state if cur else "failed"
                if cur and cur.log_path and not log_path:
                    log_path = cur.log_path
            if state in _TERMINAL:
                # Flush any final bytes written after the last read.
                for line in _read_new():
                    yield f"data: {line}\n\n"
                yield f"event: end\ndata: {state}\n\n"
                return
            now = time.monotonic()
            if now - started >= settings.SSE_MAX_SECONDS:
                yield "event: timeout\ndata: reconnect\n\n"
                return
            if now - last_sent >= settings.SSE_PING_SECONDS:
                last_sent = now
                yield ": ping\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(_events(), media_type="text/event-stream")
