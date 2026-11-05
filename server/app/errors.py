"""What an error is allowed to say to the client.

Exception text routinely carries absolute filesystem paths, SQL fragments, CLI
stderr and internal identifiers. On a local instance that is exactly what a
developer wants to see; on a PUBLIC_MODE instance it is a map of the server for
anyone who can trigger a failure. ``main.py`` already masks UNHANDLED errors
this way — this module is the same rule for the errors a handler catches and
turns into an HTTPException or a ``Job.error`` itself.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from .config import settings

logger = logging.getLogger("server.app.errors")


def _ref() -> str:
    return uuid4().hex[:12]


def public_detail(exc: BaseException, what: str = "The operation") -> str:
    """A client-safe message for *exc*.

    PUBLIC_MODE: logs the full exception server-side against a random reference
    and returns only ``"<what> failed. Quote reference <ref> …"``. Otherwise
    returns ``"<what> failed (<Type>): <text>"`` — the raw detail a developer
    debugging locally needs.
    """
    if settings.PUBLIC_MODE:
        ref = _ref()
        logger.error("%s failed [ref=%s]: %s: %s", what, ref,
                     type(exc).__name__, exc, exc_info=exc)
        return f"{what} failed. Quote reference {ref} when reporting this."
    return f"{what} failed ({type(exc).__name__}): {exc}"


def public_text(text: str, what: str = "The operation") -> str:
    """Like :func:`public_detail` for raw diagnostic TEXT (CLI stderr, a
    provider's error body) rather than an exception object."""
    if settings.PUBLIC_MODE:
        ref = _ref()
        logger.error("%s [ref=%s]: %s", what, ref, text)
        return f"{what}. Quote reference {ref} when reporting this."
    return f"{what}: {text}" if text else what


def mask_email(email: str | None) -> str | None:
    """``alice@example.com`` -> ``a***@e***.com``: enough to recognize a
    colleague, not enough to harvest an address."""
    if not email:
        return email
    local, _, domain = email.partition("@")
    if not domain:
        return (local[:1] + "***") if local else "***"
    host, dot, tld = domain.rpartition(".")
    masked_domain = (host[:1] + "***" + dot + tld) if host else (domain[:1] + "***")
    return f"{local[:1]}***@{masked_domain}"
