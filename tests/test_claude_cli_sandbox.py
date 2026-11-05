"""The Claude CLI transport: sandboxing, error detection, envelope parsing.

Every case drives the real ``_cli_call_claude`` / ``_async_cli_call_claude``
with a fake subprocess, so the argv, cwd and stdin the CLI would receive are
asserted directly.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ber_core import claude_cli
from ber_core import usage_guard


def _envelope(result="ok", *, is_error=False, cost=0.01, usage=None, **extra):
    data = {"type": "result", "is_error": is_error, "result": result,
            "session_id": "sess-1", "total_cost_usd": cost,
            "usage": usage if usage is not None else {
                "input_tokens": 10, "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 5, "output_tokens": 7}}
    data.update(extra)
    return json.dumps(data)


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    """Install a fake subprocess.run; returns (calls, queue-of-outputs)."""
    calls: list[dict] = []
    outputs: list[tuple[int, str, str]] = []

    def run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        code, out, err = outputs.pop(0) if outputs else (0, _envelope(), "")
        return subprocess.CompletedProcess(cmd, code, out, err)

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    monkeypatch.setattr(claude_cli, "_cli_help_text", lambda: "")
    monkeypatch.setattr(claude_cli, "_usage_guard", lambda model=None: None)
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: calls.append({"slept": s}))
    monkeypatch.setattr(claude_cli, "_log_dest_dir", None)
    monkeypatch.setenv("BER_LLM_PROVIDER", "claude_cli")
    claude_cli._AUTH_LATCH.clear()
    usage_guard.reset_stop()
    yield calls, outputs
    claude_cli._AUTH_LATCH.clear()
    usage_guard.reset_stop()


# ── B1: never scan the model's answer for CLI error phrases ────────────────

def test_answer_mentioning_rate_limit_is_not_a_session_limit(fake_run):
    calls, outputs = fake_run
    answer = '{"outcome": "heart rate limit exceeded in 3 patients"}'
    outputs.append((0, _envelope(answer), ""))
    resp = claude_cli.call_claude("prompt", "sonnet", "doi")
    assert resp.text == answer
    assert not any("slept" in c for c in calls)
    assert len(calls) == 1


def test_answer_quoting_authentication_failed_does_not_latch(fake_run):
    _, outputs = fake_run
    answer = '{"notes": "the device reported authentication failed / invalid api key"}'
    outputs.append((0, _envelope(answer), ""))
    assert claude_cli.call_claude("prompt", "sonnet", "doi").text == answer
    assert not claude_cli._auth_failed()


def test_error_envelope_usage_limit_pauses_then_retries(fake_run):
    calls, outputs = fake_run
    outputs.append((1, _envelope("Claude AI usage limit reached|1700000000", is_error=True), ""))
    outputs.append((0, _envelope('{"a": 1}'), ""))
    resp = claude_cli.call_claude("prompt", "sonnet", "doi")
    assert resp.text == '{"a": 1}'
    assert any(c.get("slept") == claude_cli.SESSION_LIMIT_WAIT for c in calls)


def test_error_envelope_auth_failure_latches(fake_run):
    _, outputs = fake_run
    outputs.append((1, _envelope("Invalid API key · Please run /login", is_error=True), ""))
    resp = claude_cli.call_claude("prompt", "sonnet", "doi")
    assert resp.text == ""
    assert claude_cli._auth_failed()
    with pytest.raises(SystemExit) as exc:
        claude_cli.raise_if_auth_failed()
    assert exc.value.code == 2


def test_stderr_auth_failure_latches(fake_run):
    _, outputs = fake_run
    outputs.append((1, "", "OAuth token has expired. Please run /login"))
    claude_cli.call_claude("prompt", "sonnet", "doi")
    assert claude_cli._auth_failed()


def test_error_envelope_text_is_not_returned_as_an_answer(fake_run):
    _, outputs = fake_run
    outputs.append((1, _envelope('{"looks": "like json"}', is_error=True), ""))
    assert claude_cli.call_claude("prompt", "sonnet", "doi").text == ""


# ── S3 / B28: sandbox, no tools, prompt on stdin ────────────────────────────

def test_sync_call_runs_sandboxed_with_prompt_on_stdin(fake_run):
    calls, _ = fake_run
    prompt = "Ж" * 100_000  # > 128 KiB in UTF-8: would be E2BIG on argv
    claude_cli.call_claude(prompt, "sonnet", "doi")
    call = calls[0]
    cmd = call["cmd"]
    assert prompt not in cmd
    assert call["input"] == prompt
    assert cmd[:3] == ["claude", "-p", "-"]
    assert "--strict-mcp-config" in cmd
    i = cmd.index("--setting-sources")
    assert cmd[i + 1] == ""
    i = cmd.index("--tools")
    assert cmd[i + 1] == ""
    cwd = Path(call["cwd"])
    repo_root = Path(claude_cli.__file__).resolve().parents[1]
    assert cwd != repo_root and repo_root not in cwd.parents
    assert cwd == claude_cli.sandbox_dir()
    assert not any(cwd.iterdir())


def test_use_stdin_false_still_sends_prompt_on_stdin(fake_run):
    calls, _ = fake_run
    claude_cli.call_claude("short prompt", "haiku", "doi", use_stdin=False)
    assert "short prompt" not in calls[0]["cmd"]
    assert calls[0]["input"] == "short prompt"


def test_read_files_grants_only_confined_read(fake_run, tmp_path):
    calls, _ = fake_run
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    claude_cli.call_claude("prompt", "sonnet", "doi", read_files=[pdf])
    cmd = calls[0]["cmd"]
    assert "--restricted" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path.resolve())


def test_read_files_fails_closed_without_restricted_support(fake_run, monkeypatch, tmp_path):
    calls, _ = fake_run
    monkeypatch.setattr(claude_cli, "_cli_help_text", lambda: "--tools --strict-mcp-config")
    resp = claude_cli.call_claude("prompt", "sonnet", "doi", read_files=[tmp_path / "x.pdf"])
    assert resp.text == "" and not calls


def test_old_cli_without_tools_flag_disallows_every_tool(fake_run, monkeypatch):
    monkeypatch.setattr(claude_cli, "_cli_help_text", lambda: "--strict-mcp-config")
    cmd = claude_cli.cli_command("haiku")
    assert "--tools" not in cmd
    denied = cmd[cmd.index("--disallowedTools") + 1:]
    for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "Glob", "Grep"):
        assert tool in denied


def test_sandbox_rejects_a_non_empty_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli, "_sandbox_path", None)
    monkeypatch.setattr(claude_cli.tempfile, "gettempdir", lambda: str(tmp_path))
    first = claude_cli.sandbox_dir()
    assert first.parent == tmp_path.resolve()
    (first / "planted.txt").write_text("x")
    monkeypatch.setattr(claude_cli, "_sandbox_path", None)
    second = claude_cli.sandbox_dir()
    assert second != first and not any(second.iterdir())


def test_async_call_is_sandboxed_with_prompt_on_stdin(monkeypatch):
    seen = {}

    class Child:
        returncode = 0
        pid = 0

        async def communicate(self, input=None):
            seen["input"] = input
            return _envelope('{"x": 1}').encode(), b""

    async def spawn(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen.update(kwargs)
        return Child()

    monkeypatch.setattr(claude_cli, "_cli_help_text", lambda: "")
    monkeypatch.setattr(claude_cli, "_log_dest_dir", None)
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn)
    prompt = "a very long prompt " * 10
    resp = asyncio.run(claude_cli._async_cli_call_claude(prompt, "sonnet", "id"))
    assert resp.text == '{"x": 1}'
    assert seen["input"] == prompt.encode()
    assert prompt not in seen["cmd"]
    assert seen["cmd"][:3] == ["claude", "-p", "-"]
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == ""
    assert seen["cwd"] == str(claude_cli.sandbox_dir())


# ── B27: envelope parsing ───────────────────────────────────────────────────

def test_tokens_read_from_usage_block_and_null_cost_tolerated(fake_run):
    _, outputs = fake_run
    outputs.append((0, _envelope('{"a": 1}', cost=None), ""))
    resp = claude_cli.call_claude("prompt", "sonnet", "doi")
    assert resp.cost_usd == 0.0
    assert resp.input_tokens == 115
    assert resp.output_tokens == 7
    assert resp.session_id == "sess-1"


def test_null_result_yields_empty_text(fake_run):
    _, outputs = fake_run
    outputs.append((0, _envelope(None), ""))
    assert claude_cli.call_claude("prompt", "sonnet", "doi").text == ""


# ── usage guard: latch, never raise from a worker ──────────────────────────

def test_weekly_ceiling_latches_instead_of_raising(monkeypatch, fake_run):
    calls, _ = fake_run
    monkeypatch.setattr(usage_guard, "probe_usage", lambda timeout=60: (10, 99))
    monkeypatch.setattr(usage_guard, "_last_probe", 0.0)
    monkeypatch.setenv("BER_USAGE_GUARD", "1")
    usage_guard.guard("sonnet")  # must not raise
    assert usage_guard.stop_reason()
    assert claude_cli.run_halted()
    assert claude_cli.call_claude("prompt", "sonnet", "doi").text == ""
    assert not calls
    with pytest.raises(SystemExit) as exc:
        claude_cli.raise_if_auth_failed()
    assert exc.value.code == 3


def test_usage_probe_runs_in_sandbox(monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            {"result": "Current session: 12% used\nCurrent week (all models): 40% used"}), "")

    monkeypatch.setattr(claude_cli, "_cli_help_text", lambda: "")
    monkeypatch.setattr(usage_guard.subprocess, "run", run)
    assert usage_guard.probe_usage() == (12, 40)
    assert seen["cwd"] == str(claude_cli.sandbox_dir())
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == ""


# ── is_truncated ignores braces inside strings ─────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ('{"note": "values in [brackets"}', False),
    ('{"note": "a { brace"}', False),
    ('{"a": [1, 2', True),
    ('{"note": "cut off mid string', True),
    ('{"q": "escaped \\" quote {"}', False),
    ("", True),
])
def test_is_truncated_counts_structure_only(text, expected):
    assert claude_cli.is_truncated(text) is expected


def test_openrouter_referer_has_no_hardcoded_default(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    def post(url, headers=None, json=None, timeout=None):
        seen["headers"] = headers
        return R()

    import requests
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("BER_OPENROUTER_REFERER", raising=False)
    claude_cli._openrouter_call_claude("p", "sonnet", "id")
    assert "HTTP-Referer" not in seen["headers"]
    monkeypatch.setenv("BER_OPENROUTER_REFERER", "https://example.org")
    claude_cli._openrouter_call_claude("p", "sonnet", "id")
    assert seen["headers"]["HTTP-Referer"] == "https://example.org"
