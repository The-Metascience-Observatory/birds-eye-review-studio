"""Per-IP rate limiting for the auth endpoints.

WHY
---
``/auth/login`` accepts unlimited password guesses, and each attempt costs a full
argon2 verify — so it is both a credential-stuffing target and a CPU-exhaustion
vector. ``/auth/register`` is the other unauthenticated write endpoint. Neither
had any limit before the studio was hosted publicly.

DESIGN
------
* **Disabled by default** (empty limit strings). The test suite registers many
  users in tight loops and would otherwise trip the limits; a public deploy sets
  ``RATELIMIT_REGISTER`` / ``RATELIMIT_LOGIN`` explicitly. Same
  empty-means-disabled idiom as ``SIGNUP_CODE`` and ``CORS_ORIGIN_REGEX``.
* **Redis-backed, not in-memory.** ``dev.sh`` runs the API alongside two worker
  processes, and in-memory counters would be neither shared between them nor
  survive a restart. Redis is already a hard dependency (``REDIS_URL``).
* **Never fail closed on infrastructure.** If Redis is unreachable the limiter
  must not take the API down with it — losing rate limiting is bad, refusing all
  logins is worse. ``limits`` raises on a dead backend, so the decorator is
  wrapped and errors are logged and swallowed.

The limit strings use slowapi/limits syntax: "10/hour", "5/minute",
"100/hour;10/minute" for several at once.
"""

from __future__ import annotations

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from .config import settings

logger = logging.getLogger(__name__)


def client_key(request: Request) -> str:
    """The per-client rate-limit key: the real remote address.

    ``get_remote_address`` reads the SOCKET peer. Behind the Cloudflare tunnel
    and the studio's ``/api/*`` rewrite, that peer is always ``127.0.0.1`` — so
    every visitor in the world collapses into one bucket and ten failed logins
    from one attacker lock out everybody, while the attacker themself is
    throttled no more than anyone else. Both halves of that are wrong.

    ``CF-Connecting-IP`` is Cloudflare's client address and, unlike
    ``X-Forwarded-For``, is *overwritten* (not appended to) at the edge, so a
    client cannot forge it through the tunnel. It is trusted ONLY when
    ``TRUST_PROXY_HEADERS`` is set: on a directly reachable socket the header is
    pure attacker input, and trusting it would hand out a fresh quota per forged
    value, which is strictly worse than no rate limiting at all. Default off,
    opt in with the deployment.

    ``X-Forwarded-For`` is deliberately NOT accepted, even under
    ``TRUST_PROXY_HEADERS``. The unforgeability argument above is specific to
    Cloudflare's overwrite behaviour and does not carry over: XFF is *appended
    to* by every well-behaved proxy, so its left-most entry is whatever the
    client sent. Honouring it re-opened the exact hole the paragraph above
    describes — vary the header, get an unlimited login quota and unlimited
    argon2 CPU burn. A request that arrives without ``CF-Connecting-IP`` falls
    through to the socket peer, which under-counts but cannot be forged.
    """
    if settings.TRUST_PROXY_HEADERS:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
    return get_remote_address(request)


def _storage_uri() -> str:
    """Where the counters live. Explicit override, else Redis, else in-memory."""
    if settings.RATELIMIT_STORAGE_URI:
        return settings.RATELIMIT_STORAGE_URI
    if settings.REDIS_URL:
        return settings.REDIS_URL
    return "memory://"


# One shared limiter. ``enabled`` is flipped per-request by the decorators below
# reading settings live, so tests can monkeypatch the limits without rebuilding.
limiter = Limiter(
    key_func=client_key,
    storage_uri=_storage_uri(),
    # We attach limits per-route rather than globally.
    default_limits=[],
    # Do not let a limiter problem become an API outage (see module docstring).
    swallow_errors=True,
)


# A limit string is REQUIRED by the decorator even when we intend to skip, so
# the callables below fall back to an effectively-infinite value and the
# matching ``exempt_when`` short-circuits before it is ever consulted. Settings
# are read live (not captured at import) so tests can monkeypatch them.
_NEVER = "1000000/hour"


def register_limit() -> str:
    return settings.RATELIMIT_REGISTER or _NEVER


def register_disabled() -> bool:
    return not settings.RATELIMIT_REGISTER


def login_limit() -> str:
    return settings.RATELIMIT_LOGIN or _NEVER


def login_disabled() -> bool:
    return not settings.RATELIMIT_LOGIN


# --- Per-USER limits on expensive authenticated endpoints --------------------
# The decorators above key on the client address because /auth/* is anonymous.
# The LLM endpoints are authenticated, and the resource being protected is the
# account's (or the operator's) model spend — so they key on the user id: one
# user cannot exhaust another's quota by sharing an office NAT, and cannot dodge
# their own by changing IP.

def hit_or_raise(spec: str, scope: str, key: str) -> None:
    """Count one hit against *spec* ("20/minute;300/hour") for (*scope*, *key*).

    Raises 429 when any window is exhausted. Empty *spec* = disabled. Like the
    decorators above, a broken counter backend is logged and ignored rather
    than turning into an outage.
    """
    if not spec:
        return
    from fastapi import HTTPException, status
    from limits import parse_many

    try:
        items = parse_many(spec)
        exceeded = [i for i in items if not limiter.limiter.hit(i, scope, key)]
    except Exception:  # noqa: BLE001 - never fail closed on infrastructure
        logger.warning("rate limiter unavailable for %s; allowing", scope, exc_info=True)
        return
    if exceeded:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many requests — this action is limited to {spec}. "
            "Wait a little and try again.",
        )


def per_user(setting_name: str, scope: str):
    """A dependency enforcing ``settings.<setting_name>`` per authenticated user.

    The setting is read per request (not captured), so tests and
    ``apply_public_defaults`` can change it after import.
    """
    from fastapi import Depends

    from .db.models import User
    from .deps import get_current_user

    async def _dep(user: User = Depends(get_current_user)) -> None:
        hit_or_raise(getattr(settings, setting_name), scope, user.id)

    return _dep


#: The per-user limit shared by every endpoint that spends LLM money.
llm_limit = per_user("RATELIMIT_LLM", "llm")
