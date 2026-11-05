"""May this review spend LLM money right now? One answer, used everywhere.

Two rules, both about WHOSE credential pays:

1. **The shared-CLI lease** (``llm_lease``). The host's ``claude``/``codex``
   login is one account for the whole machine; while someone else holds the
   lease, nobody else's work may run on it.
2. **PUBLIC_REQUIRE_BYO_LLM.** On a public instance the host login is the
   operator's personal subscription, so non-admin owners must bring their own
   credential (``claude_token`` / ``anthropic_key`` / ``openrouter``).

Both are decided on the REVIEW OWNER's effective provider — the account that
will actually be billed — not on whoever clicked. They used to be checked on the
requester's provider, and only on a few endpoints: an admin acting on someone's
review was judged by the admin's settings, and the agent, drafting and
background-screen endpoints skipped the check entirely.

The HTTP side calls :func:`ensure_allowed`; the worker calls
:func:`start_decision` when a job is about to START, so jobs fired by the
reconciler (which never pass through an endpoint) are covered too.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.models import Review, User
from . import llm_lease, llm_prefs

_BYO_REQUIRED = (
    "This hosted instance does not lend out its own AI login. Add your own "
    "credential in Settings — a Claude token (`claude setup-token`), an "
    "Anthropic API key, or an OpenRouter key — and select it as your provider."
)


async def owner_provider(db: AsyncSession, review: Review) -> tuple[User | None, str]:
    """(review owner, the provider their work will run under)."""
    owner = await db.get(User, review.owner_id)
    return owner, llm_prefs.effective_provider(review, owner)


def policy_refusal(owner: User | None, provider: str) -> str | None:
    """Why *provider* is not allowed for *owner* on this instance, or None."""
    if (
        settings.PUBLIC_MODE
        and settings.PUBLIC_REQUIRE_BYO_LLM
        and provider in llm_prefs.HOST_SHARED_PROVIDERS
        and (owner is None or owner.role != "admin")
    ):
        return _BYO_REQUIRED
    return None


async def ensure_allowed(
    db: AsyncSession, review: Review, *, requester: User | None = None
) -> None:
    """Raise 403 (policy) or 409 (lease held by someone else) for LLM work.

    On success refreshes the owner's lease idle timer — they are actively
    working, so their reservation should not go stale under them.
    """
    owner, provider = await owner_provider(db, review)
    refusal = policy_refusal(owner, provider)
    if refusal:
        raise HTTPException(status.HTTP_403_FORBIDDEN, refusal)
    if owner is None:
        return
    reveal = requester is not None and requester.role == "admin"
    reason = await llm_lease.blocked_reason(db, owner.id, provider, reveal=reveal)
    if reason:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    await llm_lease.touch(db, owner.id)


@dataclass(frozen=True)
class StartDecision:
    """What the worker should do with an LLM job it is about to start."""

    action: str  # "run" | "defer" | "fail"
    reason: str = ""


async def start_decision(db: AsyncSession, review: Review) -> StartDecision:
    """Run, defer (the shared CLI is someone else's right now), or fail (the
    provider is not allowed at all — waiting would never fix that)."""
    owner, provider = await owner_provider(db, review)
    refusal = policy_refusal(owner, provider)
    if refusal:
        return StartDecision("fail", refusal)
    if owner is None:
        return StartDecision("run")
    reason = await llm_lease.blocked_reason(db, owner.id, provider)
    if reason:
        return StartDecision("defer", reason)
    return StartDecision("run")
