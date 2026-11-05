"""Single-occupancy lease over the host's shared ``claude`` CLI login.

The CLI authenticates from the SERVER's ``$HOME``, so LLM work runs as one
Claude account for the whole host. Without a lease, a second user's run silently
bills the first user's subscription. These tests pin the behaviour that prevents
that — and, just as importantly, that the lease never blocks people it
shouldn't.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_lease_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app._time import utcnow  # noqa: E402
from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import llm_lease  # noqa: E402


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _user(client, email):
    """Register a user and return auth headers + their review id."""
    r = await client.post(
        "/auth/register", json={"email": email, "password": "hunter2pw"}
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "L Test", "topic_name": f"L {email}",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


@pytest.fixture(autouse=True)
def _no_cli_probe(monkeypatch):
    """Never shell out to the real `claude` binary from tests."""
    async def fake_probe():
        return llm_lease.CliAuth(True, True, "tester@example.com", "")

    monkeypatch.setattr(llm_lease, "probe_cli_auth", fake_probe)
    monkeypatch.setattr(llm_lease, "probe_codex_auth", fake_probe)


@pytest.fixture(autouse=True)
async def _free_lease():
    """The lease is a SINGLETON row in a DB shared across the whole test run, so
    a held lease leaks — not just between tests here, but into OTHER test files,
    where the enqueue gate would then 409 (observed: test_engine's enqueue tests
    failing only when run after this file). Free it before AND after each test."""
    await init_models()
    from server.app.db.session import AsyncSessionLocal

    async def _release():
        async with AsyncSessionLocal() as s:
            await llm_lease.release(s, "", force=True)

    await _release()
    yield
    await _release()


# --- CLI discovery (WSL-specific) ------------------------------------------

@pytest.mark.parametrize(
    "path, rejected",
    [
        # A Windows install seen through WSL interop cannot run from Linux.
        ("/mnt/c/Users/g/AppData/Local/claude.exe", True),
        ("/mnt/c/Program Files/claude", True),
        ("/mnt/d/tools/claude", True),
        (r"C:\Users\g\claude.exe", True),
        # Real Linux paths must be accepted...
        ("/home/u/.local/bin/claude", False),
        ("/usr/local/bin/claude", False),
        # ...including a mount whose name is NOT a drive letter.
        ("/mnt/data/claude", False),
        ("/mnt/storage/bin/claude", False),
    ],
)
def test_windows_binaries_are_rejected(path: str, rejected: bool) -> None:
    """On WSL, `shutil.which` can return the WINDOWS claude.exe. Using it would
    make the studio report a healthy CLI and then fail at job time."""
    assert llm_lease._is_windows_binary(path) is rejected


def test_find_cli_looks_beyond_PATH(tmp_path, monkeypatch) -> None:
    """The official installer drops claude in ~/.local/bin and does NOT add it to
    PATH; a background worker's PATH is often minimal. PATH alone is not enough."""
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    binary = home / ".local/bin/claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    monkeypatch.setattr(llm_lease.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("BER_CLAUDE_BIN", raising=False)

    assert llm_lease.find_cli() == str(binary)


def test_find_cli_returns_none_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(llm_lease.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("BER_CLAUDE_BIN", raising=False)
    assert llm_lease.find_cli() is None


# --- status ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_starts_unheld():
    await init_models()
    async with _client() as c:
        h, _ = await _user(c, "lease_a@example.com")
        r = await c.get("/llm/session", headers=h)
        assert r.status_code == 200
        d = r.json()
        assert d["held"] is False and d["held_by_me"] is False
        assert d["ttl_minutes"] == llm_lease.LEASE_TTL_MIN
        # The login command is surfaced because it can't be done from the UI.
        assert "codex login" in d["login_hint"]


@pytest.mark.asyncio
async def test_claim_then_release_round_trip():
    await init_models()
    async with _client() as c:
        h, _ = await _user(c, "lease_b@example.com")

        r = await c.post("/llm/session/claim", headers=h, json={})
        assert r.status_code == 200, r.text
        assert r.json()["held_by_me"] is True
        assert r.json()["claude_account"] == "tester@example.com"

        r = await c.post("/llm/session/release", headers=h)
        assert r.status_code == 200 and r.json()["held"] is False

        # Release is idempotent — a second call must not error.
        assert (await c.post("/llm/session/release", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_second_user_is_refused_and_told_who_holds_it():
    await init_models()
    async with _client() as c:
        h1, _ = await _user(c, "lease_c1@example.com")
        h2, _ = await _user(c, "lease_c2@example.com")

        await c.post("/llm/session/claim", headers=h1, json={})
        r = await c.post("/llm/session/claim", headers=h2, json={})
        assert r.status_code == 409
        # "someone else is using it" is not actionable; name them — but
        # MASKED: a non-admin may recognize a colleague, not harvest an address.
        assert "l***@e***.com" in r.text
        assert "lease_c1@example.com" not in r.text

        # …and the second user's view reflects it, masked the same way.
        d = (await c.get("/llm/session", headers=h2)).json()
        assert d["held"] is True and d["held_by_me"] is False
        assert d["holder_email"] == "l***@e***.com"
        # The holder sees their own address in full.
        d1 = (await c.get("/llm/session", headers=h1)).json()
        assert d1["holder_email"] == "lease_c1@example.com"


@pytest.mark.asyncio
async def test_concurrent_claims_have_exactly_one_winner():
    """The claim is one conditional UPDATE: two users racing for a free lease
    cannot both pass a check-then-write and silently steal from each other."""
    import asyncio

    from server.app.db.session import AsyncSessionLocal

    await init_models()
    u1, u2 = "race-user-1", "race-user-2"
    async with AsyncSessionLocal() as s:
        await llm_lease.release(s, "x", force=True)

    async def claim(uid):
        async with AsyncSessionLocal() as s:
            try:
                await llm_lease.acquire(s, uid, None)
                return uid
            except llm_lease.LeaseHeld:
                return None

    winners = [w for w in await asyncio.gather(claim(u1), claim(u2)) if w]
    assert len(winners) == 1
    async with AsyncSessionLocal() as s:
        assert (await llm_lease.current(s)).holder_user_id == winners[0]
        await llm_lease.release(s, winners[0])


# --- expiry ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_lease_can_be_taken_over():
    """A forgotten session must never block the team permanently."""
    await init_models()
    async with _client() as c:
        h1, _ = await _user(c, "lease_d1@example.com")
        h2, _ = await _user(c, "lease_d2@example.com")
        await c.post("/llm/session/claim", headers=h1, json={})

        # Age the lease past the TTL.
        from server.app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as s:
            lease = await llm_lease.current(s)
            lease.last_seen_at = utcnow() - timedelta(
                minutes=llm_lease.LEASE_TTL_MIN + 5
            )
            await s.commit()

        d = (await c.get("/llm/session", headers=h2)).json()
        assert d["expired"] is True

        r = await c.post("/llm/session/claim", headers=h2, json={"force": True})
        assert r.status_code == 200 and r.json()["held_by_me"] is True


@pytest.mark.asyncio
async def test_force_cannot_yank_a_live_lease():
    await init_models()
    async with _client() as c:
        h1, _ = await _user(c, "lease_e1@example.com")
        h2, _ = await _user(c, "lease_e2@example.com")
        await c.post("/llm/session/claim", headers=h1, json={})

        r = await c.post("/llm/session/claim", headers=h2, json={"force": True})
        assert r.status_code == 409


# --- the gate --------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_user_is_never_blocked():
    """A user with their own key doesn't touch the shared CLI, so the lease
    must not apply to them — otherwise the gate makes the app unusable for
    exactly the people it doesn't protect."""
    await init_models()
    async with _client() as c:
        h1, _ = await _user(c, "lease_f1@example.com")
        h2, _ = await _user(c, "lease_f2@example.com")
        await c.post("/llm/session/claim", headers=h1, json={})

        # h2 switches to OpenRouter with a key.
        r = await c.patch("/auth/settings", headers=h2, json={
            "llm_provider": "openrouter", "openrouter_key": "sk-or-v1-x",
        })
        assert r.status_code == 200

        d = (await c.get("/llm/session", headers=h2)).json()
        assert d["provider"] == "openrouter"

        from server.app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as s:
            assert await llm_lease.blocked_reason(s, "any-user", "openrouter") is None


@pytest.mark.asyncio
async def test_blocked_reason_only_fires_for_other_holders():
    await init_models()
    from server.app.db.session import AsyncSessionLocal
    async with _client() as c:
        h1, _ = await _user(c, "lease_g1@example.com")
        await c.post("/llm/session/claim", headers=h1, json={})
        async with AsyncSessionLocal() as s:
            lease = await llm_lease.current(s)
            holder = lease.holder_user_id
            # The holder themselves is never blocked.
            assert await llm_lease.blocked_reason(s, holder, "claude_cli") is None
            # Anyone else is.
            reason = await llm_lease.blocked_reason(s, "someone-else", "claude_cli")
            assert reason and "l***@e***.com" in reason
            assert "lease_g1@example.com" not in reason
            # An admin asking gets the real address.
            revealed = await llm_lease.blocked_reason(
                s, "someone-else", "claude_cli", reveal=True)
            assert "lease_g1@example.com" in revealed
            assert await llm_lease.blocked_reason(s, holder, "codex_cli") is None
            codex_reason = await llm_lease.blocked_reason(s, "someone-else", "codex_cli")
            assert codex_reason and "l***@e***.com" in codex_reason
