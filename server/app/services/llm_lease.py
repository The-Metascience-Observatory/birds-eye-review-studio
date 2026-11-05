"""Single-occupancy lease over the host's shared ``claude`` CLI login.

WHY THIS EXISTS
---------------
The ``claude`` CLI has no per-request credential: it reads whatever account is
logged in under the server user's ``$HOME/.claude``. ``claude_cli.py`` passes the
worker's environment straight through (minus ``CLAUDECODE``), so every LLM
subprocess on this host runs as that one account. Hosting the studio for several
people therefore means everyone silently spends ONE person's subscription.

Rather than pretend otherwise, this module makes the occupancy explicit: one
user holds the lease, runs their work under their own Claude login, and releases
it. Everyone else is refused LLM work with a message naming the holder.

Deliberately NOT covered:
* Users on the ``openrouter`` provider carry their own key (runner injects
  ``OPENROUTER_API_KEY``) and never touch the shared CLI — they are exempt.
* Non-LLM work (search, PDF fetch, export, browsing) is never gated; blocking
  the whole app whenever someone runs a screen would make it unusable.

This is a coordination lock among trusted colleagues, NOT a security boundary:
all steps run as one OS user with no setuid/container isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .._time import utcnow
from ..db.models import LlmLease, User
from ..errors import mask_email, public_text
from . import llm_prefs

# The table holds exactly one row; this is its primary key.
SINGLETON_ID = "singleton"

# A lease idle longer than this is claimable by anyone. Without it, someone who
# closes their laptop mid-session blocks the team indefinitely.
LEASE_TTL_MIN = 30

# Never let a CLI probe wedge a web request.
_AUTH_PROBE_TIMEOUT = 10
# A real (tiny) model call, so more generous than the status probe.
_VERIFY_TIMEOUT = 60

_NO_CLI_DETAIL = (
    "The claude CLI was not found on this host. On WSL it must be the "
    "LINUX build — a Claude Code installed on Windows cannot be used "
    "from WSL. Install with: curl -fsSL https://claude.ai/install.sh | bash"
)


class LeaseHeld(Exception):
    """Someone else holds a live lease; the claim did not happen."""


async def _communicate(proc, timeout: float) -> tuple[bytes, bytes]:
    """``proc.communicate()`` with a timeout that KILLS the child on expiry.

    ``asyncio.wait_for`` only stops waiting: the CLI process itself would keep
    running (and, for the verifier, keep a model call in flight) after the web
    request has already given up on it.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


@dataclass(frozen=True)
class CliAuth:
    """What the host CLI reports about itself."""

    installed: bool
    logged_in: bool
    account: str | None  # email/label, when the CLI reports one
    detail: str = ""


def _is_windows_binary(path: str) -> bool:
    """A Windows `claude.exe` seen from WSL, which cannot run from Linux.

    WSL's PATH interop exposes the Windows install under /mnt/<drive>/…. Running
    it as a bare `claude` from Linux fails, so treating it as "found" would make
    the studio report a healthy CLI and then fail at job time — the confusing
    failure `dev.sh::claude_bin_ok` already guards against.
    """
    p = path.replace("\\", "/")
    if p.lower().endswith(".exe"):
        return True
    parts = p.split("/")
    return len(parts) > 2 and parts[1] == "mnt" and len(parts[2]) == 1


def find_cli() -> str | None:
    """The host's usable Linux `claude` binary, or None.

    Mirrors ``dev.sh::claude_bin_candidates`` — PATH alone is not enough: the
    official installer drops the binary in ~/.local/bin and does NOT add it to
    PATH, and a background worker's PATH is often minimal. Windows builds seen
    through WSL interop are rejected.
    """
    candidates: list[str] = []
    on_path = shutil.which("claude")
    if on_path:
        candidates.append(on_path)

    # An explicit override wins over discovery (mirrors PIPELINE_PYTHON's role).
    override = os.environ.get("BER_CLAUDE_BIN", "").strip()
    if override:
        candidates.insert(0, override)

    home = Path.home()
    candidates += [
        str(home / ".local/bin/claude"),      # official install.sh location
        str(home / ".claude/local/claude"),
        "/usr/local/bin/claude",
        "/usr/bin/claude",
        str(home / "node_modules/.bin/claude"),
        str(home / ".volta/bin/claude"),
    ]
    nvm = home / ".nvm/versions/node"
    if nvm.is_dir():
        for d in sorted(nvm.iterdir(), reverse=True):
            candidates.append(str(d / "bin/claude"))

    for c in candidates:
        if _is_windows_binary(c):
            continue
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _sandbox_cwd() -> str:
    """The private, empty directory every `claude` child runs in (claude_cli)."""
    from ._pipeline_path import ensure_pipeline_on_path
    ensure_pipeline_on_path()
    from ber_core.claude_cli import sandbox_dir
    return str(sandbox_dir())


def _sandboxed_cli(model: str, prompt_arg: str) -> tuple[list[str], str]:
    """``claude_cli.cli_command`` argv (argv[0] is the bare name) plus its cwd."""
    from ._pipeline_path import ensure_pipeline_on_path
    ensure_pipeline_on_path()
    from ber_core.claude_cli import cli_command, sandbox_dir
    cmd = cli_command(model, prompt_arg=prompt_arg) or []
    return cmd + ["--no-session-persistence"], str(sandbox_dir())


async def probe_codex_auth() -> CliAuth:
    """Read Codex login status without a model call or exposing credential details."""
    from ._pipeline_path import ensure_pipeline_on_path
    ensure_pipeline_on_path()
    from ber_core.codex_backend import find_binary
    binary = find_binary()
    if not binary:
        return CliAuth(False, False, None, "Install Codex CLI and run `codex login` on the server.")
    env = dict(os.environ)
    for key in llm_prefs.MANAGED_VARS:
        env.pop(key, None)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "login", "status", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), _AUTH_PROBE_TIMEOUT)
        logged_in = proc.returncode == 0 and b"logged in" in (stdout + stderr).lower()
        return CliAuth(True, logged_in, None,
                       "Host Codex login configured; model access is checked at run time."
                       if logged_in else "Run `codex login` on the server.")
    except (asyncio.TimeoutError, OSError):
        return CliAuth(True, False, None, "Could not query Codex login status.")
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()


async def probe_cli_auth(env_overrides: dict[str, str] | None = None) -> CliAuth:
    """Ask the ``claude`` CLI who it is logged in as.

    ``claude auth status`` prints JSON containing ``"loggedIn": true|false`` and
    usually an account identifier. This mirrors what ``dev.sh::claude_logged_in``
    already relies on — parse the CLI's own answer rather than sniffing files,
    with the on-disk credential markers only as a fallback.

    *env_overrides* applies a specific credential to a scratch environment (via
    ``llm_prefs.credential_env``, so higher-precedence variables are cleared and
    the host's own login cannot answer in its place).

    NOTE — this reports what the CLI has CONFIGURED, not what actually works.
    ``claude auth status`` answers ``loggedIn: true`` for a syntactically
    plausible but completely invalid token, so it cannot decide whether a pasted
    credential is good. Use :func:`verify_credential` for that; this one is for
    displaying the host's login state, and for reading back an account name.
    """
    binary = find_cli()
    if not binary:
        return CliAuth(False, False, None, _NO_CLI_DETAIL)

    env = None
    if env_overrides is not None:
        env = dict(os.environ)
        for var, value in llm_prefs.credential_env(env_overrides).items():
            if value is None:
                env.pop(var, None)
            else:
                env[var] = value

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "auth", "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=await asyncio.to_thread(_sandbox_cwd),
        )
        raw, _ = await _communicate(proc, _AUTH_PROBE_TIMEOUT)
    except (asyncio.TimeoutError, OSError) as exc:
        return CliAuth(True, False, None,
                       public_text(f"{type(exc).__name__}: {exc}", "Could not query the CLI"))

    text = (raw or b"").decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Older CLIs print prose. Fall back to a substring read; be conservative
        # and report "not logged in" rather than guessing an account.
        logged_in = '"loggedIn": true' in text or "Logged in" in text
        return CliAuth(True, logged_in, None, "" if logged_in else "The CLI is not logged in.")

    if not isinstance(data, dict):
        return CliAuth(True, False, None, "Unexpected output from `claude auth status`.")

    logged_in = bool(data.get("loggedIn"))
    # The account field's shape varies across CLI versions; try the known spots
    # and accept only a string.
    account = data.get("account") or data.get("email")
    if isinstance(account, dict):
        account = account.get("emailAddress") or account.get("email")
    if not isinstance(account, str):
        oauth = data.get("oauthAccount")
        account = oauth.get("emailAddress") if isinstance(oauth, dict) else None
    return CliAuth(
        installed=True,
        logged_in=logged_in,
        account=account if isinstance(account, str) else None,
        detail="" if logged_in else "The CLI is not logged in.",
    )


async def verify_credential(env_overrides: dict[str, str]) -> CliAuth:
    """Prove a user-supplied credential actually works, by USING it.

    ``claude auth status`` is not sufficient: given a made-up
    ``CLAUDE_CODE_OAUTH_TOKEN`` it still answers ``loggedIn: true``, because it
    reports which credential source is configured rather than whether that
    credential is accepted. Verifying with it would store any string the user
    pasted and surface the problem as a failed pipeline run hours later — the
    exact failure this check exists to prevent.

    So make the smallest real model call instead: one haiku token. A rejected
    credential comes back ``is_error`` with ``terminal_reason: "api_error"`` and
    ``total_cost_usd: 0`` — it costs the user nothing to find out — while a good
    one costs a fraction of a cent and proves the whole path end to end.

    The account name is then read back from ``claude auth status`` under the same
    credential; it is best-effort (older CLIs omit it) and only used for display.
    """
    binary = find_cli()
    if not binary:
        return CliAuth(False, False, None, _NO_CLI_DETAIL)

    env = dict(os.environ)
    for var, value in llm_prefs.credential_env(env_overrides).items():
        if value is None:
            env.pop(var, None)
        else:
            env[var] = value

    # The same locked-down invocation every pipeline model call uses: private
    # empty cwd, no tools, no MCP, no settings. A credential check must not be
    # the one `claude` child that runs with tools enabled in the repo root.
    # `cli_command` may probe `claude --help` once (cached), so keep it off the
    # event loop. The prompt is a fixed literal, so it stays on argv.
    cmd, cwd = await asyncio.to_thread(_sandboxed_cli, "haiku", "ok")
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *cmd[1:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        raw, err = await _communicate(proc, _VERIFY_TIMEOUT)
    except (asyncio.TimeoutError, OSError) as exc:
        return CliAuth(True, False, None, public_text(
            f"{type(exc).__name__}: {exc}", "Could not reach Claude to check it"))

    text = (raw or b"").decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Raw CLI stderr: may carry host paths and environment details.
        detail = (err or b"").decode("utf-8", "replace").strip() or text
        if not detail:
            return CliAuth(True, False, None, "The CLI returned nothing.")
        return CliAuth(True, False, None,
                       public_text(detail[:300], "The CLI could not check that credential"))

    if not isinstance(data, dict) or data.get("is_error"):
        # `result` carries the provider's own explanation when there is one.
        detail = str(
            (isinstance(data, dict) and (data.get("result") or data.get("terminal_reason")))
            or "That credential was rejected."
        )
        return CliAuth(True, False, None, detail[:300])

    account = None
    try:
        status = await probe_cli_auth(env_overrides)
        account = status.account
    except Exception:  # pragma: no cover - display nicety, never fatal
        account = None
    return CliAuth(True, True, account, "")


async def _row(session: AsyncSession) -> LlmLease:
    """The singleton row, created on first use."""
    lease = await session.get(LlmLease, SINGLETON_ID)
    if lease is None:
        lease = LlmLease(id=SINGLETON_ID)
        session.add(lease)
        await session.flush()
    return lease


def is_expired(lease: LlmLease) -> bool:
    if lease.holder_user_id is None:
        return True
    if lease.last_seen_at is None:
        return True
    last = lease.last_seen_at
    if last.tzinfo is None:  # sqlite round-trips naive datetimes
        from datetime import timezone

        last = last.replace(tzinfo=timezone.utc)
    return (utcnow() - last) > timedelta(minutes=LEASE_TTL_MIN)


def held_by_other(lease: LlmLease, user_id: str) -> bool:
    """True when someone ELSE holds a live lease."""
    if lease.holder_user_id is None or is_expired(lease):
        return False
    return lease.holder_user_id != user_id


async def current(session: AsyncSession) -> LlmLease:
    """The lease row (created if absent). Does not mutate ownership."""
    return await _row(session)


async def holder_email(session: AsyncSession, lease: LlmLease) -> str | None:
    if not lease.holder_user_id:
        return None
    user = await session.get(User, lease.holder_user_id)
    return user.email if user else None


async def acquire(session: AsyncSession, user_id: str, account: str | None) -> LlmLease:
    """Claim the CLI for *user_id*, or raise :class:`LeaseHeld`.

    ONE conditional UPDATE, not read-check-write: two users clicking "claim" at
    the same moment both passed a Python-side ``held_by_other`` check and the
    second write silently stole the first's lease. The WHERE clause makes the
    database decide — the row changes only if it is free, already ours, or idle
    past the TTL — so exactly one concurrent claimant wins. Re-claiming one's
    own lease just refreshes it, so the claim button stays idempotent.
    """
    try:
        await _row(session)
        await session.commit()
    except IntegrityError:  # a concurrent first-ever claim created the row
        await session.rollback()
    now = utcnow()
    cutoff = now - timedelta(minutes=LEASE_TTL_MIN)
    result = await session.execute(
        update(LlmLease)
        .where(
            LlmLease.id == SINGLETON_ID,
            or_(
                LlmLease.holder_user_id.is_(None),
                LlmLease.holder_user_id == user_id,
                LlmLease.last_seen_at.is_(None),
                LlmLease.last_seen_at < cutoff,
            ),
        )
        .values(
            acquired_at=case(
                (LlmLease.holder_user_id == user_id, LlmLease.acquired_at),
                else_=now,
            ),
            holder_user_id=user_id,
            claude_account=account,
            last_seen_at=now,
            released_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    lease = await session.get(LlmLease, SINGLETON_ID, populate_existing=True)
    if not result.rowcount:
        raise LeaseHeld("The shared CLI is held by another user.")
    return lease


async def release(session: AsyncSession, user_id: str, force: bool = False) -> LlmLease:
    """Release the lease. A no-op when *user_id* isn't the holder (idempotent).

    ``force`` lets an expired lease be cleared by anyone — used by the UI's
    "take over" path so a stale lease is never a permanent block.
    """
    lease = await _row(session)
    if lease.holder_user_id and (force or lease.holder_user_id == user_id):
        lease.holder_user_id = None
        lease.claude_account = None
        lease.acquired_at = None
        lease.last_seen_at = None
        lease.released_at = utcnow()
        await session.commit()
    return lease


async def touch(session: AsyncSession, user_id: str) -> None:
    """Refresh the holder's idle timer. Cheap; call when they enqueue work."""
    lease = await _row(session)
    if lease.holder_user_id == user_id:
        lease.last_seen_at = utcnow()
        await session.commit()


async def blocked_reason(
    session: AsyncSession, user_id: str, provider: str, *, reveal: bool = False
) -> str | None:
    """Why *user_id* may not run CLI-backed LLM work right now, or None.

    Returns a message intended for a 409 body. It names the holder, because
    "someone else is using it" is not actionable — but MASKED (``a***@e***.com``)
    unless *reveal* (an admin asking), so the endpoint is not a way for any
    signed-in user to read another user's email address.
    """
    # OpenRouter users bring their own key and never touch the shared CLI.
    # Any provider where the USER supplies the credential never touches the
    # host's shared CLI login, so it must never queue behind whoever holds it.
    # Was hardcoded to "openrouter"; now covers the per-user Claude credentials
    # too (llm_prefs.BYO_PROVIDERS).
    if llm_prefs.is_byo(provider):
        return None

    lease = await _row(session)
    if not held_by_other(lease, user_id):
        return None

    email = await holder_email(session, lease)
    claude_account = lease.claude_account
    if not reveal:
        email = mask_email(email)
        claude_account = mask_email(claude_account)
    email = email or "another user"
    account = f" (account: {claude_account})" if claude_account else ""
    return (
        f"{email} currently holds the shared host CLI reservation{account}. "
        f"LLM work runs one person at a time so nobody spends someone else's "
        f"subscription. Ask them to release it in Settings, or wait — an idle "
        f"lease frees up after {LEASE_TTL_MIN} minutes."
    )
