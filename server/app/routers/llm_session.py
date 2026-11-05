"""Who owns the host's shared ``claude`` CLI login.

The CLI reads credentials from the server user's ``$HOME`` — there is no
per-request credential — so LLM work on this host always runs as ONE Claude
account. These routes make that occupancy explicit and claimable rather than
silent. See services/llm_lease.py for the full rationale.

Login itself is deliberately NOT here: ``claude auth login`` is interactive
(browser / paste-a-code) and cannot be driven from an HTTP request. The status
payload carries a ``login_hint`` shell command instead.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User
from ..deps import get_current_user, get_db
from ..errors import mask_email
from ..schemas import LlmSessionClaimIn, LlmSessionOut
from ..services import llm_lease, llm_prefs

router = APIRouter(prefix="/llm", tags=["llm"])

_LOGIN_HINT = "claude auth logout && claude auth login"


async def _build_out(
    session: AsyncSession, user: User, *, cli=None
) -> LlmSessionOut:
    lease = await llm_lease.current(session)
    provider = llm_prefs.effective_provider(None, user)
    auth = cli if cli is not None else (
        await llm_lease.probe_codex_auth() if provider == "codex_cli"
        else await llm_lease.probe_cli_auth()
    )
    expired = llm_lease.is_expired(lease)
    held = lease.holder_user_id is not None and not expired
    held_by_me = held and lease.holder_user_id == user.id
    # Other people's addresses are masked for non-admins: this endpoint is open
    # to every signed-in user, and the lease holder's email and Claude account
    # (and the HOST's own account) are not theirs to read.
    reveal = user.role == "admin"
    holder = await llm_lease.holder_email(session, lease) if held else None
    account = lease.claude_account if held else None
    if not (reveal or held_by_me):
        holder, account = mask_email(holder), mask_email(account)
    return LlmSessionOut(
        held=held,
        held_by_me=held_by_me,
        holder_email=holder,
        claude_account=account,
        acquired_at=lease.acquired_at if held else None,
        expired=lease.holder_user_id is not None and expired,
        ttl_minutes=llm_lease.LEASE_TTL_MIN,
        cli_installed=auth.installed,
        cli_logged_in=auth.logged_in,
        cli_account=auth.account if reveal or held_by_me else mask_email(auth.account),
        cli_detail=auth.detail,
        provider=provider,
        login_hint="codex login" if provider == "codex_cli" else _LOGIN_HINT,
    )


@router.get("/session", response_model=LlmSessionOut)
async def get_session(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LlmSessionOut:
    """Lease state + what the host CLI reports about itself."""
    return await _build_out(db, user)


@router.post("/session/claim", response_model=LlmSessionOut)
async def claim_session(
    payload: LlmSessionClaimIn | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LlmSessionOut:
    """Claim the host CLI for this user.

    Refused (409) while someone else holds a live lease — unless ``force`` and
    the lease has gone idle past the TTL, so a forgotten session is never a
    permanent block.
    """
    force = bool(payload and payload.force)
    lease = await llm_lease.current(db)

    reveal = user.role == "admin"
    if llm_lease.held_by_other(lease, user.id) and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            await llm_lease.blocked_reason(db, user.id, "claude_cli", reveal=reveal)
            or "The Claude CLI is in use by another user.",
        )
    # force only overrides an EXPIRED lease; never yank a live one.
    if llm_lease.held_by_other(lease, user.id) and force and not llm_lease.is_expired(lease):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That lease is still active — it can only be taken over once idle.",
        )

    provider = llm_prefs.effective_provider(None, user)
    auth = (await llm_lease.probe_codex_auth() if provider == "codex_cli"
            else await llm_lease.probe_cli_auth())
    try:
        await llm_lease.acquire(db, user.id, auth.account)
    except llm_lease.LeaseHeld:
        # Lost a race with another claimant between the check above and the
        # (atomic) claim itself.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            await llm_lease.blocked_reason(db, user.id, "claude_cli", reveal=reveal)
            or "The Claude CLI is in use by another user.",
        )
    return await _build_out(db, user, cli=auth)


@router.post("/session/release", response_model=LlmSessionOut)
async def release_session(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LlmSessionOut:
    """Give up the lease. Idempotent — a no-op if this user isn't the holder."""
    await llm_lease.release(db, user.id)
    return await _build_out(db, user)
