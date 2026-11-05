"""The resolved stage list, joined to approvals and live job state.

Lives in services rather than a router because BOTH `routers/reviews.py` (which
lists and approves stages) and `routers/jobs.py` (which runs one) need it, and a
router importing another router is an import cycle waiting to happen.

This is the one place the three axes meet: `stages.resolve_stages` supplies the
ARTIFACT axis from the filesystem, `StageState` rows supply the APPROVAL axis,
and the review's active `Job` supplies the ACTIVITY axis. Each is computed by a
module that knows nothing about the other two.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job, Review, ReviewConfig, StageState
from . import jobs as jobs_svc
from . import stage_gates as gates_svc
from . import stages as stages_svc


async def _live_steps(db: AsyncSession, review_id: str) -> tuple[str | None, set[str]]:
    """(job_id, step ids that job is currently inside).

    `routers.jobs.reject_if_busy` guarantees at most one active job per review, so this is
    one query rather than a fan-out. Without it `activity.running_job_id` is
    always null and a user watching a run sees a completely static list.
    """
    job: Job | None = await jobs_svc.active_job(db, review_id)
    if job is None:
        return None, set()

    # A step job names its step directly; a pipeline job writes the step it is
    # currently inside at the TOP LEVEL of `progress` (runner.execute_pipeline
    # commits {"step_id": ..., "step_label": ...} before each step). The nested
    # `progress.live` shape only ever exists in GET /jobs/{id} responses (the
    # sidecar overlay) — it is checked here as a defensive fallback, but the
    # top-level key is the one the DB actually carries; reading only `live`
    # left `activity.running_job_id` null for the whole of every pipeline step.
    live: set[str] = set()
    if job.kind == "step" and job.step_id:
        live.add(job.step_id)
    prog = job.progress or {}
    if prog.get("step_id"):
        live.add(prog["step_id"])
    live_rec = prog.get("live") or {}
    if live_rec.get("step_id"):
        live.add(live_rec["step_id"])
    return job.id, live


def _job_step_ids(job: Job) -> set[str]:
    """Every canonical step id a job ran or was asked to run."""
    ids: set[str] = set()
    if job.step_id:
        ids.add(job.step_id)
    params = job.params or {}
    ids.update(str(i) for i in (params.get("only_ids") or []))
    prog = job.progress or {}
    if prog.get("step_id"):
        ids.add(prog["step_id"])
    for rec in prog.get("steps") or []:
        if isinstance(rec, dict) and rec.get("step_id"):
            ids.add(rec["step_id"])
    return ids


# How far back to look for a stage's last job. Recent jobs are what the
# activity pill describes; an old run of a stage long since re-run is noise.
_LAST_JOB_WINDOW = 50


async def _last_job_states(db: AsyncSession, review_id: str, resolved: list[dict]) -> dict[str, str]:
    """stage slug -> state of the newest job that touched one of its steps.

    Populates ``activity.last_job_state``, which was hard-coded to None — so a
    stage whose last run FAILED looked exactly like one never run.
    """
    jobs = (await db.execute(
        select(Job).where(Job.review_id == review_id)
        .order_by(Job.created_at.desc()).limit(_LAST_JOB_WINDOW)
    )).scalars().all()
    out: dict[str, str] = {}
    for job in jobs:  # newest first: the first match per stage wins
        touched = _job_step_ids(job)
        if not touched:
            continue
        for s in resolved:
            if s["slug"] not in out and touched & set(s["step_ids"]):
                out[s["slug"]] = job.state
    return out


async def resolved_with_gates(db: AsyncSession, review: Review) -> list[dict]:
    """Stages with `gate` and `activity` attached."""
    import anyio

    # resolve_stages stats every declared output and reads the CSV row counts;
    # off the event loop like the other filesystem-heavy reads.
    resolved = await anyio.to_thread.run_sync(
        stages_svc.resolve_stages, Path(review.workspace_path))

    rows = (
        await db.execute(
            select(StageState).where(StageState.review_id == review.id)
        )
    ).scalars().all()
    cfg = await db.get(ReviewConfig, review.id)

    job_id, live_ids = await _live_steps(db, review.id)
    active_jobs: dict[str, str] = {}
    if job_id and live_ids:
        for s in resolved:
            if live_ids & set(s["step_ids"]):
                active_jobs[s["slug"]] = job_id

    return gates_svc.apply_gates(
        resolved,
        {r.stage: r for r in rows},
        (cfg.config_json if cfg else {}) or {},
        active_jobs=active_jobs,
        last_job_states=await _last_job_states(db, review.id, resolved),
    )


def find_stage(resolved: list[dict], slug: str) -> dict:
    """The stage with this slug, or 404. Slugs are stable forever."""
    for s in resolved:
        if s["slug"] == slug:
            return s
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No stage {slug!r} in this review.")
