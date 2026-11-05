"""Session-wide test isolation that MUST happen before the app is imported.

``server.app.config.settings`` is built ONCE at first import and cached for the
whole process. Each test module sets its own ``DATABASE_URL``/``DATA_ROOT``
before importing the app, but only the module pytest imports FIRST actually
wins — every later assignment is a no-op against the cached settings object.

That silently broke ``test_g3_worker_pipeline`` in two stacked ways:

1. ``REDIS_URL`` — only ``test_engine`` set it (db 15). Collected after another
   module, settings froze at the dev default (db 0), so the API enqueued to db 0
   while the test's worker subprocess consumed db 15. Jobs sat queued forever
   and leaked into the developer's real queue as "missing job/review".
2. ``DATABASE_URL`` — the API wrote Job rows into whichever module's sqlite file
   was imported first, while the worker subprocess (which gets an explicit env)
   read ``engine.db``. The worker dequeued a job id that did not exist in the DB
   it could see, and reported "missing job/review".

Both are fixed by pinning the shared, cross-process settings here: pytest
imports conftest before any test module, so this is immune to collection order.

The same mechanism pins the DEPLOYMENT settings (PUBLIC_MODE, the rate limits,
the signup gate) to their local-dev values, so the suite behaves identically on
a laptop and on the machine that actually hosts the studio. See the block below.

``DATA_ROOT`` is pinned for the same reason: ``workspace.workspace_dir`` reads
``settings.DATA_ROOT``, which freezes at first import — the per-module
``os.environ["DATA_ROOT"] = …`` assignments scattered through test modules only
ever won for whichever module was imported FIRST; every later one was a silent
no-op. Pinning here makes the effective root deterministic instead of
collection-order-dependent. (Worker SUBPROCESSES are unaffected: they get an
explicit env and read review paths from the DB rows, which are absolute.)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# One sqlite file for the whole test session. It must be a real FILE (not
# :memory:) because test_engine's worker runs in a SEPARATE PROCESS and has to
# see the same rows the in-process API writes.
#
# Each pytest/xdist process owns its SQLite workspace AND Redis process.
# No numbered database in an existing Redis server is touched.
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="ber_tests_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_SESSION_TMP / 'session.db'}"
os.environ["DATA_ROOT"] = str(_SESSION_TMP / "_data")

# Own a disposable Redis PROCESS, never flush a developer's numbered DB.
import atexit
import socket
import subprocess
import shutil
import time
_redis_process = None
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    _redis_port = sock.getsockname()[1]
os.environ["REDIS_URL"] = f"redis://127.0.0.1:{_redis_port}/0"
if shutil.which("redis-server"):
    _redis_process = subprocess.Popen([
        "redis-server", "--bind", "127.0.0.1", "--port", str(_redis_port),
        "--save", "", "--appendonly", "no", "--dir", str(_SESSION_TMP)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def _stop_test_redis():
        if _redis_process.poll() is None:
            _redis_process.terminate()
            _redis_process.wait(timeout=5)
    atexit.register(_stop_test_redis)
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", _redis_port), timeout=.05):
                break
        except OSError:
            time.sleep(.02)

os.environ.setdefault("JWT_SECRET", "test-secret")

# Deployment posture is pinned to the LOCAL defaults, unconditionally.
#
# `server/.env` is a real file on a machine that hosts this app, and
# pydantic-settings reads it — so a developer who configured their box for
# public hosting would otherwise run the suite under those settings. With
# PUBLIC_MODE=true that means signup fails closed and the password floor rises
# to 12, and the ~104 tests that register a throwaway account all fail with a
# 403 that has nothing to do with the code under test.
#
# Environment variables outrank the .env file in pydantic-settings, so assigning
# here (before the app is imported, like everything else in this module) makes
# the suite hermetic against however this machine happens to be deployed. Tests
# that WANT public behaviour opt in per-test with monkeypatch.setattr — see
# tests/test_invites.py.
os.environ["PUBLIC_MODE"] = "false"
os.environ["TRUST_PROXY_HEADERS"] = "false"
os.environ["MIN_PASSWORD_LEN"] = "0"
os.environ["SIGNUP_CODE"] = ""
os.environ["RATELIMIT_REGISTER"] = ""
os.environ["RATELIMIT_LOGIN"] = ""


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
