"""Test setup for the repo-root pipeline tests in ``tests/``.

These tests are written to run from the repo root, and they depend on it in
two ways:

1. **Package imports** — ``from ber_core import utils`` resolves only when the
   repo root is on ``sys.path`` (nothing is installed).
2. **Relative script loads** — several tests load a step script with
   ``importlib.util.spec_from_file_location(name, "steps/classify.py")``, a
   relative path (step scripts are not importable package modules).

So the repo root goes on ``sys.path`` (fixes 1) and becomes the process CWD for
the session (fixes 2). ``conftest.py`` is imported before collection, so this
runs early enough. No test reads real project data: everything is built in
``tmp_path``.

``server/tests/`` has its OWN conftest and is unaffected — this file only governs
tests collected under this ``tests/`` directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/ -> repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# 1. Package imports (ber_core.*).
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 2. The tests load step scripts by paths relative to the repo root, so run
# them with the repo root as CWD. Session-wide and unconditional: these tests
# have no meaning from any other directory.
os.chdir(_REPO_ROOT)


import pytest

@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """Workers for ``-n auto``: up to 7 cores, n-1 on machines with < 8."""
    return min(7, max(1, (os.cpu_count() or 2) - 1))


# Unit tests never read operator credentials from either environment or dotenv.
import dotenv
from ber_core.environment import MANAGED_VARS
for _secret_name in MANAGED_VARS:
    os.environ.pop(_secret_name, None)
dotenv.load_dotenv = lambda *args, **kwargs: False
