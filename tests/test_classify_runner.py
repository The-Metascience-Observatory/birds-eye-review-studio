"""classify's worker pool: per-paper isolation, halting, retries, prompts."""
from __future__ import annotations

import csv
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from ber_core import claude_cli
from ber_core import usage_guard
from ber_core.utils import load_done_dois_csv

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("_classify_runner", ROOT / "steps/classify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Resp:
    def __init__(self, text, session_id=None, cost=0.0):
        self.text = text
        self.session_id = session_id
        self.cost_usd = cost
        self.input_tokens = 1
        self.output_tokens = 1


@pytest.fixture
def c3(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "emit_run_summary", lambda *a, **k: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    claude_cli._AUTH_LATCH.clear()
    usage_guard.reset_stop()
    yield mod
    claude_cli._AUTH_LATCH.clear()
    usage_guard.reset_stop()


def _papers(tmp_path, n):
    out = []
    for i in range(n):
        f = tmp_path / f"10.1--p{i}_abstract.md"
        f.write_text("A real abstract about the topic. " * 10)
        out.append((f"10.1/p{i}", f, "abstracts"))
    return out


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_null_and_bool_values_do_not_crash_the_parser(c3):
    row = c3._parse_quick(json.dumps({
        "is_relevant": True, "studies_treatment": None, "article_type": "Review",
        "is_excluded": False, "exclusion_reason": None, "certainty": None,
        "organism": None, "relevance_confidence": None}), "10.1/a", valid_types={"Review"})
    assert row["is_relevant"] == "yes" and row["is_excluded"] == "no"
    assert row["studies_treatment"] == "" and row["organism"] == "other"
    screen = c3._parse_screen(json.dumps({"is_relevant": None, "trial_type": None,
                                          "phase": 2, "is_excluded": None}), "10.1/a")
    assert screen["is_relevant"] == "parse_error" and screen["phase"] == "2"


def test_one_paper_raising_does_not_cost_the_run(c3, tmp_path, monkeypatch):
    calls = []

    def fake_call(prompt, model, doi, **kw):
        calls.append(doi)
        return Resp(json.dumps({"is_relevant": "yes", "confidence": "high",
                                "studies_treatment": "no"}))

    def parse(text, doi, **kw):
        if doi == "10.1/p1":
            raise AttributeError("'NoneType' object has no attribute 'lower'")
        return c3._parse_relevance(text, doi)

    monkeypatch.setattr(c3, "call_claude", fake_call)
    out = tmp_path / "relevance.csv"
    counter = c3._run_pipeline(_papers(tmp_path, 4), out,
                               ["doi", "is_relevant", "confidence", "studies_treatment"],
                               {}, "{abstract}", parse, {}, False, "haiku", workers=2)
    written = {r["doi"] for r in _rows(out)}
    assert written == {"10.1/p0", "10.1/p2", "10.1/p3"}
    assert counter["errors"] == 1
    assert len(calls) == 4  # no paper ran twice


def test_auth_failure_cancels_the_queue(c3, tmp_path, monkeypatch):
    calls = []
    lock = threading.Lock()

    def fake_call(prompt, model, doi, **kw):
        with lock:
            calls.append(doi)
            if len(calls) == 2:
                claude_cli._latch_auth_failure(doi, "OAuth token has expired")
        return Resp(json.dumps({"is_relevant": "yes"}))

    monkeypatch.setattr(c3, "call_claude", fake_call)
    out = tmp_path / "relevance.csv"
    c3._run_pipeline(_papers(tmp_path, 60), out, ["doi", "is_relevant"], {},
                     "{abstract}", c3._parse_relevance, {}, False, "haiku", workers=2)
    assert len(calls) < 10
    with pytest.raises(SystemExit) as exc:
        claude_cli.raise_if_auth_failed()
    assert exc.value.code == 2


def test_usage_stop_cancels_the_queue(c3, tmp_path, monkeypatch):
    calls = []

    def fake_call(prompt, model, doi, **kw):
        calls.append(doi)
        usage_guard._STOP["reason"] = "usage_guard: weekly ceiling"
        return Resp("")

    monkeypatch.setattr(c3, "call_claude", fake_call)
    c3._run_pipeline(_papers(tmp_path, 40), tmp_path / "o.csv", ["doi", "is_relevant"], {},
                     "{abstract}", c3._parse_relevance, {}, False, "haiku", workers=1)
    assert len(calls) <= 2


def test_systematic_failure_stops_the_run(c3, tmp_path, monkeypatch):
    monkeypatch.setattr(c3, "call_claude", lambda *a, **k: Resp("{}"))

    def parse(*a, **k):
        raise RuntimeError("bug")

    with pytest.raises(SystemExit):
        c3._run_pipeline(_papers(tmp_path, 60), tmp_path / "o.csv", ["doi", "is_relevant"],
                         {}, "{abstract}", parse, {}, False, "haiku", workers=1)


def test_stateless_retry_resends_prompt_and_bills_it(c3, tmp_path, monkeypatch):
    prompts = []
    answers = [json.dumps({"article_type": "Narrative", "confidence": "high"}),
               json.dumps({"article_type": "Review", "confidence": "low"})]

    def fake_call(prompt, model, doi, resume_session=None, **kw):
        prompts.append((prompt, resume_session))
        return Resp(answers.pop(0), session_id=None, cost=0.5)

    monkeypatch.setattr(c3, "call_claude", fake_call)
    abst = _papers(tmp_path, 1)[0][1]
    result = c3._classify_one("10.1/p0", abst, "abstracts", {}, "PROMPT {abstract}",
                              c3._parse_type, {"valid_types": {"Review"}},
                              ["doi", "article_type", "confidence"], False, "haiku")
    assert result["article_type"] == "Review"
    assert result["_cost"] == pytest.approx(1.0)
    second_prompt, resumed = prompts[1]
    assert resumed is None
    assert second_prompt.startswith("PROMPT A real abstract")
    assert "'Narrative' is not a valid article type" in second_prompt


def test_session_retry_resumes(c3, tmp_path, monkeypatch):
    seen = []
    answers = [json.dumps({"article_type": "X"}), json.dumps({"article_type": "Review"})]

    def fake_call(prompt, model, doi, resume_session=None, **kw):
        seen.append(resume_session)
        return Resp(answers.pop(0), session_id="sess", cost=0.25)

    monkeypatch.setattr(c3, "call_claude", fake_call)
    abst = _papers(tmp_path, 1)[0][1]
    result = c3._classify_one("10.1/p0", abst, "abstracts", {}, "{abstract}",
                              c3._parse_type, {"valid_types": {"Review"}},
                              ["doi", "article_type", "confidence"], False, "haiku")
    assert seen == [None, "sess"]
    assert result["_cost"] == pytest.approx(0.5)


def test_braces_in_config_values_do_not_break_the_prompt(c3):
    config = {"topic": {"name": "Covid {long}", "description": "uses {curly} braces",
                        "not_about": ["{x}"]},
              "article_types": ["Review {narrative}"]}
    template = ("Topic {topic_name}: {topic_description}\n{not_about_bullets}\n"
                'Answer {{"is_relevant": "yes"}}\n{abstract}')
    prompt = c3._build_prompt_screen(config, template).format(abstract="ABS")
    assert "Covid {long}" in prompt and "uses {curly} braces" in prompt
    assert '{"is_relevant": "yes"}' in prompt and prompt.endswith("ABS")
    quick = c3._build_prompt_quick(config, "{article_types_list} {abstract}")
    assert quick.format(abstract="A") == "- Review {narrative} A"


def test_text_source_recorded_and_added_to_legacy_header(c3, tmp_path, monkeypatch):
    monkeypatch.setattr(c3, "call_claude", lambda *a, **k: Resp(json.dumps(
        {"is_relevant": "yes", "studies_treatment": "yes"})))
    out = tmp_path / "trial_screening.csv"
    out.write_text("doi,source_folder,is_long_covid,studies_treatment\n10.1/old,x,yes,no\n")
    fields = ["doi", "source_folder", "is_relevant", "studies_treatment", "text_source"]
    c3._run_pipeline(_papers(tmp_path, 1), out, fields, {}, "{abstract}",
                     c3._parse_screen, {}, False, "haiku", workers=1)
    rows = _rows(out)
    assert rows[0] == {"doi": "10.1/old", "source_folder": "x", "is_long_covid": "yes",
                       "studies_treatment": "no", "text_source": ""}
    assert rows[1]["is_long_covid"] == "yes"  # written through the alias
    assert rows[1]["text_source"] == "abstract"


def test_done_set_reads_legacy_verdict_column(c3, tmp_path):
    out = tmp_path / "trial_screening.csv"
    out.write_text("doi,is_long_covid\n10.1/a,yes\n10.1/b,unknown\n")
    done = load_done_dois_csv(out, skip_values={"parse_error", "unknown", ""},
                              status_column=c3._status_columns("is_relevant"))
    assert done == {"10.1/a"}


def test_label_task_retries_parse_error_rows(c3, tmp_path):
    out = tmp_path / "spray_types.csv"
    out.write_text("doi,spray_type,agent,confidence\n10.1/a,antiviral,x,high\n"
                   "10.1/b,parse_error,,\n10.1/c,,,\n")
    done = load_done_dois_csv(out, skip_values={"parse_error", ""},
                              status_column=c3._status_columns("spray_type"))
    assert done == {"10.1/a"}


def test_load_done_without_status_column_counts_every_row(tmp_path):
    out = tmp_path / "x.csv"
    out.write_text("doi,is_long_covid\n10.1/a,unknown\n")
    assert load_done_dois_csv(out) == {"10.1/a"}
