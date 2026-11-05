"""Per-person access codes: minting, redemption, and revocation.

The ONE place that knows how a code is shaped, hashed, and judged valid. Both
doors that consult codes — ``routers/auth.py::register`` at redemption and
``deps.py::get_current_user`` on every subsequent request — call in here, so the
policy cannot drift between them.

Threat model this is written against: the studio sits behind a Cloudflare tunnel
on a desktop where every pipeline step runs as the operator's own OS user, with
their data-source API keys in the environment. An account is therefore close to
a shell, and the operator hands codes only to people they vouch for. What this
buys over the old shared ``SIGNUP_CODE`` is that a code identifies a *person*,
so access can be withdrawn from one of them without disturbing the rest.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .._time import utcnow
from ..db.models import InviteCode

# Distinctive, greppable prefix. If a code ever lands in a chat log, a paste bin
# or a git diff, "ber_" is what makes an automated secret scan able to spot it.
CODE_PREFIX = "ber_"
# 24 bytes ≈ 192 bits of entropy. Far past any feasible online guessing, which is
# what lets the stored hash be a plain sha256 (see the InviteCode docstring).
_CODE_BYTES = 24


def generate_code() -> str:
    """A fresh raw access code. Returned to the admin once, never stored."""
    return CODE_PREFIX + secrets.token_urlsafe(_CODE_BYTES)


def hash_code(code: str) -> str:
    """The stored form of *code*.

    Deterministic on purpose: redemption must be one indexed lookup. Whitespace
    is stripped first because codes get copied out of emails and chat, where a
    trailing space is routine — silently failing on one would look like a broken
    code rather than a stray character.
    """
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CodeState:
    """Why a code is or isn't usable. Kept separate from the HTTP layer so the
    caller decides what (if anything) to reveal — see ``_BAD_CODE`` in auth.py:
    a caller who learns *which* of these applies learns whether a guessed code
    exists at all."""

    ok: bool
    reason: str  # "" when ok; otherwise one of the constants below

    REVOKED = "revoked"
    EXPIRED = "expired"
    REDEEMED = "already redeemed"
    UNKNOWN = "unknown code"


def check(invite: InviteCode | None, *, now: datetime | None = None) -> CodeState:
    """Judge an invite for REDEMPTION (i.e. it must still be unredeemed)."""
    if invite is None:
        return CodeState(False, CodeState.UNKNOWN)
    state = check_active(invite, now=now)
    if not state.ok:
        return state
    if invite.redeemed_at is not None:
        return CodeState(False, CodeState.REDEEMED)
    return CodeState(True, "")


def check_active(invite: InviteCode, *, now: datetime | None = None) -> CodeState:
    """Judge an ALREADY-REDEEMED invite for continued access.

    Split from :func:`check` because the two callers disagree about exactly one
    condition: at redemption a used code is spent, while on a live request a used
    code is the normal case — it is the user's own. Revocation and expiry apply
    to both, which is what makes access withdrawable after the fact.
    """
    if invite.revoked_at is not None:
        return CodeState(False, CodeState.REVOKED)
    expires = invite.expires_at
    if expires is not None:
        # Rows written by sqlite come back naive; compare in UTC either way
        # rather than letting a TypeError decide someone's access.
        moment = now or utcnow()
        if expires.tzinfo is None:
            moment = moment.replace(tzinfo=None)
        if expires <= moment:
            return CodeState(False, CodeState.EXPIRED)
    return CodeState(True, "")


async def find_by_code(db: AsyncSession, code: str) -> InviteCode | None:
    """The invite matching a raw code, or None."""
    if not code or not code.strip():
        return None
    q = select(InviteCode).where(InviteCode.code_hash == hash_code(code))
    return (await db.execute(q)).scalar_one_or_none()


async def active_for_user(db: AsyncSession, user_id: str) -> InviteCode | None:
    """The invite a user registered with, if any.

    Users predating the invite system — the operator, seeded admins, every test
    fixture — have no row here, and are deliberately unaffected by all of this.
    """
    q = select(InviteCode).where(InviteCode.redeemed_by_user_id == user_id)
    return (await db.execute(q)).scalars().first()


async def mint(
    db: AsyncSession,
    *,
    label: str,
    created_by_user_id: str | None = None,
    note: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[InviteCode, str]:
    """Create an invite. Returns (row, raw_code) — the raw code is the ONLY copy."""
    code = generate_code()
    invite = InviteCode(
        code_hash=hash_code(code),
        label=label.strip(),
        note=(note or "").strip() or None,
        created_by_user_id=created_by_user_id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.flush()
    return invite, code


async def claim(db: AsyncSession, invite_id: str, user_id: str) -> bool:
    """Bind an invite to the user it just created. True if this call won it.

    A CONDITIONAL update, not a read-then-write: two registrations submitted with
    the same code at the same moment would both pass an earlier validity check,
    and a plain assignment would let both through — one code, two accounts. The
    ``redeemed_at IS NULL`` predicate makes the database the arbiter, and the
    loser sees the same generic failure as any other bad code.
    """
    result = await db.execute(
        update(InviteCode)
        .where(InviteCode.id == invite_id, InviteCode.redeemed_at.is_(None))
        .values(redeemed_at=utcnow(), redeemed_by_user_id=user_id)
    )
    return (result.rowcount or 0) == 1
