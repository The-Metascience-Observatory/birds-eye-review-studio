"""Per-user Claude credentials, and the precedence trap they depend on.

The host's `claude` CLI authenticates from the SERVER's home directory, so
without this feature every AI step on a shared instance spends the operator's
subscription. `claude setup-token` closes that: a user mints a one-year OAuth
token against their own plan, and injecting it as CLAUDE_CODE_OAUTH_TOKEN
outranks the host's login.

WHAT THIS FILE IS REALLY GUARDING. Claude Code picks its credential by a fixed
precedence, and CLAUDE_CODE_OAUTH_TOKEN sits at rank 5 — below ANTHROPIC_API_KEY
(3), ANTHROPIC_AUTH_TOKEN (2) and the CLAUDE_CODE_USE_* provider flags (1). The
server hands every pipeline step a copy of os.environ, which dev.sh has already
loaded the whole repo-root .env.local into. So a single ANTHROPIC_API_KEY in that
file would silently outrank every user's token and bill the operator instead —
no exception, no log line, nothing to notice. The tests below seed exactly those
variables and assert they are CLEARED.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_usercreds_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app import crypto  # noqa: E402
from server.app.db.models import UserCredential  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import llm_lease, llm_prefs, runner  # noqa: E402
from server.app.services._pipeline_path import ensure_pipeline_on_path  # noqa: E402

ensure_pipeline_on_path()
from ber_core import claude_cli  # noqa: E402  (pipeline module; needs the path above)


# The variables that outrank a user's subscription token, plus the token itself.
HIGHER_PRECEDENCE = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def polluted_env(monkeypatch):
    """An environment already carrying every higher-precedence credential var —
    i.e. what `dev.sh` would produce if any of them were in .env.local."""
    for var in HIGHER_PRECEDENCE:
        monkeypatch.setenv(var, f"host-value-for-{var}")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "someone-elses-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "someone-elses-or-key")


# --- the precedence trap ---------------------------------------------------

async def test_provider_env_clears_everything_that_outranks_the_token(polluted_env):
    """A user's token must be the credential that actually gets used.

    Asserted against the env handed to the `claude` CHILD PROCESS, which is the
    thing that decides which credential wins. `provider_env` deliberately no
    longer mutates `os.environ` (see its docstring — the shared dict leaked one
    tenant's token to the next across an await), so the exclusive-clearing rule
    is checked where it now takes effect. Same property, real boundary.
    """
    creds = {"CLAUDE_CODE_OAUTH_TOKEN": "user-token-abc"}
    with llm_prefs.provider_env("claude_token", creds):
        env = claude_cli._child_env()
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "user-token-abc"
        for var in HIGHER_PRECEDENCE:
            assert var not in env, f"{var} would outrank the user's token"
        # Another tenant's OpenRouter key must not ride along either.
        assert "OPENROUTER_API_KEY" not in env
        # And the provider the dispatcher will act on is this caller's.
        assert claude_cli.effective_provider() == "claude_token"


async def test_provider_env_does_not_touch_the_process_environment(polluted_env):
    """The whole point of the contextvar rewrite: nothing global changes.

    If this regresses, `runner.build_step_env` (which copies `os.environ`
    wholesale) starts shipping one tenant's credential into another tenant's
    pipeline subprocess.
    """
    before = dict(os.environ)
    with llm_prefs.provider_env("claude_token", {"CLAUDE_CODE_OAUTH_TOKEN": "user-token-abc"}):
        assert dict(os.environ) == before
        # Specifically: the previous occupant's value is still what os.environ says.
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "someone-elses-token"
    assert dict(os.environ) == before


async def test_concurrent_callers_do_not_see_each_others_credentials(polluted_env):
    """Two owners' in-process calls overlapping across an await.

    `agent.py` locks per REVIEW, so two different owners' turns genuinely
    interleave, and a turn holds its credential scope open across an await of up
    to LLM_TIMEOUT_S. Under the old os.environ implementation B's restore wrote
    A's token back as the durable process-wide value; here each task must see
    only its own.
    """
    import asyncio

    seen: dict[str, list] = {"a": [], "b": []}

    async def turn(name: str, token: str, first_sleep: float, second_sleep: float):
        await asyncio.sleep(first_sleep)
        with llm_prefs.provider_env("claude_token", {"CLAUDE_CODE_OAUTH_TOKEN": token}):
            seen[name].append(claude_cli._child_env()["CLAUDE_CODE_OAUTH_TOKEN"])
            await asyncio.sleep(second_sleep)  # the interleaving window
            seen[name].append(claude_cli._child_env()["CLAUDE_CODE_OAUTH_TOKEN"])

    # Interleaved on purpose: A enters, B enters, A exits, B exits.
    await asyncio.gather(
        turn("a", "token-owner-A", 0.00, 0.02),
        turn("b", "token-owner-B", 0.01, 0.03),
    )

    assert seen["a"] == ["token-owner-A", "token-owner-A"]
    assert seen["b"] == ["token-owner-B", "token-owner-B"]
    # And neither leaked into the process for the next tenant to inherit.
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "someone-elses-token"


async def test_a_worker_thread_scope_does_not_leak_to_the_event_loop(polluted_env):
    """`search.py` / `taxonomy_draft.py` enter the scope inside anyio worker threads."""
    import anyio

    def blocking_call() -> str:
        with llm_prefs.provider_env("openrouter", {"OPENROUTER_API_KEY": "or-owner-A"}):
            return claude_cli._child_env()["OPENROUTER_API_KEY"]

    assert await anyio.to_thread.run_sync(blocking_call) == "or-owner-A"
    # The event loop's own view is untouched by what that thread did.
    assert claude_cli._child_env()["OPENROUTER_API_KEY"] == "someone-elses-or-key"


async def test_build_step_env_clears_them_too(tmp_path, polluted_env):
    """The subprocess env is the one that matters — it is what `claude` reads."""
    env = runner.build_step_env(
        tmp_path, "claude_token", {"CLAUDE_CODE_OAUTH_TOKEN": "user-token-abc"}
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "user-token-abc"
    assert env["BER_LLM_PROVIDER"] == "claude_token"
    for var in HIGHER_PRECEDENCE:
        assert var not in env, f"{var} would outrank the user's token in the subprocess"


async def test_host_provider_clears_a_leftover_user_token(tmp_path, polluted_env):
    """Running on the HOST's login must not inherit the previous tenant's token.

    Without this, a job whose owner uses the shared CLI would silently run on
    whichever user's credential happened to be in the environment last.
    """
    env = runner.build_step_env(tmp_path, "claude_cli", {})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    for var in HIGHER_PRECEDENCE:
        assert var not in env


async def test_anthropic_key_provider_sets_only_its_own_var(tmp_path, polluted_env):
    env = runner.build_step_env(
        tmp_path, "anthropic_key", {"ANTHROPIC_API_KEY": "sk-ant-user"}
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-user"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


async def test_provider_env_restores_the_environment(polluted_env):
    """os.environ is shared by every request in this long-lived server, so a
    set-without-restore leaks one owner's credential into the next."""
    before = {v: os.environ.get(v) for v in llm_prefs.MANAGED_VARS}
    with llm_prefs.provider_env("claude_token", {"CLAUDE_CODE_OAUTH_TOKEN": "x"}):
        pass
    after = {v: os.environ.get(v) for v in llm_prefs.MANAGED_VARS}
    assert after == before


async def test_provider_env_restores_even_when_the_call_raises(polluted_env):
    before = {v: os.environ.get(v) for v in llm_prefs.MANAGED_VARS}
    with pytest.raises(RuntimeError):
        with llm_prefs.provider_env("claude_token", {"CLAUDE_CODE_OAUTH_TOKEN": "x"}):
            raise RuntimeError("boom")
    assert {v: os.environ.get(v) for v in llm_prefs.MANAGED_VARS} == before


# --- lease exemption -------------------------------------------------------

async def test_byo_providers_are_exempt_from_the_lease() -> None:
    """A user on their own credential never touches the host's CLI login, so
    they must never queue behind whoever holds it."""
    await init_models()
    async with AsyncSessionLocal() as db:
        for provider in ("claude_token", "anthropic_key", "openrouter"):
            assert await llm_lease.blocked_reason(db, "someone", provider) is None
            assert llm_prefs.is_byo(provider)
    assert not llm_prefs.is_byo("claude_cli")


# --- storage ---------------------------------------------------------------

async def _register(c: httpx.AsyncClient, email: str) -> dict:
    r = await c.post(
        "/auth/register", json={"email": email, "password": "hunter2pw"}
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_settings_never_return_a_stored_secret(monkeypatch) -> None:
    """A credential is write-only from the client's side."""
    await init_models()
    secret = "sk-ant-oat-verysecret-token-value"

    async def _ok(env_overrides):
        return llm_lease.CliAuth(True, True, "alice@example.com", "")

    monkeypatch.setattr(llm_lease, "verify_credential", _ok)

    async with _client() as c:
        headers = await _register(c, "creds_out@example.com")
        r = await c.patch(
            "/auth/settings", headers=headers, json={"claude_token": secret}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert secret not in r.text
        assert body["has_claude_token"] is True
        # The verified account is shown so the user can confirm WHICH of their
        # accounts will be billed.
        assert body["claude_token_account"] == "alice@example.com"
        assert body["claude_token_expires_at"]  # ~12 months out

        got = await c.get("/auth/settings", headers=headers)
        assert secret not in got.text
        assert got.json()["has_claude_token"] is True


async def test_stored_credential_is_ciphertext(monkeypatch) -> None:
    await init_models()
    secret = "sk-ant-oat-plaintext-check"

    async def _ok(env_overrides):
        return llm_lease.CliAuth(True, True, "bob@example.com", "")

    monkeypatch.setattr(llm_lease, "verify_credential", _ok)

    async with _client() as c:
        headers = await _register(c, "creds_cipher@example.com")
        await c.patch("/auth/settings", headers=headers, json={"claude_token": secret})

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        from server.app.db.models import User

        # Scope to THIS test's user: other tests in this file store their own
        # claude_token rows, so an unfiltered .first() would assert against
        # whichever happened to be written first.
        user_id = (
            await db.execute(
                select(User.id).where(User.email == "creds_cipher@example.com")
            )
        ).scalar_one()
        row = (
            await db.execute(
                select(UserCredential).where(
                    UserCredential.user_id == user_id,
                    UserCredential.kind == "claude_token",
                )
            )
        ).scalar_one()
        assert row.secret_encrypted != secret
        assert crypto.decrypt(row.secret_encrypted) == secret


async def test_a_credential_that_does_not_authenticate_is_refused(monkeypatch) -> None:
    """422 at paste time, not a failure three steps into a pipeline run."""
    await init_models()

    async def _bad(env_overrides):
        return llm_lease.CliAuth(True, False, None, "The CLI is not logged in.")

    monkeypatch.setattr(llm_lease, "verify_credential", _bad)

    async with _client() as c:
        headers = await _register(c, "creds_bad@example.com")
        r = await c.patch(
            "/auth/settings", headers=headers, json={"claude_token": "garbage"}
        )
        assert r.status_code == 422, r.text
        # And nothing was stored.
        assert (await c.get("/auth/settings", headers=headers)).json()[
            "has_claude_token"
        ] is False


async def test_verification_uses_a_real_call_not_auth_status(monkeypatch) -> None:
    """Regression: `claude auth status` cannot judge a credential.

    Given a syntactically plausible but completely invalid
    CLAUDE_CODE_OAUTH_TOKEN, `claude auth status` still answers
    ``loggedIn: true`` — it reports which credential SOURCE is configured, not
    whether that credential is accepted. Verifying with it stored whatever the
    user pasted and surfaced the problem as a failed pipeline run hours later.
    The settings endpoint must therefore go through ``verify_credential``, which
    makes a real (free-on-failure) model call.
    """
    await init_models()
    calls: list[str] = []

    async def _status_says_yes(env_overrides=None):
        calls.append("probe")
        return llm_lease.CliAuth(True, True, None, "")

    async def _real_call_says_no(env_overrides):
        calls.append("verify")
        return llm_lease.CliAuth(
            True, False, None, "Failed to authenticate. API Error: 401"
        )

    monkeypatch.setattr(llm_lease, "probe_cli_auth", _status_says_yes)
    monkeypatch.setattr(llm_lease, "verify_credential", _real_call_says_no)

    async with _client() as c:
        headers = await _register(c, "creds_realcheck@example.com")
        r = await c.patch(
            "/auth/settings", headers=headers, json={"claude_token": "plausible-junk"}
        )
    assert r.status_code == 422, r.text
    assert "401" in r.json()["detail"]
    assert "verify" in calls, "settings must verify with a real call"
    assert "probe" not in calls, "auth status must not be what decides this"


async def test_selecting_a_provider_without_its_credential_is_refused() -> None:
    await init_models()
    async with _client() as c:
        headers = await _register(c, "creds_noprov@example.com")
        r = await c.patch(
            "/auth/settings", headers=headers, json={"llm_provider": "claude_token"}
        )
        assert r.status_code == 422, r.text


async def test_clearing_the_credential_falls_back_instead_of_trapping(
    monkeypatch,
) -> None:
    """Removing a credential while selected must never 422 — that would leave the
    user stuck in a setting they cannot leave."""
    await init_models()

    async def _ok(env_overrides):
        return llm_lease.CliAuth(True, True, "carol@example.com", "")

    monkeypatch.setattr(llm_lease, "verify_credential", _ok)

    async with _client() as c:
        headers = await _register(c, "creds_clear@example.com")
        r = await c.patch(
            "/auth/settings",
            headers=headers,
            json={"llm_provider": "claude_token", "claude_token": "sk-ant-oat-x"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["llm_provider"] == "claude_token"

        r = await c.patch("/auth/settings", headers=headers, json={"claude_token": ""})
        assert r.status_code == 200, r.text
        assert r.json()["has_claude_token"] is False
        assert r.json()["llm_provider"] == "codex_cli"


async def test_unknown_provider_is_rejected() -> None:
    await init_models()
    async with _client() as c:
        headers = await _register(c, "creds_unknown@example.com")
        r = await c.patch(
            "/auth/settings", headers=headers, json={"llm_provider": "not_a_provider"}
        )
        assert r.status_code == 422


# --- credential failure is fatal, not per-item -----------------------------

async def test_auth_failure_latches_and_exits_nonzero() -> None:
    """A rejected credential must stop the run.

    Every per-paper worker catches Exception so one bad paper cannot abort a run.
    A raise would therefore be recorded as "this paper failed" for EVERY paper,
    and the run would report success having extracted nothing — after spending
    hours on someone's quota. The latch short-circuits instead, and
    raise_if_auth_failed turns that into a non-zero exit.
    """
    from ber_core import claude_cli

    claude_cli._AUTH_LATCH.clear()
    try:
        assert claude_cli.auth_failure_detail() is None
        claude_cli.raise_if_auth_failed()  # no-op when healthy

        claude_cli._latch_auth_failure("doi:10.1/x", "OAuth token has expired")
        assert claude_cli._auth_failed()
        assert "expired" in claude_cli.auth_failure_detail()
        with pytest.raises(SystemExit) as exc:
            claude_cli.raise_if_auth_failed()
        assert exc.value.code == 2
    finally:
        claude_cli._AUTH_LATCH.clear()


async def test_auth_phrases_are_checked_before_usage_limit_phrases() -> None:
    """An expired-credential message containing the word "limit" must not be
    mistaken for a quota pause — that would sleep 30 minutes and retry a
    credential that can never work again."""
    from ber_core import claude_cli

    text = "authentication_error: your plan limit could not be verified".lower()
    assert any(p in text for p in claude_cli._AUTH_FAILURE_PHRASES)
    assert any(p in text for p in ("plan limit",))  # it WOULD also match a pause
