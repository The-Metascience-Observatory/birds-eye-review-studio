"""Startup reaches every service without installing or logging into Claude."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("logged_in", [True, False])
def test_public_startup_uses_codex_without_interactive_login(tmp_path, logged_in):
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls"
    codex = bins / "codex"
    codex.write_text(
        '#!/bin/bash\n'
        '[[ "$*" == "login status" ]] || exit 99\n'
        '[ -z "${OPENAI_API_KEY:-}${CODEX_API_KEY:-}" ] || exit 98\n'
        'echo codex-status >> "$TEST_CALLS"\n'
        f'exit {0 if logged_in else 1}\n'
    )
    codex.chmod(0o755)
    claude = bins / "claude"
    claude.write_text('#!/bin/bash\necho UNEXPECTED-CLAUDE >> "$TEST_CALLS"\nexit 99\n')
    claude.chmod(0o755)
    # Source function definitions, without executing the command dispatcher.
    script = Path("dev.sh").read_text().split('# Split flags from positionals')[0]
    script += '''
PUBLIC_MODE=1
ensure_prereqs() { :; }
report_machine() { :; }
load_env_local() { :; }
start_api() { echo api-started; }
start_worker() { echo worker-started; }
start_pdfworker() { echo pdfworker-started; }
start_studio() { echo studio-started; }
cmd_up
'''
    env = dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}", TEST_CALLS=str(calls),
               OPENAI_API_KEY="must-be-cleared", CODEX_API_KEY="must-be-cleared")
    env.pop("BER_LLM_PROVIDER", None)
    result = subprocess.run(["bash", "-c", script], env=env, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["codex-status"]
    assert "codex" in result.stdout
    if not logged_in:
        assert "NOT LOGGED IN" in result.stdout
    for service in ("api", "worker", "pdfworker", "studio"):
        assert f"{service}-started" in result.stdout


def test_default_dispatch_needs_no_anthropic_key(monkeypatch):
    from ber_core import claude_cli
    from ber_core import codex_backend
    monkeypatch.delenv("BER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(codex_backend, "find_binary", lambda: "/test/codex")
    assert claude_cli.effective_provider() == "codex_cli"
