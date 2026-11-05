"""Pass 1 / pass 2: halting without losing work, inputs, no_text retries."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from ber_core import claude_cli
from ber_core import usage_guard
from ber_core.utils import load_done_ids_jsonl, requeue_no_text

ROOT = Path(__file__).resolve().parents[1]


def _load(script, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_latches():
    claude_cli._AUTH_LATCH.clear()
    usage_guard.reset_stop()
    yield
    claude_cli._AUTH_LATCH.clear()
    usage_guard.reset_stop()


def _resp(text, cost=0.1):
    return types.SimpleNamespace(text=text, session_id=None, elapsed=0.1, cost_usd=cost,
                                 input_tokens=1, output_tokens=1, is_policy_error=False)


def _project(tmp_path, n=6, *, eligible_trials=None):
    projects = tmp_path / "projects"
    store = projects / "demo" / "papers" / "Clinical_Trial"
    for i in range(n):
        d = store / f"10.1--p{i}"
        d.mkdir(parents=True)
        (d / "body.md").write_text("Trial text. " * 100)
    cfg = {"topic": {"name": "demo"}, "models": {"extraction_pass1": "sonnet"}}
    if eligible_trials:
        cfg["dirs"] = {"eligible_trials": str(eligible_trials)}
    cfg_path = tmp_path / "cfg.yaml"
    import yaml
    cfg_path.write_text(yaml.safe_dump(cfg))
    return projects, cfg_path


def _run_p1(p1, monkeypatch, projects, cfg_path, extra=()):
    monkeypatch.setattr(p1, "project_paths",
                        lambda cfg: __import__("ber_core.utils", fromlist=["project_paths"]).project_paths(cfg, projects))
    monkeypatch.setattr(p1, "emit_run_summary", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["extract_pass1.py", "--config", str(cfg_path),
                                      "--workers", "1", *extra])
    p1.main()
    return projects / "demo" / "data" / "trial_extractions.jsonl"


def test_pass1_halt_keeps_finished_verdicts_and_cancels_the_queue(tmp_path, monkeypatch):
    p1 = _load("steps/extract_pass1.py", "_p1_halt")
    projects, cfg_path = _project(tmp_path, n=8)
    calls = []
    lock = threading.Lock()

    def fake_call(prompt, model, ident, **kw):
        with lock:
            calls.append(ident)
            if len(calls) == 3:
                usage_guard._STOP["reason"] = "usage_guard: weekly ceiling"
                return _resp("")
        return _resp(json.dumps({"screening": {"eligible_for_pass2": True},
                                 "metadata": {"title": "t"}}))

    monkeypatch.setattr(p1, "call_claude", fake_call)
    out = _run_p1(p1, monkeypatch, projects, cfg_path)
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(records) == 2                      # the two that finished
    assert all(r["_status"] == "ok" for r in records)
    assert len(calls) == 3                        # the rest never started
    with pytest.raises(SystemExit) as exc:
        claude_cli.raise_if_auth_failed()
    assert exc.value.code == 3


def test_pass1_ignores_eligible_trials_as_input(tmp_path, monkeypatch):
    p1 = _load("steps/extract_pass1.py", "_p1_input")
    other = tmp_path / "someone_elses_bucket"
    (other / "10.9--x").mkdir(parents=True)
    (other / "10.9--x" / "body.md").write_text("x " * 200)
    projects, cfg_path = _project(tmp_path, n=1, eligible_trials=other)
    seen = []
    monkeypatch.setattr(p1, "call_claude", lambda prompt, model, ident, **kw:
                        seen.append(ident) or _resp(json.dumps({"screening": {}})))
    _run_p1(p1, monkeypatch, projects, cfg_path)
    assert seen == ["10.1/p0"]


def test_pass1_uses_per_project_prompt_override(tmp_path, monkeypatch):
    p1 = _load("steps/extract_pass1.py", "_p1_prompt")
    projects, cfg_path = _project(tmp_path, n=0)
    seen = {}

    def fake_template(name, *a, slug=None, **kw):
        seen["slug"] = slug
        return "{json_schema}{rules}{topic_name}{full_text}{paper_id}{format_note}"

    monkeypatch.setattr(p1, "load_prompt_template", fake_template)
    _run_p1(p1, monkeypatch, projects, cfg_path)
    assert seen["slug"] == "demo"


def test_no_text_records_are_requeued(tmp_path):
    out = tmp_path / "x.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in (
        {"paper_id": "10.1/a", "_status": "no_text"},
        {"paper_id": "10.1/b", "_status": "ok"},
        {"paper_id": "10.1/c", "_status": "no_text"},
    )) + "\n")
    assert load_done_ids_jsonl(out) == {"10.1/a", "10.1/b", "10.1/c"}
    assert requeue_no_text(out, ["10.1/a", "10.1/b"]) == 1
    assert load_done_ids_jsonl(out) == {"10.1/b", "10.1/c"}  # c not in this run


def test_pass2_halt_writes_no_parse_error(tmp_path, monkeypatch):
    p2 = _load("steps/extract_pass2.py", "_p2_halt")

    class Ref:
        paper_id = "10.1/a"
        stem = "10.1--a"
        md_path = xml_path = pdf_path = None

    def fake_call(*a, **k):
        claude_cli._latch_auth_failure("x", "OAuth token has expired")
        return _resp("")

    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("text " * 100, "md"))
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")
    summary = p2.extract_one(Ref(), 1, 1, "sys", "{paper_id} {full_text}", "rct")
    assert summary["_status"] == "halted"
    assert not (tmp_path / "out.jsonl").exists()


def test_pass2_managed_run_ignores_configured_dirs(monkeypatch):
    from ber_core.utils import configured_dir
    cfg = {"dirs": {"eligible_trials": "/elsewhere"}}
    monkeypatch.delenv("BER_MANAGED_RUN", raising=False)
    assert configured_dir(cfg, "eligible_trials") == "/elsewhere"
    monkeypatch.setenv("BER_MANAGED_RUN", "1")
    assert configured_dir(cfg, "eligible_trials") is None
    assert configured_dir({"dirs": {"eligible_trials": "  "}}, "eligible_trials") is None
