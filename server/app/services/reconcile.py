"""Recover dispatch and continue explicitly started reviews without an open tab."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import select, update

from ..db.models import Job, Review, ReviewAutomation, ReviewConfig
from ..db.session import AsyncSessionLocal
from . import evidence, jobs, paper_metadata

logger = logging.getLogger(__name__)


def signature(kind, rows, config):
    return hashlib.sha256(json.dumps([kind, sorted(r["doi"] for r in rows), config],
                                    sort_keys=True).encode()).hexdigest()


async def _sweep_orphans(session) -> None:
    """Settle DB rows whose arq job ended without the row moving.

    * ``running`` with no live arq job: the worker died mid-job.
    * ``queued`` whose arq job already COMPLETED: the delivery finished without
      ever claiming the row (e.g. it exhausted its deferrals). arq refuses to
      re-enqueue a job id whose result it still holds, so ``dispatch_pending``
      cannot revive it — left alone, the row says "queued" for an hour, then
      silently re-runs when the result expires.

    A ``queued`` row whose arq job is simply NOT FOUND (Redis restarted, or the
    dispatch never reached it) needs nothing here: ``dispatch_pending`` has
    just re-enqueued it.
    """
    from arq.jobs import Job as RedisJob, JobStatus

    from .._time import utcnow

    rows = (await session.execute(
        select(Job).where(Job.state.in_(("running", "queued")),
                          Job.arq_job_id.is_not(None)))).scalars().all()
    if not rows:
        return
    pool = await jobs.get_arq_pool()
    for job in rows:
        try:
            state = await RedisJob(job.arq_job_id, pool,
                                   _queue_name=jobs.queue_for(job.kind)).status()
        except Exception:  # noqa: BLE001 - Redis hiccup: try again next sweep
            logger.debug("status check failed for %s", job.id, exc_info=True)
            continue
        values = None
        if job.state == "running":
            if state in (JobStatus.not_found, JobStatus.complete):
                values = dict(state="failed", ended_at=utcnow(),
                              error="Worker stopped without recording completion; "
                                    "retry the resumable job.")
            elif state in (JobStatus.queued, JobStatus.deferred):
                values = dict(state="queued")
        elif state == JobStatus.complete:
            values = dict(state="failed", ended_at=utcnow(),
                          error="The job was dropped by the queue before it could "
                                "start; start it again.")
        if values:
            await session.execute(update(Job).where(Job.id == job.id, Job.state == job.state)
                                  .values(**values).execution_options(synchronize_session=False))
    await session.commit()


async def reconcile_once(session):
    await jobs.dispatch_pending(session)
    # A process killed without cleanup must not leave a phantom active job.
    await _sweep_orphans(session)
    reviews = (await session.execute(select(Review).join(
        ReviewAutomation, ReviewAutomation.review_id == Review.id))).scalars().all()
    for review in reviews:
        metadata = await paper_metadata.load_metadata(session, review.id)
        try:
            rows = (await asyncio.to_thread(
                evidence.build_evidence, Path(review.workspace_path), metadata=metadata))["rows"]
        except evidence.CorruptVerdictFile as exc:
            # One review's broken decisions file must not stall every other
            # review's background work; this one waits until it is fixed.
            logger.warning("reconcile skipped review %s: %s", review.id, exc)
            continue
        cfg = await session.get(ReviewConfig, review.id)
        config = cfg.config_json if cfg else {}
        work = {
            "metadata": [r for r in rows if not r.get("metadata_source")],
            "pdf": [r for r in rows if r.get("relevance") == "yes" and not r.get("is_excluded") and (
                not r.get("has_fulltext") or not r.get("has_xml") or
                ((config.get("fulltext") or {}).get("supplementary") and not r.get("has_supplement")))],
            "fulltext_screen": [r for r in rows if r.get("screen_state") == "pending" and r.get("has_fulltext")],
        }
        for kind, pending in work.items():
            if pending:
                await jobs.enqueue(session, review, kind,
                                   fingerprint=signature(kind, pending, config))
        await session.commit()


async def run():
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await reconcile_once(session)
        except Exception:
            logger.exception("Background reconciliation failed; will retry")
        await asyncio.sleep(15)
