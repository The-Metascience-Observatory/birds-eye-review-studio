"""Agent endpoints: the review's collaborator conversation.

Thin HTTP shims over :mod:`services.agent` — the service owns the transcript,
the per-review lock, provider routing and cost recording, so those live in
exactly one place regardless of which endpoint triggered a turn. The two
endpoints that make a model call pass the same LLM access check and per-user
rate limit as every other LLM-spending endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Review, User
from ..deps import get_current_user, get_db, owned_review
from ..ratelimit import llm_limit
from ..schemas import (
    AgentAskIn,
    AgentDismissIn,
    AgentMessageOut,
    AgentReviewIn,
)
from ..services import agent as agent_svc
from ..services import llm_access

router = APIRouter(prefix="/reviews/{review_id}/agent", tags=["agent"])


@router.get("/messages", response_model=list[AgentMessageOut])
async def get_messages(
    stage: str | None = None,
    limit: int = 200,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
):
    return await agent_svc.recent_messages(
        db, review.id, stage=stage, limit=max(1, min(limit, 500))
    )


@router.post("/ask", response_model=AgentMessageOut,
             dependencies=[Depends(llm_limit)])
async def ask(
    payload: AgentAskIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await llm_access.ensure_allowed(db, review, requester=user)
    try:
        return await agent_svc.ask(
            db, review, stage_slug=payload.stage, message=payload.message
        )
    except agent_svc.AgentBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except agent_svc.AgentFailed as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc


@router.post("/review", response_model=AgentMessageOut,
             dependencies=[Depends(llm_limit)])
async def review_stage(
    payload: AgentReviewIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The post-run sanity check. Advisory — blockers annotate, never halt.
    Idempotent per job_id, so the client may fire it optimistically."""
    # An already-reviewed run is answered from the transcript without a model
    # call, so it needs no access check (and must not 409 on a held lease).
    prior = await agent_svc.prior_review(db, review.id, payload.job_id)
    if prior is not None:
        return prior
    await llm_access.ensure_allowed(db, review, requester=user)
    try:
        return await agent_svc.review_stage(
            db, review, stage_slug=payload.stage, job_id=payload.job_id
        )
    except agent_svc.AgentBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except agent_svc.AgentFailed as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc


@router.post("/dismiss", response_model=AgentMessageOut)
async def dismiss_blocker(
    payload: AgentDismissIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record 'I know, proceed'. The override is durable (StageState.decisions)
    and visible in the transcript, so a colleague's objection costs one click —
    never a support request."""
    return await agent_svc.dismiss_blocker(
        db,
        review,
        stage_slug=payload.stage,
        code=payload.code,
        reason=payload.reason,
        user_id=user.id,
    )
