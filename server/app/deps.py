"""FastAPI dependencies: DB session, current user, admin guard, ownership."""
from __future__ import annotations

from typing import AsyncGenerator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Job, Review, User, UserDisabled
from .db.session import AsyncSessionLocal
from .security import decode_token
from .services import invites

_bearer = HTTPBearer(auto_error=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    unauth = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise unauth
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise unauth
    user = await db.get(User, sub)
    if user is None:
        raise unauth

    # Revocation. Access tokens are stateless HS256 JWTs with no denylist, so a
    # token stays cryptographically valid until it expires — revoking someone
    # would otherwise mean waiting out ACCESS_TTL_MIN. But this dependency
    # already loads the user from the database on EVERY request, so checking
    # their invite here costs one more indexed lookup and makes withdrawal take
    # effect on the user's very next request.
    #
    # Users with no invite row (the operator, seeded admins, test fixtures) are
    # untouched by the invite check: `active_for_user` returns None.
    invite = await invites.active_for_user(db, user.id)
    if invite is not None and not invites.check_active(invite).ok:
        raise unauth
    # The off switch for accounts that never had an invite (see UserDisabled).
    if await db.get(UserDisabled, user.id) is not None:
        raise unauth
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


async def owned_review(
    review_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Review:
    """The review named by the ``{review_id}`` path parameter, if the caller
    owns it (admins see every review). 404 / 403 otherwise.

    One dependency instead of a helper per router: three routers each carried
    their own copy, and an ownership rule that lives in three places is one
    that eventually differs in one of them.
    """
    review = await db.get(Review, review_id)
    if review is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    if review.owner_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your review")
    return review


async def owned_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """The job named by ``{job_id}``, if the caller owns its review."""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    review = await db.get(Review, job.review_id)
    if review is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    if review.owner_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your job")
    return job
