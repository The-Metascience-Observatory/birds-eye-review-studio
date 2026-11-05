"""Unit tests for extract_pass2.py's failure handling.

Drawn from a real failing run (the nattokinase review): pass 2 was fed 305
pass-1-ineligible papers, and the model's two ways of declining each broke a
different part of the script:

* Schema-compliant all-null JSON (``"study_design": null``) crashed
  ``_postprocess`` — ``dict.get(k, {})`` returns the STORED None for an
  explicitly-null key, so ``.get("arms")`` raised ``AttributeError`` (the
  ``UNHANDLED ERROR: 'NoneType' object has no attribute 'get'`` flood, 118
  records).
* Prose refusals failed JSON parsing and were retried 3x at full price with a
  resume prompt claiming the "previous JSON output was incomplete" — which the
  model rightly argued with (15 papers x 3 Sonnet calls).

Companion coverage: ``server/tests/test_pass2_input_gate.py`` pins the input
funnel (staged bucket, never converted/); ``tests/test_claude_cli.py`` pins
``is_refusal`` itself.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

from ber_core.utils import resolve_input_dir


def _load_pass2():
    spec = importlib.util.spec_from_file_location("extract_pass2",
                                                  "steps/extract_pass2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def p2():
    return _load_pass2()


def test_large_batch_context_retains_exact_arm_identity(p2):
    arms = [{"arm_id": 1, "label": "Drug", "dose": "900 mg",
             "duration_value": 6, "duration_unit": "months"},
            {"arm_id": 2, "label": "Placebo", "dose": None}]
    record = {"study_design": {"arms": arms}, "participants": {"criteria": "CFS"},
              "outcomes": [{"outcome_id": i, "name": "Outcome " + str(i),
                            "synthesis_population": "Human participants " * 100}
                           for i in range(40)]}
    text, issue = p2._batch_identity_context(record)
    parsed = json.loads(text)
    assert len(text) <= 16000
    assert parsed["study_design"] == record["study_design"]
    assert parsed["participants"] == record["participants"]
    retained = {o["outcome_id"] for o in parsed["outcomes"]}
    assert retained and retained.isdisjoint(issue["omitted_outcome_ids"])
    assert retained | set(issue["omitted_outcome_ids"]) == set(range(40))
    assert len(record["outcomes"]) == 40


def test_batch_context_keeps_study_even_when_it_alone_exceeds_budget(p2):
    record = {"study_design": {"arms": [{"arm_id": 1, "dose": "x" * 500}]},
              "participants": {}, "outcomes": [{"outcome_id": 1, "name": "Fatigue"}]}
    text, issue = p2._batch_identity_context(record, max_chars=100)
    assert json.loads(text)["study_design"] == record["study_design"]
    assert issue["study_context_exceeds_allowance"]
    assert issue["omitted_outcome_ids"] == [1]


def test_small_batch_context_preserves_all_identity_fields(p2):
    outcome = {"outcome_id": 7, "name": "Fatigue", "analysis_basis": "change_from_baseline",
               "timepoints": [{"timepoint_id": 2, "label": "9 months"}]}
    text, issue = p2._batch_identity_context({"outcomes": [outcome]})
    assert issue is None
    parsed = json.loads(text)["outcomes"][0]
    assert all(parsed[k] == v for k, v in outcome.items())


# --- resolve_input_dir precedence ------------------------------------------

def test_explicit_input_wins():
    got = resolve_input_dir("/explicit", "/configured", Path("/fallback"))
    assert got == Path("/explicit")


def test_configured_beats_fallback():
    got = resolve_input_dir(None, "/configured", Path("/fallback"))
    assert got == Path("/configured")


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_configured_is_unset(blank):
    """An empty config value must fall through, never become Path('.')."""
    got = resolve_input_dir(None, blank, Path("/fallback"))
    assert got == Path("/fallback")


# --- _postprocess must tolerate explicit JSON nulls -------------------------

def _all_null_parsed():
    return {
        "paper_id": "10.1000/null",
        "study_design": None,
        "participants": None,
        "sample_sizes": None,
        "baseline_demographics": None,
        "adverse_events": None,
        "outcomes": [],
        "risk_of_bias": None,
        "extraction_metadata": None,
    }


def test_postprocess_survives_all_null_sections(p2):
    """THE crash: every section access must go through `or {}` / `or []`."""
    out = p2._postprocess(_all_null_parsed(), "10.1000/null", "md", "rct")
    assert "no_outcomes_extracted" in out.get("_warnings", [])


def test_postprocess_survives_all_null_with_pdf_source(p2):
    # The pdf-fallback branch touches extraction_metadata too.
    out = p2._postprocess(_all_null_parsed(), "10.1000/null", "pdf", "rct")
    assert "used_pdf_fallback_text" in out.get("_warnings", [])


def test_postprocess_still_validates_real_extractions(p2):
    parsed = {
        "paper_id": "10.1000/real",
        "study_design": {"design_type": "RCT",
                         "arms": [{"arm_id": 1}, {"arm_id": 2}]},
        "sample_sizes": {"n_randomized_total": 100,
                         "per_arm": [{"arm_id": 1, "n_randomized": 50},
                                     {"arm_id": 2, "n_randomized": 30}]},
        "outcomes": [{"outcome_id": 1,
                      "arm_results": [{"arm_id": 3}],
                      "between_group_effects": []}],
    }
    out = p2._postprocess(parsed, "10.1000/real", "md", "rct")
    warns = out.get("_warnings", [])
    assert any(w.startswith("n_randomized_mismatch") for w in warns)
    assert "orphan_arm_id=3_in_outcome_1" in warns


# --- _is_null_extraction ----------------------------------------------------

def test_all_null_is_a_structured_refusal(p2):
    assert p2._is_null_extraction(_all_null_parsed())


def test_populated_design_is_not_a_refusal(p2):
    """A real trial with no extractable outcomes keeps the existing
    no_outcomes_extracted warning path — it must NOT be recorded ineligible."""
    parsed = _all_null_parsed()
    parsed["study_design"] = {"design_type": "RCT", "arms": [{"arm_id": 1}]}
    assert not p2._is_null_extraction(parsed)


def test_outcomes_alone_defeat_the_refusal_check(p2):
    parsed = _all_null_parsed()
    parsed["outcomes"] = [{"outcome_id": 1}]
    assert not p2._is_null_extraction(parsed)


# --- refusal short-circuit in the retry loop --------------------------------

def _fake_response(text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        text=text, session_id="sess-1", elapsed=1.0, cost_usd=0.2,
        input_tokens=100, output_tokens=50, is_policy_error=False)


class _FakeRef:
    paper_id = "10.1000/refused"
    stem = "10.1000--refused"
    # The real PaperRef always carries these. read_best_text is monkeypatched in
    # these tests, but the source-fallback chain reads them directly to decide
    # which rungs are still available — None means "no other source to try".
    md_path = None
    xml_path = None
    pdf_path = None


def test_prose_refusal_costs_exactly_one_call(p2, monkeypatch, tmp_path):
    calls = []

    def fake_call(prompt, model, ident, **kw):
        calls.append(prompt)
        return _fake_response(
            "This paper is a fermentation-optimization study, not a clinical "
            "trial. I won't fabricate a JSON extraction for it.")

    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("text " * 100, "md"))
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")

    summary = p2.extract_one(_FakeRef(), 1, 1, "sys", "{paper_id} {full_text}",
                             "rct")

    assert len(calls) == 1, "a refusal must never be retried"
    assert summary["_status"] == "refused"
    rec = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert rec["_status"] == "refused"
    assert rec["paper_id"] == "10.1000/refused"


def test_truncated_response_still_retries(p2, monkeypatch, tmp_path):
    """The short-circuit must not eat the legitimate truncation-retry path."""
    calls = []
    good = json.dumps({"study_design": {"design_type": "RCT", "arms": []},
                       "sample_sizes": None,
                       "outcomes": [{"outcome_id": 1, "arm_results": []}]})

    def fake_call(prompt, model, ident, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return _fake_response('{"study_design": {"design_type": "RCT"')
        return _fake_response(good)

    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("text " * 100, "md"))
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")
    # Provenance needs real quotes; not the subject here.
    monkeypatch.setattr(p2, "verify_record", lambda rec, text, **kw: {})

    summary = p2.extract_one(_FakeRef(), 1, 1, "sys", "{paper_id} {full_text}",
                             "rct")

    assert len(calls) == 2
    assert summary["_status"] == "ok"
    # The retry must have used the truncation prompt (honest reason).
    assert calls[1] == p2._RETRY_RESUME_PROMPT


@pytest.mark.parametrize("first_text,truncated", [
    ("", False), ("invalid JSON", False), ('{"study_design": {', True),
])
def test_stateless_retry_preserves_requested_data_scope(p2, monkeypatch,
                                                       first_text, truncated):
    calls = []
    outcomes = [{"outcome_id": i, "name": f"Outcome {i}", "arm_results": []}
                for i in range(1, 10)]
    good = json.dumps({"study_design": {"design_type": "RCT", "arms": []},
                       "sample_sizes": None, "outcomes": outcomes})

    def fake_call(prompt, model, ident, **kwargs):
        calls.append((prompt, kwargs))
        response = _fake_response(first_text if len(calls) == 1 else good)
        response.session_id = None
        return response

    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2.time, "sleep", lambda _: None)
    monkeypatch.setattr(p2, "run_halted", lambda: False)
    source_prompt = "Extract all outcomes, timepoints and pairwise comparisons. SOURCE"
    result = p2._run_extraction(source_prompt, "10.1000/retry", 1, 1)
    assert len(calls) == 2
    assert result.result["outcomes"] == outcomes
    retried, kwargs = calls[1]
    assert retried.startswith(source_prompt + "\n\n")
    assert "Limit outcomes" not in retried
    assert "only include comparisons vs the control" not in retried
    assert "resume_session" not in kwargs
    if truncated:
        assert "Preserve the originally requested extraction scope" in retried
    else:
        assert retried.endswith(p2._RETRY_MALFORMED_PROMPT)
        assert "got truncated" not in retried


def test_all_null_result_writes_ineligible_not_crash(p2, monkeypatch, tmp_path):
    def fake_call(prompt, model, ident, **kw):
        return _fake_response(json.dumps(_all_null_parsed()))

    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("text " * 100, "md"))
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")

    summary = p2.extract_one(_FakeRef(), 1, 1, "sys", "{paper_id} {full_text}",
                             "rct")

    assert summary["_status"] == "ineligible"
    rec = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert rec["_status"] == "ineligible"


@pytest.mark.parametrize("notes", [
    "Full numeric results are provided in Tables 2-5; the schema outcome array is abbreviated here because rows have the same structure.",
    "Table 1 contains six vehicle rows; only selected category outcomes are represented above.",
])
def test_model_admitted_abbreviation_keeps_numbers_but_requires_review(p2, monkeypatch, tmp_path, notes):
    record = {"study_design": {"design_type": "RCT", "arms": []},
              "outcomes": [{"outcome_id": 1, "name": "Fatigue", "arm_results": [
                  {"arm_id": 1, "mean": 7.1, "sd": 1.2}]}],
              "extraction_metadata": {"extractor_notes": notes}}
    monkeypatch.setattr(p2, "call_claude", lambda *a, **kw: _fake_response(json.dumps(record)))
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("text " * 100, "md"))
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")
    p2.extract_one(_FakeRef(), 1, 1, "sys", "{paper_id} {full_text}", "rct")
    saved = json.loads((tmp_path / "out.jsonl").read_text())
    assert saved["outcomes"][0]["arm_results"][0]["mean"] == 7.1
    assert saved["_extraction_inputs"]["status"] == "partial"
    assert saved["_extraction_inputs"]["requires_review"] is True
    assert any(f["code"] == "model_declared_incomplete_extraction" and
               f["severity"] == "blocking" for f in saved["_source_findings"])


def test_abbreviated_instrument_is_not_an_admitted_omission(p2):
    assert p2._declared_extraction_omissions({"extraction_metadata": {
        "extractor_notes": "The abbreviated fatigue questionnaire was used; outcomes are not abbreviated."}}) == []
    note = "The extraction was abbreviated to fit."
    assert p2._declared_extraction_omissions({"_batch_metadata": [
        {"record": {"extraction_metadata": {"extractor_notes": note}}}]}) == [note]


@pytest.mark.parametrize("provider", ["codex_cli", "openrouter"])
def test_unavailable_pdf_fallback_keeps_empty_paper_for_review(p2, monkeypatch, tmp_path, provider):
    calls = []
    def fake_call(prompt, model, ident, **kw):
        calls.append(kw)
        return _fake_response(json.dumps(_all_null_parsed()))

    ref = _FakeRef()
    ref.pdf_path = tmp_path / "paper.pdf"
    ref.pdf_path.write_bytes(b"placeholder PDF; no transport may read this")
    monkeypatch.setattr(p2, "effective_provider", lambda: provider)
    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("text " * 100, "md"))
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")
    summary = p2.extract_one(ref, 1, 1, "sys", "{paper_id} {full_text}", "rct")
    record = json.loads((tmp_path / "out.jsonl").read_text())
    assert len(calls) == 1
    assert summary["_status"] == "low_quality_text"
    assert record["_extraction_inputs"]["requires_review"] is True
    assert record["_extraction_inputs"]["issues"] == [{
        "code": "fallback_source_unavailable", "source": "paper.pdf",
        "source_kind": "pdf-native", "provider": provider}]


@pytest.mark.parametrize('design,expect_mismatch', [('crossover', False), ('RCT', True)])
def test_sample_size_warning_respects_overlapping_crossover_exposures(design, expect_mismatch):
    p2 = _load_pass2()
    record = {'study_design': {'design_type': design},
        'sample_sizes': {'n_randomized_total': 15, 'per_arm': [
            {'arm_id': 1, 'n_randomized': 15}, {'arm_id': 2, 'n_randomized': 15}]},
        'outcomes': []}
    result = p2._postprocess(record, '10.test/crossover', 'md', 'rct')
    assert any(w.startswith('n_randomized_mismatch') for w in result.get('_warnings', [])) == expect_mismatch


@pytest.mark.parametrize('response', [
    '{"design_type": "RCT", "arms": []}',
    '{"study_design": {"design_type": "RCT", "arms": []}, "outcomes": [oops]}',
    '{"message": "try again"}',
])
def test_nested_or_unrelated_json_is_not_an_empty_extraction(p2, response):
    assert p2._parse_extraction(response, '10.1000/parser') is None


def test_parser_keeps_explicit_null_extraction(p2):
    parsed = p2._parse_extraction(json.dumps(_all_null_parsed()), '10.1000/parser')
    assert parsed is not None
    assert p2._is_null_extraction(parsed)


def test_premature_close_does_not_silently_drop_an_outcome(p2):
    response = ('{"study_design":{"design_type":"RCT"},'
                '"outcomes":[{"name":"depression"}]},'
                '{"name":"anxiety"}],"risk_of_bias":{}}')
    assert p2._parse_extraction(response, '10.1000/early-close') is None


def test_outer_extraction_survives_literal_pdf_control_character(p2):
    raw = ('{"study_design":{"design_type":"RCT"},"sample_sizes":null,'
           '"outcomes":[{"name":"fatigue","result_quote":"group '+chr(2)+' time"}]}')
    result = p2._parse_extraction(raw, '10.1000/control')
    assert result['study_design']['design_type'] == 'RCT'
    assert result['outcomes'][0]['result_quote'] == 'group '+chr(2)+' time'
    assert result['_parse_diagnostics'] == ['literal_control_characters_preserved_in_strings']
    assert json.loads(json.dumps(result)) == result
