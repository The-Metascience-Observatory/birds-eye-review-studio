"""Time helpers shared across the app.

`utcnow()` was copy-pasted as a private `_now()` in four modules (models, runner,
publishing, admin). One definition here removes the drift risk — an aware UTC
`datetime`, which is what every timestamp column and decision record expects.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
