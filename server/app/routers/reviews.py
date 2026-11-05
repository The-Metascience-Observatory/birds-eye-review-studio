"""Review CRUD, derived pipeline state, and the wizard stage list.

The other per-review endpoints live in sibling routers that share the
``/reviews/{review_id}`` prefix: ``evidence`` (the live paper table and its
background jobs), ``dashboard`` (the built bundle + synthesis), ``publish``,
``review_config`` (config + prompts) and ``drafts`` (AI-drafted config).
"""
from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .._time import utcnow
from ..db.models import Review, ReviewConfig, StageState, User
from ..deps import get_current_user, get_db, owned_review
from ..errors import public_detail
from ..schemas import (
    ClassifySummaryOut,
    PhaseOut,
    PreflightOut,
    PregatePreviewOut,
    ReviewCreate,
    ReviewOut,
    ReviewPatch,
    ReviewStateOut,
    StageApproveIn,
    StageOut,
    StagesOut,
)
from ..services import review_lifecycle
from ..services import stage_gates as gates_svc
from ..services import stages as stages_svc
from ..services import workspace as ws
from ..services.pipeline_bridge import (
    classify_summary,
    preflight,
    pregate_preview,
    project_slug,
    resume_info,
    review_state,
)
from ..services.stage_view import find_stage as _find_stage
from ..services.stage_view import resolved_with_gates as _resolved_with_gates

router = APIRouter(prefix="/reviews", tags=["reviews"])
logger = logging.getLogger(__name__)


def _stages_out(resolved: list[dict]) -> StagesOut:
    """Build the StagesOut response from a resolved stage list.

    `current_stage` is sharper than artifacts alone: finished-but-unreviewed
    work is exactly what the wizard exists to surface, so it isn't skipped.
    """
    return StagesOut(
        stages=[StageOut(**s) for s in resolved],
        current_stage=gates_svc.next_stage(resolved),
        phases=[PhaseOut(**p) for p in stages_svc.phase_list()],
    )


async def config_for(db: AsyncSession, review: Review) -> dict:
    """The review's effective (normalized) config, as the pipeline would see it."""
    cfg = await db.get(ReviewConfig, review.id)
    return ws.normalize_config(cfg.config_json if cfg else {})


@router.post("", response_model=ReviewOut, status_code=status.HTTP_201_CREATED)
async def create_review(
    payload: ReviewCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Review:
    slug = ws.slugify(payload.topic_name)
    config_dict = ws.build_config_dict(
        topic_name=payload.topic_name,
        synonyms=payload.synonyms,
        description=payload.description,
        not_about=payload.not_about,
        include_observational=payload.include_observational,
        generate_meta_analysis=payload.generate_meta_analysis,
        question=payload.question,
    )

    review = Review(
        owner_id=user.id,
        slug=slug,
        topic_name=payload.topic_name,
        title=payload.title,
        # Follow the owner's account-level provider by default (see llm_prefs
        # INHERIT), so a later Settings switch applies without touching each
        # review. A per-review override sets a concrete provider instead.
        llm_default_provider="inherit",
    )
    db.add(review)
    await db.flush()  # assign review.id before writing the workspace

    workspace = ws.create_review_workspace(review.id, config_dict)
    review.workspace_path = str(workspace)

    db.add(ReviewConfig(review_id=review.id, config_json=config_dict))

    await db.commit()
    await db.refresh(review)
    return review


@router.get("", response_model=list[ReviewOut])
async def list_reviews(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Review]:
    stmt = select(Review)
    if user.role != "admin":
        stmt = stmt.where(Review.owner_id == user.id)
    result = await db.execute(stmt.order_by(Review.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{review_id}", response_model=ReviewOut)
async def get_review(review: Review = Depends(owned_review)) -> Review:
    return review


@router.patch("/{review_id}", response_model=ReviewOut)
async def patch_review(
    payload: ReviewPatch,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> Review:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(review, field, value)
    await db.commit()
    await db.refresh(review)
    return review


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a review: stop its jobs, remove its rows, and move its workspace
    aside (``DATA_ROOT/_deleted/<id>-<ts>``) rather than erase it."""
    await review_lifecycle.delete_review(db, review)


@router.get("/{review_id}/state", response_model=ReviewStateOut)
async def get_review_state(review: Review = Depends(owned_review)) -> ReviewStateOut:
    workspace = Path(review.workspace_path)

    def _build() -> dict:
        return {
            "steps": review_state(workspace),
            "slug": project_slug(workspace),
            **resume_info(workspace),
        }

    return ReviewStateOut(**await anyio.to_thread.run_sync(_build))


@router.get("/{review_id}/stages", response_model=StagesOut)
async def get_review_stages(
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> StagesOut:
    """The wizard stage list, resolved against this review's canonical steps.

    The client renders exactly what this returns — the stage structure lives on
    the server so the two can't drift.
    """
    resolved = await _resolved_with_gates(db, review)
    return _stages_out(resolved)


@router.post("/{review_id}/stages/{slug}/approve", response_model=StagesOut)
async def approve_stage(
    slug: str,
    payload: StageApproveIn | None = None,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StagesOut:
    """Record a human sign-off on one stage.

    Stamps the CURRENT config digest, so a later edit to this stage's own keys
    shows as stale while edits elsewhere leave it alone.
    """
    resolved = await _resolved_with_gates(db, review)
    stage = _find_stage(resolved, slug)

    ok, reason = gates_svc.can_approve(stage)
    if not ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)

    cfg = await db.get(ReviewConfig, review.id)
    config_json = (cfg.config_json if cfg else {}) or {}
    keys = stage.get("config_keys") or []

    st = await db.get(StageState, (review.id, slug))
    if st is None:
        st = StageState(review_id=review.id, stage=slug)
        db.add(st)
    st.approved_at = utcnow()
    st.approved_by = user.id
    st.approved_config_version = cfg.version if cfg else None
    st.stage_config_digest = gates_svc.config_digest(config_json, keys)
    # Per-key digests let the UI name which key went stale later.
    st.decisions = {**(st.decisions or {}),
                    "key_digests": gates_svc.key_digests(config_json, keys)}
    if payload and payload.notes is not None:
        st.notes = payload.notes
    await db.commit()

    resolved = await _resolved_with_gates(db, review)
    return _stages_out(resolved)


@router.post("/{review_id}/stages/{slug}/unapprove", response_model=StagesOut)
async def unapprove_stage(
    slug: str,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> StagesOut:
    """Withdraw a sign-off. Deletes the row — absence IS the unapproved state.

    Deliberately does NOT touch artifacts: the pipeline is resumable and
    idempotent, so re-running forward is the recovery path. Silently deleting a
    user's extraction because they un-ticked a box would be unrecoverable.
    """
    st = await db.get(StageState, (review.id, slug))
    if st is not None:
        await db.delete(st)
        await db.commit()
    resolved = await _resolved_with_gates(db, review)
    return _stages_out(resolved)


@router.get("/{review_id}/classify-summary", response_model=ClassifySummaryOut)
async def get_classify_summary(review: Review = Depends(owned_review)) -> ClassifySummaryOut:
    """Categorical counts from the classify stage's CSVs (empty when not run)."""
    data = await anyio.to_thread.run_sync(classify_summary, Path(review.workspace_path))
    return ClassifySummaryOut(**data)


@router.get("/{review_id}/preflight", response_model=PreflightOut)
async def get_preflight(
    stage: str | None = None,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> PreflightOut:
    """Config/data problems to surface before a run is started.

    ``stage`` narrows the checks to the config keys that stage's scripts
    actually read. Without it every page showed every warning — the screening
    page carried four warnings about pass-2 extraction keys, stages away and not
    yet actionable, which buried the one warning that mattered (a pre-gate about
    to discard 93% of the corpus) in noise.
    """
    config = await config_for(db, review)

    def _check() -> list[dict]:
        # Refresh the on-disk YAML so a run launched right after this sees
        # exactly the config the checks below were computed against.
        ws.create_review_workspace(review.id, config)
        return preflight(Path(review.workspace_path), config, stage_slug=stage)

    issues = await anyio.to_thread.run_sync(_check)
    return PreflightOut(
        issues=issues,
        has_errors=any(i["level"] == "error" for i in issues),
    )


@router.get("/{review_id}/pregate-preview", response_model=PregatePreviewOut)
async def get_pregate_preview(
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> PregatePreviewOut:
    """How many papers the pre-gate would skip — computed without any LLM call."""
    config = await config_for(db, review)
    try:
        data = await anyio.to_thread.run_sync(
            partial(pregate_preview, Path(review.workspace_path), config))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            public_detail(exc, "Pre-gate preview"),
        ) from exc
    return PregatePreviewOut(**data)
