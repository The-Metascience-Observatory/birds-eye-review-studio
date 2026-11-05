"""Admin routes: the publish-request queue and its approve/reject actions.

Every route here is behind `get_current_admin` (403 for non-admins). Approving a
request is the one place the actual deploy runs — an owner can only propose.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import InviteCode, PublishedDashboard, PublishRequest, User, UserDisabled
from ..deps import get_current_admin, get_db
from .._time import utcnow
from ..schemas import (
    InviteCreatedOut,
    InviteCreateIn,
    InviteOut,
    PublishApproveIn,
    PublishedDashboardOut,
    PublishRejectIn,
    PublishRequestOut,
    UserAccessOut,
    UserDisableIn,
)
from ..services import invites as invites_svc
from ..services import publishing as publishing_svc

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/publish-requests", response_model=list[PublishRequestOut])
async def list_publish_requests(
    state: str = "pending",
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[PublishRequest]:
    """The moderation queue. Defaults to pending; pass `state=all` for history."""
    q = select(PublishRequest).order_by(PublishRequest.created_at.desc())
    if state != "all":
        q = q.where(PublishRequest.state == state)
    return list((await db.execute(q)).scalars().all())


@router.get("/published", response_model=list[PublishedDashboardOut])
async def list_published(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[PublishedDashboard]:
    """Everything currently live, including the grandfathered CLI dashboards."""
    q = select(PublishedDashboard).order_by(PublishedDashboard.published_at.desc())
    return list((await db.execute(q)).scalars().all())


async def _get_request(db: AsyncSession, rid: str) -> PublishRequest:
    req = await db.get(PublishRequest, rid)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such publish request")
    return req


@router.post("/publish-requests/{rid}/approve", response_model=PublishedDashboardOut)
async def approve_publish_request(
    rid: str,
    payload: PublishApproveIn,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> PublishedDashboard:
    """Approve a request: run the atomic deploy, then record it live.

    409 when the confirm_token is stale (a rebuild since preflight), the route is
    taken, or the publish would shrink the corpus without `allow_shrink` — none of
    which should silently proceed.
    """
    req = await _get_request(db, rid)
    if req.state != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Request is already {req.state}.")
    try:
        pub = await publishing_svc.approve_and_deploy(
            db, req, admin,
            confirm_token=payload.confirm_token,
            route=payload.route,
            allow_shrink=payload.allow_shrink)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    await db.refresh(pub)
    return pub


@router.post("/publish-requests/{rid}/reject", response_model=PublishRequestOut)
async def reject_publish_request(
    rid: str,
    payload: PublishRejectIn,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> PublishRequest:
    req = await _get_request(db, rid)
    if req.state != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Request is already {req.state}.")
    req.state = "rejected"
    req.decided_by = admin.id
    req.decided_at = utcnow()
    req.decision = {"reason": payload.reason}
    await db.commit()
    await db.refresh(req)
    return req


# --- Access codes ----------------------------------------------------------
# Admin-only, like everything else in this router. These are the operator's
# controls for who may hold an account at all; see services/invites.py for the
# code format and the validity rules.

def _invite_out(invite: InviteCode, email: str | None) -> InviteOut:
    """Shape one invite for the admin list, deriving its display state.

    The state is computed here rather than stored so it cannot go stale: an
    invite with an expiry becomes "expired" by the passage of time alone, with
    nothing to write back.
    """
    if invite.revoked_at is not None:
        state = "revoked"
    elif not invites_svc.check_active(invite).ok:
        state = "expired"
    elif invite.redeemed_at is not None:
        state = "redeemed"
    else:
        state = "pending"
    return InviteOut(
        id=invite.id,
        label=invite.label,
        note=invite.note,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        revoked_at=invite.revoked_at,
        redeemed_at=invite.redeemed_at,
        redeemed_by_user_id=invite.redeemed_by_user_id,
        redeemed_by_email=email,
        state=state,
    )


async def _emails_for(db: AsyncSession, rows: list[InviteCode]) -> dict[str, str]:
    """user_id -> email for the redeemed invites in *rows*, in one query.

    One query rather than per-row lookups: the list is rendered whole, and a
    per-row await is how an admin page with thirty invites becomes thirty
    round-trips.
    """
    ids = {r.redeemed_by_user_id for r in rows if r.redeemed_by_user_id}
    if not ids:
        return {}
    q = select(User.id, User.email).where(User.id.in_(ids))
    return {uid: email for uid, email in (await db.execute(q)).all()}


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[InviteOut]:
    """Every access code ever issued, newest first. Never includes the codes."""
    q = select(InviteCode).order_by(InviteCode.created_at.desc())
    rows = list((await db.execute(q)).scalars().all())
    emails = await _emails_for(db, rows)
    return [
        _invite_out(r, emails.get(r.redeemed_by_user_id or "")) for r in rows
    ]


@router.post(
    "/invites", response_model=InviteCreatedOut, status_code=status.HTTP_201_CREATED
)
async def create_invite(
    payload: InviteCreateIn,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> InviteCreatedOut:
    """Mint one access code for one person.

    The raw code is in THIS response and nowhere else — only its sha256 is
    stored, so it cannot be recovered later. If it is lost, revoke the invite and
    mint another.
    """
    expires_at = None
    if payload.expires_in_days:
        expires_at = utcnow() + timedelta(days=payload.expires_in_days)
    invite, code = await invites_svc.mint(
        db,
        label=payload.label,
        note=payload.note,
        created_by_user_id=admin.id,
        expires_at=expires_at,
    )
    await db.commit()
    await db.refresh(invite)
    return InviteCreatedOut(invite=_invite_out(invite, None), code=code)


@router.post("/invites/{invite_id}/revoke", response_model=InviteOut)
async def revoke_invite(
    invite_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> InviteOut:
    """Withdraw an access code, and with it the account that redeemed it.

    Takes effect on the holder's NEXT request: `deps.get_current_user` rechecks
    the invite on every call, so an already-issued JWT stops working immediately
    rather than lasting out its TTL.
    """
    invite = await db.get(InviteCode, invite_id)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such access code")
    # Idempotent: re-revoking keeps the original timestamp, which is the one that
    # matters for an audit trail.
    if invite.revoked_at is None:
        invite.revoked_at = utcnow()
        await db.commit()
        await db.refresh(invite)
    emails = await _emails_for(db, [invite])
    return _invite_out(invite, emails.get(invite.redeemed_by_user_id or ""))


# --- Account switch-off --------------------------------------------------------
# Revoking an invite ends the account that redeemed it. These cover accounts
# made WITHOUT an invite (open signup on a dev box, the shared SIGNUP_CODE,
# seeded users): a ``user_disabled`` row, checked by deps.get_current_user on
# every request, so the user's very next call is refused.

async def _user_or_404(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")
    return user


@router.post("/users/{user_id}/disable", response_model=UserAccessOut)
async def disable_user(
    user_id: str,
    payload: UserDisableIn | None = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> UserAccessOut:
    """Switch an account off. Idempotent; an admin cannot disable themself
    (that is how an instance ends up with no one able to undo it)."""
    user = await _user_or_404(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot disable your own account.")
    if await db.get(UserDisabled, user.id) is None:
        db.add(UserDisabled(user_id=user.id, disabled_by=admin.id,
                            reason=payload.reason if payload else None))
        await db.commit()
    return UserAccessOut(user_id=user.id, email=user.email, disabled=True)


@router.post("/users/{user_id}/enable", response_model=UserAccessOut)
async def enable_user(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> UserAccessOut:
    """Switch a disabled account back on. Idempotent."""
    user = await _user_or_404(db, user_id)
    row = await db.get(UserDisabled, user.id)
    if row is not None:
        await db.delete(row)
        await db.commit()
    return UserAccessOut(user_id=user.id, email=user.email, disabled=False)
