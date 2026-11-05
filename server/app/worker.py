"""arq worker: task functions + the two worker settings classes.

Run with::

    server/.venv/bin/python -m arq server.app.worker.WorkerSettings
    server/.venv/bin/python -m arq server.app.worker.PdfWorkerSettings

``WorkerSettings`` (queue ``ber:default``) runs every LLM-spending job kind one
at a time (``max_jobs=1``): work on the host's shared CLI login must be
serialized. ``PdfWorkerSettings`` (queue ``ber:pdf``) runs the non-LLM
background kinds — full-text fetch and metadata enrichment — in a separate
process, so they never occupy the LLM slot.

A job that cannot START yet is not allowed to sit on its slot waiting: when the
owner's shared-CLI lease is held by someone else, or another job holds the
review's workspace lock, the job goes back to ``queued`` and arq re-delivers it
after a delay (``Retry``), freeing the worker for other users' jobs.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path

from arq.connections import RedisSettings
from arq.worker import Retry, func
from sqlalchemy import update

from .config import apply_public_defaults, assert_public_posture, settings
from .db.models import Job, Review
from ._time import utcnow
from .errors import public_detail
from .services import runner

logger = logging.getLogger(__name__)

# arq's default job_timeout is 300s, which silently kills real pipeline runs:
# a PDF-download step alone routinely takes ~28 minutes, and extraction passes
# run far longer. A run that trips this dies mid-flight with its DB row left
# saying "running" forever. 24h is effectively "no limit" while still bounding a
# truly wedged job. Override with BER_JOB_TIMEOUT_S.
JOB_TIMEOUT_S = int(os.environ.get("BER_JOB_TIMEOUT_S") or 86_400)

# Kinds that spend LLM money: they check the owner's provider policy and the
# shared-CLI lease at START (jobs fired by the reconciler never pass through an
# HTTP endpoint, so the endpoint checks alone would not cover them).
_LLM_KINDS = frozenset({"step", "pipeline", "fulltext_screen", "synthesis"})
# Kinds that write workspace artifacts and so must hold the review's lock.
_LOCKED_KINDS = frozenset({"step", "pipeline", "pdf", "fulltext_screen", "synthesis"})

# arq counts every delivery as a "try", deferrals included. Failures are never
# retried (a task that raises anything but Retry is finished), so this ceiling
# only bounds how long a job may keep deferring: ~1 day at the default delays.
_MAX_DEFERRALS = 2_000


async def _load_job_and_review(session, job_id: str):
    """Claim a queued Job (queued -> running) and load its Review.

    Returns (job, review), or (None, None) when the job is gone, already
    terminal, or claimed by another worker — the compare-and-set on ``state``
    is what makes a redelivered arq job (worker restart, deferral) a no-op.
    """
    candidate = await session.get(Job, job_id)
    if candidate is None:
        return None, None
    # Share configuration's review lock so a job cannot start between its
    # running-state check and the config commit.
    await session.execute(update(Review).where(Review.id == candidate.review_id)
                          .values(updated_at=Review.updated_at))
    result = await session.execute(update(Job).where(
        Job.id == job_id, Job.state == "queued").values(state="running"))
    await session.commit()
    if not result.rowcount:
        return None, None  # terminal/replayed job, or claimed by another worker
    job = await session.get(Job, job_id, populate_existing=True)
    review = await session.get(Review, job.review_id)
    return job, review


async def _requeue(session, job_id: str) -> None:
    """Hand a claimed-but-not-started job back to ``queued`` (unless something
    — a cancel — moved it on meanwhile)."""
    await session.execute(update(Job).where(
        Job.id == job_id, Job.state == "running").values(state="queued"))
    await session.commit()


async def _fail_job(sessionmaker, job_id: str, error: str) -> None:
    """Mark a job failed from OUTSIDE its own session.

    Used when the task is cancelled (arq ``job_timeout``, worker SIGTERM). The
    task's own session is unusable at that point — the CancelledError unwinds
    through every ``await``, so a commit on it would itself be cancelled. A
    fresh short-lived session is the only way the row actually gets written.
    """
    from datetime import datetime, timezone

    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None or job.state in ("succeeded", "failed", "cancelled"):
            return
        job.state = "failed"
        job.error = error
        job.ended_at = datetime.now(timezone.utc)
        await session.commit()


def job_task(kind: str, cancelled_msg: str):
    """Wrap a ``body(session, job, review)`` coroutine as an arq task.

    The ONE copy of the bookkeeping every job kind needs: claim the row, apply
    the LLM start checks, take the workspace lock, and turn every way the body
    can end — return, exception, cancellation, deferral — into a correct
    ``Job`` state.
    """
    def deco(body):
        @functools.wraps(body)
        async def task(ctx, job_id: str) -> str:
            sessionmaker = ctx["sessionmaker"]
            try:
                async with sessionmaker() as session:
                    job, review = await _load_job_and_review(session, job_id)
                    if job is None or review is None:
                        return "missing job/review"
                    if kind in _LLM_KINDS:
                        from .services import llm_access

                        decision = await llm_access.start_decision(session, review)
                        if decision.action == "fail":
                            await _fail_job(sessionmaker, job_id, decision.reason)
                            return "refused"
                        if decision.action == "defer":
                            logger.info("job %s deferred: %s", job_id, decision.reason)
                            await _requeue(session, job_id)
                            raise Retry(defer=settings.LLM_LEASE_DEFER_S)
                    if kind in _LOCKED_KINDS:
                        try:
                            lock = runner.workspace_lock(Path(review.workspace_path))
                            lock.__enter__()
                        except runner.WorkspaceBusy:
                            logger.info("job %s deferred: workspace busy", job_id)
                            await _requeue(session, job_id)
                            raise Retry(defer=settings.WORKSPACE_BUSY_DEFER_S)
                        try:
                            await body(session, job, review)
                        finally:
                            lock.__exit__(None, None, None)
                    else:
                        await body(session, job, review)
                    return job.state
            except Retry:
                raise
            except Exception as exc:
                await _fail_job(sessionmaker, job_id, public_detail(exc, "The job"))
                raise
            except asyncio.CancelledError:
                await _fail_job(sessionmaker, job_id, cancelled_msg)
                raise
        return task
    return deco


_CANCELLED = (f"Cancelled by the worker after {JOB_TIMEOUT_S}s "
              "(arq job_timeout) or on worker shutdown.")


@job_task("step", _CANCELLED)
async def run_step_task(session, job, review) -> None:
    """Execute a single-step job."""
    await runner.execute_step(session, job, review, job.step_id)


@job_task("pipeline", _CANCELLED)
async def run_pipeline_task(session, job, review) -> None:
    """Execute a pipeline (range) job, reading run params from ``Job.params``."""
    params = job.params or {}
    await runner.execute_pipeline(
        session,
        job,
        review,
        from_num=params.get("from_num"),
        to_num=params.get("to_num"),
        only=params.get("only"),
        only_ids=params.get("only_ids"),
        since=params.get("since"),
        fresh=bool(params.get("fresh", False)),
    )


@job_task("metadata", "Metadata enrichment cancelled.")
async def enrich_metadata_task(session, job, review) -> None:
    """Background bibliographic enrichment (non-LLM queue).

    Exempt from the one-job guard and from the workspace lock: it only fetches
    from the network and writes the paper_metadata table — none of the
    resume-critical workspace files a pipeline step would race.
    """
    from .services import paper_metadata

    job.started_at = utcnow()
    await session.commit()
    result = await paper_metadata.enrich_review(
        session, review.id, Path(review.workspace_path))
    job.progress = {"metadata": result}
    # _finish_job re-reads the row: a Stop pressed while the crawl ran must
    # stay "cancelled", not be overwritten with "succeeded".
    await runner._finish_job(session, job)


@job_task("pdf", "Full-text fetch cancelled.")
async def run_pdf_task(session, job, review) -> None:
    """Background full-text fetch: download + convert PDFs (non-LLM queue)."""
    await runner.run_fulltext_fetch(session, job, review)


@job_task("fulltext_screen", "Full-text screen cancelled.")
async def run_fulltext_screen_task(session, job, review) -> None:
    """Background full-text eligibility screen (pass 1 + stage). LLM queue:
    extract_pass1 is a model step, so it shares the single LLM slot."""
    await runner.run_fulltext_screen(session, job, review)


@job_task("synthesis", "Review synthesis cancelled.")
async def run_synthesis_task(session, job, review) -> None:
    """Generate the narrative review document (LLM queue). NOT exempt from the
    one-job guard: it reads the bundle files a pipeline run rewrites."""
    await runner.run_synthesis(session, job, review)


async def _on_startup(ctx) -> None:
    """Build the async sessionmaker the tasks use (reuse db.session helpers)."""
    from .db.session import AsyncSessionLocal, init_models

    # Same gate as create_app: a worker that inherits BER_ENGINE_FAKE_STEPS
    # from a stray environment must not start under PUBLIC_MODE with the
    # documented JWT default still in place.
    assert_public_posture(settings)
    apply_public_defaults(settings)

    await init_models()
    ctx["sessionmaker"] = AsyncSessionLocal


def _deferrable(task):
    """Register *task* with room for deferrals (see ``_MAX_DEFERRALS``)."""
    return func(task, max_tries=_MAX_DEFERRALS)


class WorkerSettings:
    """The LLM queue, one job at a time (shared CLI login)."""

    functions = [
        _deferrable(run_step_task),
        _deferrable(run_pipeline_task),
        _deferrable(run_fulltext_screen_task),
        _deferrable(run_synthesis_task),
        # Metadata jobs are dispatched to ber:pdf now; still registered here
        # only so a job queued on this queue before the move is not orphaned.
        _deferrable(enrich_metadata_task),
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 1
    queue_name = "ber:default"
    on_startup = _on_startup
    # See JOB_TIMEOUT_S: arq's 300s default kills real runs mid-flight.
    job_timeout = JOB_TIMEOUT_S
    # NEVER auto-retry a FAILED job. arq's default (max_tries=5) would silently
    # re-run a failed pipeline up to five times, spending real LLM money each
    # attempt and confusing the resume logic. Only an explicit Retry (a
    # deferral that never started) re-delivers — see _deferrable.
    max_tries = 1
    # Lets the cancel endpoint abort a job that is still waiting in the queue
    # (or deferred) instead of leaving it for the entry guard to discard.
    allow_abort_jobs = True


class PdfWorkerSettings:
    """The NON-LLM queue: background full-text fetch and metadata enrichment.

    A SEPARATE arq process consuming `ber:pdf`, so this work runs concurrently
    with an LLM step instead of queueing behind it. `max_jobs=1` (not higher):
    GROBID is a single shared server and each convert worker uses ~8 GB RAM, so
    the concurrency lives INSIDE the scripts (`--workers`), not across jobs.
    """

    functions = [_deferrable(run_pdf_task), _deferrable(enrich_metadata_task)]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 1
    queue_name = "ber:pdf"
    on_startup = _on_startup
    job_timeout = JOB_TIMEOUT_S
    max_tries = 1
    allow_abort_jobs = True
