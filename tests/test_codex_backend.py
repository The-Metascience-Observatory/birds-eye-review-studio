"""Codex transport and real screening/extraction parsers, without inference."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from ber_core import claude_cli
from ber_core import codex_backend


def load_script(filename):
    spec = importlib.util.spec_from_file_location("codex_test_" + Path(filename).stem, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("BER_LLM_PROVIDER", "codex_cli")
    monkeypatch.delenv("BER_CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(codex_backend, "find_binary", lambda: "/mock/codex")
    calls = []

    def install(answer='{"eligible":true}', events=None, code=0, stderr=""):
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(answer)
            return SimpleNamespace(returncode=code, stderr=stderr, stdout="\n".join(
                json.dumps(e) for e in (events if events is not None else [
                    {"type": "thread.started", "thread_id": "do-not-resume"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "commentary"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 12}},
                ])))
        monkeypatch.setattr(codex_backend.subprocess, "run", run)
    install()
    return calls, install


def test_luna_transport_isolated_and_stdin(transport, monkeypatch):
    calls, _ = transport
    for key in ["CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]:
        monkeypatch.setenv(key, "must-not-leak")
    prompt = "paper text " * 20000
    result = claude_cli.call_claude(prompt, "sonnet", "test")
    cmd, opts = calls[0]
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
    assert prompt not in cmd and opts["input"] == prompt
    assert "--ignore-user-config" in cmd and "--ephemeral" in cmd
    assert "read-only" in cmd and "features.shell_tool=false" in cmd
    assert "must-not-leak" not in opts["env"].values()
    assert not Path(opts["cwd"]).exists()  # temporary work + answer cleaned up
    assert json.loads(result.text) == {"eligible": True}
    assert result.input_tokens == 100 and result.output_tokens == 12
    assert result.session_id is None


@pytest.mark.parametrize("events,code", [([], 0), ([{"type": "turn.failed"}], 0),
                                        ([{"type": "turn.completed"}], 1)])
def test_partial_or_failed_answer_never_accepted(transport, events, code):
    _, install = transport
    install(events=events, code=code)
    assert not claude_cli.call_claude("prompt", "luna", "test").text


def test_effort_is_per_process_and_model_escalation_explicit(transport, monkeypatch):
    calls, _ = transport
    claude_cli.call_claude("prompt", "luna", "default")
    assert 'model_reasoning_effort="medium"' in calls[-1][0]
    monkeypatch.setenv("BER_CODEX_REASONING_EFFORT", "low")
    claude_cli.call_claude("prompt", "luna", "screen")
    assert 'model_reasoning_effort="low"' in calls[-1][0]
    assert calls[-1][0][3] == codex_backend.LUNA_MODEL
    monkeypatch.setenv("BER_CODEX_REASONING_EFFORT", "medium")
    claude_cli.call_claude("prompt", "terra", "difficult-extraction")
    assert calls[-1][0][3] == codex_backend.TERRA_MODEL
    assert 'model_reasoning_effort="medium"' in calls[-1][0]


def test_invalid_effort_fails_before_inference(transport, monkeypatch):
    calls, _ = transport
    monkeypatch.setenv("BER_CODEX_REASONING_EFFORT", 'low" unexpected=true')
    with pytest.raises(ValueError, match="BER_CODEX_REASONING_EFFORT"):
        codex_backend.call_codex("prompt", "luna", "invalid")
    assert not calls


def test_timeout_and_missing_binary(transport, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 1)
    monkeypatch.setattr(codex_backend.subprocess, "run", timeout)
    assert not claude_cli.call_claude("prompt", "luna", "test", timeout=1).text
    monkeypatch.setattr(codex_backend, "find_binary", lambda: None)
    with pytest.raises(RuntimeError, match="Codex CLI not found"):
        claude_cli.call_claude("prompt", "luna", "test")


def test_pdf_fallback_does_not_invent_file_access(transport):
    calls, _ = transport
    assert not claude_cli.call_claude("read PDF", "luna", "test", read_files=[Path("paper.pdf")]).text
    assert not calls


@pytest.mark.parametrize("source", ["event", "stderr"])
def test_rejected_codex_login_stops_run_without_leaking_diagnostics(transport, capsys, source):
    calls, install = transport
    diagnostic = "401 Unauthorized: token_expired secret-placeholder"
    install(code=1, events=[{"type": "turn.failed", "error": {"message": diagnostic}}]
            if source == "event" else [], stderr=diagnostic if source == "stderr" else "")
    with claude_cli.llm_context("codex_cli"):
        assert not claude_cli.call_claude("prompt", "luna", "first").text
        assert not claude_cli.call_claude("prompt", "luna", "second").text
        assert len(calls) == 1
        with pytest.raises(SystemExit) as error:
            claude_cli.raise_if_auth_failed()
        assert error.value.code == 2
        assert "secret-placeholder" not in claude_cli.auth_failure_detail()
    assert "secret-placeholder" not in capsys.readouterr().err
    with claude_cli.llm_context("codex_cli"):
        assert claude_cli.auth_failure_detail() is None


def test_transient_codex_error_does_not_latch_login_failure(transport):
    calls, install = transport
    install(code=1, events=[{"type": "turn.failed", "error": {"message": "503 unavailable"}}])
    with claude_cli.llm_context("codex_cli"):
        assert not claude_cli.call_claude("prompt", "luna", "first").text
        install()
        assert claude_cli.call_claude("prompt", "luna", "retry").text
        assert claude_cli.auth_failure_detail() is None
    assert len(calls) == 2


def test_inspect_batch_stops_on_usage_exhaustion_without_exposing_diagnostics(transport):
    _, install = transport
    install(code=1, events=[{"type": "turn.failed", "error": {"message": "usage_limit_reached secret-placeholder"}}])
    with pytest.raises(codex_backend.CodexUsageLimit) as error:
        codex_backend.call_codex("prompt", "gpt-5.6-terra", "review", stop_on_usage_limit=True)
    assert "secret-placeholder" not in str(error.value)
    assert claude_cli.auth_failure_detail() is None


def test_pipeline_usage_limit_stops_remaining_papers(transport, capsys):
    from ber_core.usage_guard import reset_stop
    calls, install = transport
    reset_stop()
    try:
        install(code=1, events=[{"type": "turn.failed", "error": {
            "message": "usage_limit_reached secret-placeholder"}}])
        assert not claude_cli.call_claude("paper", "luna", "first").text
        assert claude_cli.run_halted()
        assert not claude_cli.call_claude("paper", "luna", "second").text
        assert len(calls) == 1
        with pytest.raises(SystemExit) as error:
            claude_cli.raise_if_auth_failed()
        assert error.value.code == 3
        assert "secret-placeholder" not in capsys.readouterr().err
    finally:
        reset_stop()


def test_async_dispatch_and_provider_scope(transport):
    assert asyncio.run(claude_cli.async_call_claude("prompt", "haiku", "test")).text
    assert claude_cli.resolve_model("haiku") == codex_backend.LUNA_MODEL
    assert claude_cli.resolve_model("gpt-5.6-luna") == codex_backend.LUNA_MODEL
    with claude_cli.llm_context("claude_cli"):
        assert claude_cli.resolve_model("haiku") == "haiku"


def test_screening_model_and_parser_use_codex(transport):
    _, install = transport
    screen = load_script("steps/classify.py")
    install('{"article_type":"Clinical Trial","confidence":"high"}')
    model = screen._classify_model({"models": {"classification": "haiku"}})
    assert model == codex_backend.LUNA_MODEL
    response = screen.call_claude("synthetic abstract", model, "10.test/paper")
    parsed = screen._parse_type(response.text, "10.test/paper", valid_types={"Clinical Trial"})
    assert parsed["article_type"] == "Clinical Trial"


def test_extraction_retries_full_prompt_without_session(transport, monkeypatch):
    calls, install = transport
    extraction = load_script("steps/extract_pass2.py")
    monkeypatch.setattr(extraction, "_MODEL", codex_backend.LUNA_MODEL)
    monkeypatch.setattr(extraction, "MAX_RETRIES", 2)
    monkeypatch.setattr(extraction.time, "sleep", lambda _: None)
    install('not JSON')
    outcome = extraction._run_extraction("synthetic full paper", "10.test/paper", 1, 1)
    assert outcome.result is None and len(calls) == 2
    assert all("synthetic full paper" in kwargs["input"] for _, kwargs in calls)
    install('{"study_design":{"design_type":"RCT"},"outcomes":[]}')
    outcome = extraction._run_extraction("synthetic full paper", "10.test/paper", 1, 1)
    assert outcome.result["study_design"]["design_type"] == "RCT"


def test_eligibility_extraction_uses_codex(transport, monkeypatch):
    _, install = transport
    extraction = load_script("steps/extract_pass1.py")
    monkeypatch.setattr(extraction, "read_best_text", lambda *a, **k: ("synthetic paper", "md"))
    install('{"screening":{"eligible_for_pass2":true},"metadata":{"title":"Synthetic"}}')
    ref = SimpleNamespace(paper_id="10.test/paper", md_path=Path("paper.md"),
                          xml_path=None, pdf_path=None)
    result = extraction.extract_one(ref, 1, 1, "Extract {full_text}", "luna")
    assert result["_status"] == "ok" and result["screening"]["eligible_for_pass2"]
