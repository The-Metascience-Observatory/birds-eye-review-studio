"""User-selectable LLM provider + OpenRouter BYO-key.

Covers the four moving parts added for provider selection:
* ``app.crypto`` round-trips a secret and fails closed on garbage;
* ``claude_cli._openrouter_call_claude`` (sync) parses text/cost/tokens and
  honors the alias→slug map, with a bounded — not 30-minute — retry;
* ``async_call_claude`` dispatches on ``BER_LLM_PROVIDER`` (openrouter vs cli);
* ``PATCH/GET /auth/settings`` store the key encrypted, never echo it, and
  reject selecting OpenRouter with no key;
* the in-server agent stamps the effective provider and does not resume on it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_provider_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402

# Repo root on path for the claude_cli import (mirrors the server's shim).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ber_core import claude_cli  # noqa: E402
from server.app import crypto  # noqa: E402
from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import agent as agent_svc  # noqa: E402

# NOTE: the async API tests below carry an explicit @pytest.mark.asyncio; the
# pure-unit tests (crypto, sync provider, dispatch) stay synchronous.


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _setup(client, email):
    await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "P Test", "topic_name": "P Test",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


@pytest.mark.asyncio
async def test_codex_settings_and_session_use_host_codex(monkeypatch):
    from server.app.services import llm_lease, llm_prefs
    assert not llm_prefs.is_byo("codex_cli")

    async def codex_status():
        return llm_lease.CliAuth(True, True, None, "Codex configured")

    async def wrong_status(*args, **kwargs):
        raise AssertionError("Codex settings must not probe Claude")

    monkeypatch.setattr(llm_lease, "probe_codex_auth", codex_status)
    monkeypatch.setattr(llm_lease, "probe_cli_auth", wrong_status)
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "codex-provider@test.example")
        saved = await client.patch("/auth/settings", headers=headers,
                                   json={"llm_provider": "codex_cli"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["llm_provider"] == "codex_cli"
        status = await client.get("/llm/session", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["provider"] == "codex_cli"
        assert status.json()["cli_logged_in"] is True
        assert status.json()["login_hint"] == "codex login"


# ── crypto ───────────────────────────────────────────────────────────────────

def test_crypto_roundtrip_and_fails_closed():
    tok = crypto.encrypt("sk-or-v1-secret")
    assert tok != "sk-or-v1-secret"  # actually ciphertext
    assert crypto.decrypt(tok) == "sk-or-v1-secret"
    assert crypto.decrypt("not-a-token") is None
    assert crypto.decrypt("") is None
    assert crypto.decrypt(None) is None


# ── OpenRouter provider (sync) ───────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_openrouter_sync_parses_and_maps_slug(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["model"] = json["model"]
        seen["auth"] = headers.get("Authorization")
        return _FakeResp(payload={
            "choices": [{"message": {"content": "  PONG  "}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "cost": 0.0007},
        })

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("BER_LLM_PROVIDER", "openrouter")

    resp = claude_cli.call_claude("hi", "sonnet", "id")
    assert seen["url"] == claude_cli.OPENROUTER_URL
    assert seen["model"] == claude_cli.OPENROUTER_MODELS["sonnet"]  # alias mapped
    assert seen["auth"] == "Bearer sk-or-test"
    assert resp.text == "PONG"  # stripped
    assert resp.cost_usd == pytest.approx(0.0007)
    assert resp.input_tokens == 11 and resp.output_tokens == 4


def test_openrouter_missing_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("BER_LLM_PROVIDER", "openrouter")
    resp = claude_cli.call_claude("hi", "sonnet", "id")
    assert resp.text == ""  # empty-response contract, no crash


def test_openrouter_client_error_no_retry_storm(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _FakeResp(status=400, text="bad request")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: None)  # no real waits
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("BER_LLM_PROVIDER", "openrouter")
    resp = claude_cli.call_claude("hi", "sonnet", "id")
    assert resp.text == ""
    assert calls["n"] == 1  # a 4xx is not retried


def test_openrouter_passthrough_slug():
    # A full slug is used verbatim (config may already carry one).
    assert claude_cli._openrouter_model_slug("anthropic/claude-x") == "anthropic/claude-x"
    assert claude_cli._openrouter_model_slug("haiku") == claude_cli.OPENROUTER_MODELS["haiku"]


# ── async dispatch ───────────────────────────────────────────────────────────

def test_async_dispatches_on_provider(monkeypatch):
    routed = {}

    def fake_or(prompt, model, identifier, timeout=120, resume_session=None, use_stdin=False):
        routed["provider"] = "openrouter"
        return claude_cli.ClaudeResponse(text="OR")

    async def fake_cli(*a, **k):
        routed["provider"] = "cli"
        return claude_cli.ClaudeResponse(text="CLI")

    monkeypatch.setattr(claude_cli, "_openrouter_call_claude", fake_or)
    monkeypatch.setattr(claude_cli, "_async_cli_call_claude", fake_cli)

    monkeypatch.setenv("BER_LLM_PROVIDER", "openrouter")
    r = asyncio.run(
        claude_cli.async_call_claude("hi", "sonnet", "id")
    )
    assert routed["provider"] == "openrouter" and r.text == "OR"

    monkeypatch.setenv("BER_LLM_PROVIDER", "claude_cli")
    r = asyncio.run(
        claude_cli.async_call_claude("hi", "sonnet", "id")
    )
    assert routed["provider"] == "cli" and r.text == "CLI"


# ── settings endpoints ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_default_and_key_lifecycle():
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "settings1@example.com")

        r = await client.get("/auth/settings", headers=headers)
        assert r.status_code == 200
        # Exact equality on purpose: this pins the whole settings shape, so a
        # field added server-side cannot silently go missing from types.ts.
        assert r.json() == {
            "llm_provider": "codex_cli",
            "has_openrouter_key": False,
            "has_claude_token": False,
            "has_anthropic_key": False,
            "claude_token_account": None,
            "anthropic_key_account": None,
            "claude_token_expires_at": None,
            "pdf_dir": None,
            "pdf_dir_warning": None,
        }

        # Can't select openrouter with no key.
        r = await client.patch("/auth/settings", headers=headers,
                               json={"llm_provider": "openrouter"})
        assert r.status_code == 422

        # Add a key + select openrouter in one shot.
        r = await client.patch("/auth/settings", headers=headers,
                               json={"llm_provider": "openrouter",
                                     "openrouter_key": "sk-or-v1-abc"})
        assert r.status_code == 200
        assert r.json() == {
            "llm_provider": "openrouter",
            "has_openrouter_key": True,
            "has_claude_token": False,
            "has_anthropic_key": False,
            "claude_token_account": None,
            "anthropic_key_account": None,
            "claude_token_expires_at": None,
            "pdf_dir": None,
            "pdf_dir_warning": None,
        }
        # The raw key is NEVER echoed.
        assert "sk-or-v1-abc" not in r.text

        # Clearing the key with "" leaves provider but drops the stored key.
        r = await client.patch("/auth/settings", headers=headers,
                               json={"openrouter_key": ""})
        assert r.json()["has_openrouter_key"] is False


@pytest.mark.asyncio
async def test_settings_stores_ciphertext_not_plaintext():
    await init_models()
    from server.app.db.session import AsyncSessionLocal
    from server.app.db.models import User
    from sqlalchemy import select

    async with _client() as client:
        headers, _ = await _setup(client, "settings2@example.com")
        await client.patch("/auth/settings", headers=headers,
                           json={"llm_provider": "openrouter",
                                 "openrouter_key": "sk-or-v1-plZ"})

    from server.app.db.models import UserCredential

    async with AsyncSessionLocal() as db:
        u = (await db.execute(
            select(User).where(User.email == "settings2@example.com")
        )).scalar_one()
        # Written to user_credentials, never to the legacy users column.
        assert u.openrouter_key_encrypted is None
        row = (await db.execute(select(UserCredential).where(
            UserCredential.user_id == u.id, UserCredential.kind == "openrouter"
        ))).scalar_one()
        assert row.secret_encrypted != "sk-or-v1-plZ"  # encrypted at rest
        assert crypto.decrypt(row.secret_encrypted) == "sk-or-v1-plZ"


async def test_legacy_openrouter_column_is_still_read_as_a_fallback():
    """A key saved before the move to user_credentials keeps working, and the
    next save moves it (and clears the legacy column)."""
    from server.app.db.session import AsyncSessionLocal
    from server.app.db.models import User
    from server.app.services import llm_prefs
    from sqlalchemy import select

    async with _client() as client:
        headers, _ = await _setup(client, "legacy_or@example.com")
    async with AsyncSessionLocal() as db:
        u = (await db.execute(
            select(User).where(User.email == "legacy_or@example.com"))).scalar_one()
        u.openrouter_key_encrypted = crypto.encrypt("sk-or-legacy")
        await db.commit()
        assert await llm_prefs.openrouter_key_for(db, u) == "sk-or-legacy"
        assert await llm_prefs.has_openrouter_key(db, u)
    async with _client() as client:
        r = await client.get("/auth/settings", headers=headers)
        assert r.json()["has_openrouter_key"] is True
        await client.patch("/auth/settings", headers=headers,
                           json={"openrouter_key": "sk-or-new"})
    async with AsyncSessionLocal() as db:
        u = (await db.execute(
            select(User).where(User.email == "legacy_or@example.com"))).scalar_one()
        assert u.openrouter_key_encrypted is None
        assert await llm_prefs.openrouter_key_for(db, u) == "sk-or-new"


# ── agent honors provider ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_stamps_effective_provider(monkeypatch):
    await init_models()

    captured = {}

    async def fake_llm(prompt, *, identifier, resume_session):
        # Record what the agent decided about resume + the credential scope the
        # call runs under. Read through claude_cli, not os.environ: the scope is
        # held in contextvars now precisely so it is NOT process-global (see
        # llm_prefs.provider_env). This is what the dispatcher and the child
        # process actually see.
        captured["resume"] = resume_session
        captured["env_provider"] = claude_cli.effective_provider()
        captured["env_key"] = claude_cli._child_env().get("OPENROUTER_API_KEY")
        return SimpleNamespace(text="hi there", cost_usd=0.02, session_id=None)

    monkeypatch.setattr(agent_svc, "_llm", fake_llm)

    async with _client() as client:
        headers, rid = await _setup(client, "agentor@example.com")
        # Turn the owner into an OpenRouter user with a key.
        await client.patch("/auth/settings", headers=headers,
                           json={"llm_provider": "openrouter",
                                 "openrouter_key": "sk-or-v1-live"})
        r = await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                              json={"stage": "search", "message": "why so few?"})
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["provider"] == "openrouter"
        # OpenRouter has no --resume: the agent must not pass a resume id.
        assert captured["resume"] is None
        # The key + provider were in the env at call time.
        assert captured["env_provider"] == "openrouter"
        assert captured["env_key"] == "sk-or-v1-live"
    # And nothing leaked: the scope unwound with the request, and the process
    # environment was never touched in the first place.
    assert claude_cli._child_env().get("OPENROUTER_API_KEY") is None
    assert os.environ.get("OPENROUTER_API_KEY") is None


# ── PDF directory setting ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pdf_dir_round_trip_and_clear(tmp_path):
    """Save, read back, and clear the account-level PDF folder."""
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "pdfdir1@example.com")
        target = str(tmp_path / "my_pdfs")

        r = await client.patch("/auth/settings", headers=headers,
                               json={"pdf_dir": target})
        assert r.status_code == 200, r.text
        assert r.json()["pdf_dir"] == target

        # Persists across requests.
        r = await client.get("/auth/settings", headers=headers)
        assert r.json()["pdf_dir"] == target

        # "" restores the per-review default.
        r = await client.patch("/auth/settings", headers=headers,
                               json={"pdf_dir": ""})
        assert r.status_code == 200 and r.json()["pdf_dir"] is None


@pytest.mark.asyncio
async def test_pdf_dir_rejects_system_directory():
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "pdfdir2@example.com")
        r = await client.patch("/auth/settings", headers=headers,
                               json={"pdf_dir": "/etc"})
        assert r.status_code == 422
        assert "system directory" in r.text


@pytest.mark.asyncio
async def test_pdf_dir_translates_windows_path_and_warns():
    """A Windows path typed into the WSL-hosted UI is converted, with advice."""
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "pdfdir3@example.com")
        r = await client.patch("/auth/settings", headers=headers,
                               json={"pdf_dir": r"C:\Users\someone\PDFs"})
        # /mnt/c may not exist in CI; either it saved (with a warning) or it
        # was rejected as unwritable — never silently stored unconverted.
        if r.status_code == 200:
            assert r.json()["pdf_dir"] == "/mnt/c/Users/someone/PDFs"
            assert "symlink" in (r.json()["pdf_dir_warning"] or "").lower()
        else:
            assert r.status_code == 422


@pytest.mark.asyncio
async def test_pdf_dir_invalid_path_does_not_apply_other_fields(tmp_path):
    """A rejected path must not half-apply the rest of the patch."""
    await init_models()
    async with _client() as client:
        headers, _ = await _setup(client, "pdfdir4@example.com")
        r = await client.patch("/auth/settings", headers=headers,
                               json={"pdf_dir": "/etc",
                                     "openrouter_key": "sk-or-v1-should-not-save"})
        assert r.status_code == 422
        r = await client.get("/auth/settings", headers=headers)
        assert r.json()["has_openrouter_key"] is False
        assert r.json()["pdf_dir"] is None
