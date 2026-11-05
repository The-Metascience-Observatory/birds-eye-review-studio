"""Publishing, owner side.

Publishing is two-party: the OWNER proposes here; an ADMIN approves in the admin
router, which is where the actual deploy runs. Building the dashboard LOCALLY
(the view-dashboard stage) stays open to every owner; only pushing it public is
gated. `status`/`visibility` are server-owned — set by the deploy, never by
PATCH (test_no_accidental_publish pins this).

There is deliberately no owner "unpublish" endpoint. The old one deleted the
database record while leaving the feed live on the public site — the review
then read "unpublished" while the dashboard was still being served, and its
route was freed for another review to publish over. Taking a dashboard down is
an operator action on the website checkout.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import PublishRequest, Review, User
from ..deps import get_current_user, get_db, owned_review
from ..schemas import PublishPreflightOut, PublishRequestIn, PublishRequestOut
from ..services import publishing as publishing_svc

router = APIRouter(prefix="/reviews/{review_id}", tags=["publish"])


@router.get("/publish/preflight", response_model=PublishPreflightOut)
async def publish_preflight(
    route: str,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PublishPreflightOut:
    """The evidence packet for a publish decision: bundle state, route
    availability, the diff vs. the live site, shrink warning, honesty numbers,
    and a confirm_token approve must echo. Writes nothing."""
    try:
        data = await publishing_svc.build_preflight(
            db, review, route, reveal_holder=user.role == "admin")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return PublishPreflightOut(**data)


@router.post("/publish-request", response_model=PublishRequestOut,
             status_code=status.HTTP_201_CREATED)
async def create_publish_request(
    payload: PublishRequestIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PublishRequest:
    """An owner proposes publishing this review to a public route.

    One open request per review: a second while one is pending returns the
    pending one rather than stacking duplicates the admin would have to triage.
    """
    canonical_route = publishing_svc.deploy_svc.route_for(
        publishing_svc.pipeline_bridge.project_slug(Path(review.workspace_path)))
    if payload.route.strip() != canonical_route:
        raise HTTPException(422, f"This review publishes at {canonical_route!r}.")

    existing = (await db.execute(
        select(PublishRequest).where(
            PublishRequest.review_id == review.id,
            PublishRequest.state == "pending"))).scalar_one_or_none()
    if existing is not None:
        return existing

    req = PublishRequest(
        review_id=review.id, requested_by=user.id,
        requested_route=payload.route.strip())
    db.add(req)
    await db.commit()
    await db.refresh(req)
    return req
