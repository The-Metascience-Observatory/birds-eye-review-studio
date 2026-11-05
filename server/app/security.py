"""Password hashing (argon2) and JWT access tokens (pyjwt, HS256)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


_DUMMY_HASH: str | None = None


def burn_password_check(password: str) -> None:
    """Spend one argon2 verify on a login for an email that has no account.

    Without it, "unknown email" answers in microseconds and "wrong password"
    after a full argon2 verify — a timing oracle that tells an attacker which
    addresses are registered. The dummy hash is made once, lazily, with the
    same parameters as real ones, so both paths cost the same.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _ph.hash("not-a-real-password")
    verify_password(_DUMMY_HASH, password)


def create_access_token(
    subject: str,
    *,
    extra: dict[str, Any] | None = None,
    ttl_min: int | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_min or settings.ACCESS_TTL_MIN),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


def decode_token(token: str) -> dict[str, Any]:
    """Decode + verify a JWT. Raises jwt.PyJWTError on any problem.

    ``exp`` and ``sub`` are REQUIRED: pyjwt only checks ``exp`` when it is
    present, so a token minted without one (by a leaked secret, or a future
    code path that forgets it) would otherwise never expire.
    """
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGO],
        options={"require": ["exp", "sub"]},
    )
