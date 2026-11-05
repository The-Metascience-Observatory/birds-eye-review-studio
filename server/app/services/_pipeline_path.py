"""Put the pipeline repo on ``sys.path`` — once, for every service that imports it.

Importing the pipeline's ``ber_core`` package (``ber_core.pipeline``,
``ber_core.utils``, …) needs the repo root on ``sys.path``. That insert
was copy-pasted into ~8 service modules and the copies drifted, so it lives here.
A service that needs the pipeline calls :func:`ensure_pipeline_on_path` before
importing from it (an explicit call rather than an import-for-side-effect, which
linters flag as unused and a well-meaning cleanup would delete). ``REPO_ROOT``
is exported for the callers that build paths from it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..config import settings

REPO_ROOT = Path(settings.REPO_ROOT)


def ensure_pipeline_on_path() -> Path:
    """Make ``from ber_core import …`` work.
    Idempotent; returns the repo root."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return REPO_ROOT


ensure_pipeline_on_path()
