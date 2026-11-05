"""Every `claude` child the server spawns runs in claude_cli's sandbox.

`claude_cli.cli_command` + `sandbox_dir` are the one locked-down invocation:
private empty cwd, no tools, no MCP, no settings. The credential verifier used
to hand-roll `claude -p ok --model haiku` in the server's own cwd (the repo
root, holding server/.env and dev.db) with every tool enabled.
"""
from __future__ import annotations

from pathlib import Path

from app.services import llm_lease


class _Proc:
    returncode = 0

    def __init__(self, out: bytes):
        self._out = out

    async def communicate(self):
        return self._out, b""

    def kill(self):  # pragma: no cover - never reached on success
        pass

    async def wait(self):  # pragma: no cover
        return 0


async def test_verify_credential_uses_the_sandboxed_invocation(monkeypatch):
    calls: list[tuple[tuple, dict]] = []

    async def spawn(*argv, **kw):
        calls.append((argv, kw))
        if "auth" in argv:
            return _Proc(b'{"loggedIn": true}')
        return _Proc(b'{"is_error": false, "result": "ok"}')

    monkeypatch.setattr(llm_lease, "find_cli", lambda: "/opt/claude")
    monkeypatch.setattr(llm_lease.asyncio, "create_subprocess_exec", spawn)
    res = await llm_lease.verify_credential({"ANTHROPIC_API_KEY": "x"})
    assert res.logged_in

    from ber_core.claude_cli import sandbox_dir
    sandbox = str(sandbox_dir())
    repo_root = str(Path(__file__).resolve().parents[2])
    for argv, kw in calls:
        assert argv[0] == "/opt/claude"
        assert kw.get("cwd") == sandbox, argv
        assert not str(kw["cwd"]).startswith(repo_root)

    model_call = next(argv for argv, _ in calls if "-p" in argv)
    assert "--strict-mcp-config" in model_call
    i = model_call.index("--setting-sources")
    assert model_call[i + 1] == ""
    # Tools off: either `--tools ""` or the explicit deny-list fallback.
    if "--tools" in model_call:
        assert model_call[model_call.index("--tools") + 1] == ""
    else:
        assert "--disallowedTools" in model_call
