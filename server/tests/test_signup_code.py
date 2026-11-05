"""Access-code gating on /auth/register, and the /auth/* rate limits.

Both features are DISABLED by default (empty-string settings) because the rest of
the suite registers users freely — 13 other test files POST /auth/register with
only email+password. The first test here pins that default; everything else
enables the feature explicitly.

Settings are toggled with monkeypatch.setattr, never os.environ: `settings` is
lru_cache'd and frozen at first import (see the warning in app/config.py), so an
env var set at test time would be silently ignored.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_signup_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app.config import settings  # noqa: E402
from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.ratelimit import limiter  # noqa: E402


CODE = "let-me-in-9f3a2b"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def gated(monkeypatch):
    """Turn signup gating on for one test, restored automatically."""
    monkeypatch.setattr(settings, "SIGNUP_CODE", CODE)


# --- the default: open signup (protects the other 13 test files) -----------

async def test_open_signup_needs_no_code() -> None:
    """With SIGNUP_CODE empty (the default), registration is unchanged.

    This is the guarantee the rest of the suite depends on — if it breaks, 13
    other test files break with it.
    """
    await init_models()
    assert settings.SIGNUP_CODE == "", "default must be open, or the suite breaks"
    async with _client() as c:
        r = await c.post(
            "/auth/register",
            json={"email": "open_default@example.com", "password": "hunter2pw"},
        )
        assert r.status_code == 201, r.text


async def test_open_signup_ignores_a_supplied_code() -> None:
    """An old client that sends a code to an ungated server still works."""
    await init_models()
    async with _client() as c:
        r = await c.post("/auth/register", json={
            "email": "open_extra@example.com",
            "password": "hunter2pw",
            "invite_code": "whatever",
        })
        assert r.status_code == 201, r.text


# --- gated -----------------------------------------------------------------

async def test_gated_rejects_missing_code(gated) -> None:
    await init_models()
    async with _client() as c:
        r = await c.post("/auth/register", json={
            "email": "gated_none@example.com", "password": "hunter2pw",
        })
        assert r.status_code == 403


async def test_gated_rejects_wrong_code_with_identical_message(gated) -> None:
    """Missing and wrong must be INDISTINGUISHABLE.

    A different message for each would tell an attacker which half they got
    right, and is how a per-branch string leak creeps back in.
    """
    await init_models()
    async with _client() as c:
        missing = await c.post("/auth/register", json={
            "email": "gated_a@example.com", "password": "hunter2pw",
        })
        wrong = await c.post("/auth/register", json={
            "email": "gated_b@example.com", "password": "hunter2pw",
            "invite_code": "not-the-code",
        })
    assert missing.status_code == wrong.status_code == 403
    assert missing.json()["detail"] == wrong.json()["detail"]


async def test_gated_accepts_the_right_code(gated) -> None:
    await init_models()
    async with _client() as c:
        r = await c.post("/auth/register", json={
            "email": "gated_ok@example.com", "password": "hunter2pw",
            "invite_code": CODE,
        })
        assert r.status_code == 201, r.text
        # Signup still logs you in.
        assert r.json()["access_token"]


async def test_code_is_whitespace_tolerant(gated) -> None:
    """Users paste codes out of email; surrounding whitespace must not matter."""
    await init_models()
    async with _client() as c:
        r = await c.post("/auth/register", json={
            "email": "gated_ws@example.com", "password": "hunter2pw",
            "invite_code": f"  {CODE}\n",
        })
        assert r.status_code == 201, r.text


async def test_code_check_precedes_duplicate_email(gated) -> None:
    """The gate must run BEFORE the duplicate-email query.

    Otherwise /auth/register is an email-enumeration oracle: an anonymous
    caller could distinguish "already registered" from "created" without ever
    holding a valid code.
    """
    await init_models()
    async with _client() as c:
        # Create the account first, with a valid code.
        first = await c.post("/auth/register", json={
            "email": "dupe@example.com", "password": "hunter2pw",
            "invite_code": CODE,
        })
        assert first.status_code == 201

        # Now retry that email WITHOUT a valid code.
        again = await c.post("/auth/register", json={
            "email": "dupe@example.com", "password": "hunter2pw",
            "invite_code": "wrong",
        })
        assert again.status_code == 403
        # The response must not reveal that the address exists.
        assert "already registered" not in again.text.lower()


# --- rate limiting ---------------------------------------------------------

async def test_rate_limit_off_by_default() -> None:
    """Limits must be inert unless configured, or the suite trips them."""
    await init_models()
    assert settings.RATELIMIT_LOGIN == ""
    assert settings.RATELIMIT_REGISTER == ""
    async with _client() as c:
        codes = [
            (await c.post("/auth/login", json={
                "email": f"rl_off{i}@example.com", "password": "nope",
            })).status_code
            for i in range(6)
        ]
    assert 429 not in codes, codes


async def test_login_rate_limit_returns_429(monkeypatch) -> None:
    """Login is the argon2-cost endpoint: unlimited guessing is both a
    credential-stuffing risk and a CPU-exhaustion vector.

    NOTE: the limiter's counters are keyed by client IP and live in shared
    storage, so they persist across tests in a run. Reset them here rather than
    asserting on how many requests precede the 429 — an earlier test in the same
    module (or file order) would otherwise have already spent the budget.
    """
    await init_models()
    monkeypatch.setattr(settings, "RATELIMIT_LOGIN", "3/minute")
    limiter.reset()
    async with _client() as c:
        codes = [
            (await c.post("/auth/login", json={
                "email": f"rl_on{i}@example.com", "password": "nope",
            })).status_code
            for i in range(6)
        ]
    assert 429 in codes, codes
    # Requests under the limit still get the normal auth failure, not 429.
    assert codes[0] == 401, codes
