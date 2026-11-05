"""Enqueue service + lazily-cached arq pool accessor.

Requests enqueue jobs through :func:`get_arq_pool`, NOT ``app.state`` — so the
enqueue path works under ASGITransport tests (which skip the app lifespan) and
under a normal server alike.

Two queues (see ``worker.py``): the LLM queue (``ber:default``, one job at a
time, because LLM work routes through one shared CLI login) and the non-LLM
queue (``ber:pdf``) for the network/conversion background kinds, so a long PDF
fetch or a metadata crawl never occupies the LLM slot.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy import select, update

from ..config import settings
from ..db.models import Job, Review, ReviewAutomation

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("queued", "running")

# Kinds EXEMPT from the one-job-per-review guard. They may run beside a
# pipeline step: metadata only writes its own DB table, and pdf/fulltext_screen
# take the review's workspace lock in the worker, so they never interleave with
# another job's writes to the same artifacts.
BACKGROUND_KINDS = frozenset({"metadata", "pdf", "fulltext_screen"})

# Must match worker.WorkerSettings.queue_name / PdfWorkerSettings.queue_name.
QUEUE_NAME = "ber:default"
PDF_QUEUE_NAME = "ber:pdf"
# Kinds that use no LLM and therefore never belong on the single LLM slot.
_PDF_QUEUE_KINDS = frozenset({"pdf", "metadata"})

# arq function name per job kind (registered in worker.py).
_TASKS = {
    "step": "run_step_task",
    "pipeline": "run_pipeline_task",
    "metadata": "enrich_metadata_task",
    "pdf": "run_pdf_task",
    "fulltext_screen": "run_fulltext_screen_task",
    "synthesis": "run_synthesis_task",
}


def queue_for(kind: str) -> str:
    """The arq queue a job of *kind* is dispatched to."""
    return PDF_QUEUE_NAME if kind in _PDF_QUEUE_KINDS else QUEUE_NAME


async def active_job(session, review_id: str) -> Job | None:
    """The review's queued/running NON-background job, if any.

    Two jobs on ONE review share a workspace and race each other's artifacts:
    the second reads files the first is still writing, so a resumable step sees
    partial output as "already done" and skips real work. (Observed in practice:
    a duplicate run classified 0 abstracts because the first run's CSV was
    already on disk.) Callers use this to refuse the second enqueue.
    """
    result = await session.execute(
        select(Job)
        .where(
            Job.review_id == review_id,
            Job.state.in_(ACTIVE_STATES),
            Job.kind.notin_(tuple(BACKGROUND_KINDS)),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


# Module-level cache: one ArqRedis pool reused across requests in a process.
_pool = None


async def get_arq_pool():
    """Lazily create + cache the ArqRedis pool from ``settings.REDIS_URL``."""
    global _pool
    if _pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        _pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    return _pool


async def close_arq_pool() -> None:
    """Close + clear the cached pool (called on app shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue(
    session,
    review: Review,
    kind: str,
    *,
    step_id: str | None = None,
    params: dict | None = None,
    fingerprint: str | None = None,
) -> Job | None:
    """Create a queued Job of *kind* for *review* and dispatch it to its queue.

    Idempotent: an ACTIVE job of the same kind (and step) is returned instead of
    stacking a duplicate — every job is resumable, so the running one picks up
    whatever changed. An active NON-background job of a DIFFERENT kind is a 409:
    the two would race on the review's workspace.

    *fingerprint* (background kinds fired by the reconciler) makes "this exact
    work was already attempted" a no-op returning ``None``; a manual retry
    without a fingerprint always runs.
    """
    if kind not in _TASKS:
        raise ValueError(f"unknown job kind {kind!r}")
    job_params = dict(params or {})
    if fingerprint:
        job_params["input_fingerprint"] = fingerprint
    job = Job(
        review_id=review.id,
        kind=kind,
        step_id=step_id,
        params=job_params or None,
        state="queued",
        queue=queue_for(kind),
    )
    return await _persist_and_dispatch(session, job)


async def dispatch_pending(session, job=None):
    """Job rows are a durable outbox; deterministic Redis IDs make retries safe.

    A crash after Redis accepts a job but before this commit is harmless: arq
    rejects a duplicate ID. The API lifespan retries undispatched rows.
    """
    pending = [job] if job is not None else list((await session.execute(
        select(Job).where(Job.state == "queued")
    )).scalars())
    for item in pending:
        if item.state != "queued":
            continue
        try:
            pool = await get_arq_pool()
            await pool.enqueue_job(_TASKS[item.kind], item.id, _job_id=item.id,
                                   _queue_name=queue_for(item.kind))
        except Exception as exc:
            logger.warning("Dispatch deferred for %s (%s)", item.id, type(exc).__name__)
            continue
        # Update only dispatch state, never overwrite a fast worker's result.
        await session.execute(update(Job).where(Job.id == item.id).values(arq_job_id=item.id))
        await session.commit()


async def _persist_and_dispatch(session, job):
    # A write lock serializes admission in SQLite and locks the review row on
    # Postgres. Repeat the active check inside that lock (HTTP checks can race).
    await session.execute(update(Review).where(Review.id == job.review_id).values(
        updated_at=Review.updated_at))
    q = select(Job).where(Job.review_id == job.review_id, Job.state.in_(ACTIVE_STATES))
    if job.kind in BACKGROUND_KINDS:
        q = q.where(Job.kind == job.kind)
    else:
        q = q.where(Job.kind.notin_(tuple(BACKGROUND_KINDS)))
    existing = (await session.execute(q.limit(1))).scalars().first()
    if existing is not None:
        await session.commit()
        if existing.kind == job.kind and existing.step_id == job.step_id:
            return existing
        # Never hand back a job of a different kind as if it were this one: the
        # caller would report (and poll) work it never asked for.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This review already has a {existing.state} {existing.kind} job "
            f"({existing.id}). Wait for it to finish or cancel it first — two "
            "runs on one review overwrite each other's outputs.",
        )
    fingerprint = (job.params or {}).get("input_fingerprint")
    if fingerprint:
        previous = (await session.execute(select(Job).where(
            Job.review_id == job.review_id, Job.kind == job.kind))).scalars()
        if any((prior.params or {}).get("input_fingerprint") == fingerprint for prior in previous):
            await session.commit()
            return None  # already attempted this exact work; manual retry remains available
    if await session.get(ReviewAutomation, job.review_id) is None:
        session.add(ReviewAutomation(review_id=job.review_id))
    session.add(job)
    await session.commit()  # worker must be able to see the job before Redis can
    await session.refresh(job)
    await dispatch_pending(session, job)
    await session.refresh(job)
    return job


async def stop_job(session, job: Job, reason: str) -> None:
    """Mark *job* cancelled, then stop whatever is executing it.

    The row is committed FIRST: it is the source of truth that unblocks the
    review, and the worker's own cleanup (``runner._finish_job`` /
    ``worker._fail_job``) re-reads it and leaves a cancelled job cancelled.
    Then, best-effort:

    * the per-job cancel flag, which the worker's watcher thread polls
      (``runner.run_subprocess``) and answers by killing the step's whole
      process group — including in-flight LLM CLI calls;
    * an arq abort, which removes a job still waiting in (or deferred to) the
      queue, and cancels a running task (the workers allow aborts).
    """
    from pathlib import Path

    from .._time import utcnow

    job.state = "cancelled"
    job.ended_at = utcnow()
    job.error = job.error or reason
    await session.commit()

    review = await session.get(Review, job.review_id)
    if review is not None:
        from .runner import cancel_flag_path

        try:
            flag = cancel_flag_path(Path(review.workspace_path), job.id)
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("cancelled by user\n", encoding="utf-8")
        except OSError:
            pass

    if job.arq_job_id:
        try:
            from arq.jobs import Job as ArqJob

            pool = await get_arq_pool()
            await ArqJob(job.arq_job_id, pool,
                         _queue_name=queue_for(job.kind)).abort(timeout=0)
        except Exception:  # noqa: BLE001 - abort is advisory
            pass
