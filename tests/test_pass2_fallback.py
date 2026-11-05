"""The pass-2 source chain: XML -> markdown -> the model reading the PDF itself.

A record can be perfectly well-formed and still carry no numbers, because the
source it was read from lost its TABLES — an image-only <table-wrap> in the XML,
or a `grobid-only` conversion of the PDF. That is the failure this chain exists
for, and it is invisible to `_is_null_extraction` (which only fires when design,
sample sizes AND outcomes are all empty).

The subtlest thing under test is the haystack rebinding. `verify_record` checks
every quote against `full_text`; if a fallback swaps the record but not the text
it came from, every quote scores absent and `traceable_rate` — the number that
makes the provenance fields mean anything — collapses corpus-wide with no error
anywhere. That is asserted explicitly below.
"""
from __future__ import annotations

import importlib.util
import json

import pytest


def _load_pass2():
    spec = importlib.util.spec_from_file_location("extract_pass2",
                                                  "steps/extract_pass2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def p2():
    return _load_pass2()


# --- has_outcome_data -------------------------------------------------------

def _rec(outcomes):
    return {"paper_id": "10.1/a", "study_design": {"design_type": "RCT"},
            "outcomes": outcomes}


def test_no_numbers_anywhere_is_empty(p2):
    """The exact shape a table-free source produces: named outcomes, no values."""
    rec = _rec([{"name": "NIHSS", "arm_results": [], "between_group_effects": []},
                {"name": "Barthel", "arm_results": None,
                 "between_group_effects": None}])
    assert p2.has_outcome_data(rec) is False


def test_nulls_in_arm_results_are_still_empty(p2):
    rec = _rec([{"name": "NIHSS",
                 "arm_results": [{"arm": "treatment", "mean": None, "sd": None}]}])
    assert p2.has_outcome_data(rec) is False


def test_one_effect_value_is_enough(p2):
    rec = _rec([{"name": "NIHSS",
                 "between_group_effects": [{"effect_value": -1.4}]}])
    assert p2.has_outcome_data(rec) is True


def test_one_arm_mean_is_enough(p2):
    rec = _rec([{"name": "NIHSS", "arm_results": [{"arm": "t", "mean": 4.2}]}])
    assert p2.has_outcome_data(rec) is True


def test_event_counts_count(p2):
    rec = _rec([{"name": "AEs", "arm_results": [{"arm": "t", "events": 3}]}])
    assert p2.has_outcome_data(rec) is True


def test_a_record_with_no_outcomes_is_empty(p2):
    assert p2.has_outcome_data({"paper_id": "10.1/a"}) is False


def test_malformed_outcomes_do_not_raise(p2):
    assert p2.has_outcome_data(_rec(["not a dict", None])) is False


# --- the chain --------------------------------------------------------------

class _Ref:
    """A PaperRef stand-in whose paths exist on disk."""
    def __init__(self, tmp_path, md=True, pdf=True, xml=True):
        self.paper_id = "10.1/a"
        self.stem = "10.1--a"
        self.dir = tmp_path
        self.md_path = tmp_path / "body.md" if md else None
        self.xml_path = tmp_path / "10.1--a.xml" if xml else None
        self.pdf_path = tmp_path / "10.1--a.pdf" if pdf else None
        for p in (self.md_path, self.xml_path, self.pdf_path):
            if p:
                p.write_text("x")


def test_fallback_order_is_xml_then_md_then_native_pdf(p2, tmp_path, monkeypatch):
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("MD TEXT", "md"))
    rungs = list(p2._fallback_sources(_Ref(tmp_path), "xml"))
    assert [source for _, source, _ in rungs] == ["md", "pdf-native"]


def test_the_native_rung_hands_over_the_file_not_text(p2, tmp_path, monkeypatch):
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("MD TEXT", "md"))
    ref = _Ref(tmp_path)
    _, source, read_files = list(p2._fallback_sources(ref, "xml"))[-1]
    assert source == "pdf-native"
    assert read_files == [ref.pdf_path]


def test_the_chain_never_walks_back_up(p2, tmp_path, monkeypatch):
    """After markdown, re-reading the XML would be revisiting a source the
    chain already preferred and already failed on."""
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("MD TEXT", "md"))
    rungs = list(p2._fallback_sources(_Ref(tmp_path), "md"))
    assert [source for _, source, _ in rungs] == ["pdf-native"]


def test_a_paper_with_no_other_source_offers_nothing(p2, tmp_path):
    ref = _Ref(tmp_path, md=False, pdf=False)
    assert list(p2._fallback_sources(ref, "xml")) == []


# --- the integration that matters -------------------------------------------

def _wire(p2, monkeypatch, tmp_path, responses):
    """Drive extract_one with a scripted sequence of model responses."""
    seen = {"prompts": [], "read_files": [], "verify": []}
    calls = iter(responses)

    def fake_call(prompt, model, ident, **kw):
        seen["prompts"].append(prompt)
        seen["read_files"].append(kw.get("read_files"))
        return type("R", (), {
            "text": json.dumps(next(calls)), "elapsed": 1.0, "cost_usd": 0.1,
            "input_tokens": 10, "output_tokens": 10, "session_id": "s",
            "is_policy_error": False})()

    def fake_verify(rec, text, alt_text=None):
        seen["verify"].append(text)
        return {"traceable_rate": 1.0}

    monkeypatch.setattr(p2, "call_claude", fake_call)
    monkeypatch.setattr(p2, "verify_record", fake_verify)
    monkeypatch.setattr(p2, "_MODEL", "sonnet")
    monkeypatch.setattr(p2, "_OUTPUT_PATH", tmp_path / "out.jsonl")
    return seen


EMPTY = {"paper_id": "10.1/a", "study_design": {"design_type": "RCT"},
         "participants": {"n_randomized": 40},
         "outcomes": [{"name": "NIHSS", "arm_results": [], "between_group_effects": []}]}
FULL = {"paper_id": "10.1/a", "study_design": {"design_type": "RCT"},
        "participants": {"n_randomized": 40},
        "outcomes": [{"name": "NIHSS",
                      "arm_results": [{"arm": "t", "mean": 4.2}]}]}


def test_an_empty_xml_extraction_retries_against_markdown(p2, tmp_path, monkeypatch):
    seen = _wire(p2, monkeypatch, tmp_path, [EMPTY, FULL])
    monkeypatch.setattr(p2, "read_best_text",
                        lambda ref, **kw: ("XML TEXT", "xml")
                        if kw.get("xml_first") else ("MD TEXT", "md"))

    summary = p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                             "{full_text}|{paper_id}|{format_note}", "rct")

    assert summary["_status"] == "ok"
    assert len(seen["prompts"]) == 2, "the empty result must be retried once"
    assert "MD TEXT" in seen["prompts"][1]


def test_the_quote_haystack_follows_the_winning_record(p2, tmp_path, monkeypatch):
    """The trap. If `full_text` is not rebound alongside `result`, every quote
    from the markdown re-extraction is checked against the XML and
    traceable_rate collapses corpus-wide, with no error anywhere."""
    seen = _wire(p2, monkeypatch, tmp_path, [EMPTY, FULL])
    monkeypatch.setattr(p2, "read_best_text",
                        lambda ref, **kw: ("XML TEXT", "xml")
                        if kw.get("xml_first") else ("MD TEXT", "md"))

    p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                   "{full_text}|{paper_id}|{format_note}", "rct")

    assert seen["verify"] == ["MD TEXT"], \
        "verify_record must see the text the winning record was extracted from"


def test_the_record_records_which_source_won(p2, tmp_path, monkeypatch):
    _wire(p2, monkeypatch, tmp_path, [EMPTY, FULL])
    monkeypatch.setattr(p2, "read_best_text",
                        lambda ref, **kw: ("XML TEXT", "xml")
                        if kw.get("xml_first") else ("MD TEXT", "md"))

    p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                   "{full_text}|{paper_id}|{format_note}", "rct")

    rec = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert rec["_text_source"] == "md"
    assert rec["_source_file"] == "body.md"
    assert "refetched_from_md" in rec["_warnings"]


def test_a_successful_first_pass_costs_exactly_one_call(p2, tmp_path, monkeypatch):
    seen = _wire(p2, monkeypatch, tmp_path, [FULL])
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("XML TEXT", "xml"))

    p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                   "{full_text}|{paper_id}|{format_note}", "rct")

    assert len(seen["prompts"]) == 1, "a record with numbers must not be retried"
    assert seen["verify"] == ["XML TEXT"]


def test_partial_xml_uses_pdf_conversion_even_when_first_result_has_numbers(p2, tmp_path, monkeypatch):
    seen = _wire(p2, monkeypatch, tmp_path, [FULL, FULL])
    ref = _Ref(tmp_path)
    ref.md_path.write_text('<table><tr><td>NIHSS</td><td>4.2</td></tr></table>')
    xml = '<body><p>Mean 4.2</p><table-wrap><graphic href="results.png"/></table-wrap></body>'
    monkeypatch.setattr(p2, 'read_best_text', lambda ref, **kw: (xml, 'xml'))
    p2.extract_one(ref, 1, 1, 'sys', '{full_text}|{paper_id}|{format_note}', 'rct')
    assert len(seen['prompts']) == 2
    assert '<table>' in seen['prompts'][1]
    record = json.loads((tmp_path/'out.jsonl').read_text())
    assert record['_text_source'] == 'xml'
    assert record['outcomes'][0]['arm_results'][0]['mean'] == 4.2
    assert record['_extraction_inputs']['requires_review']
    assert len(record['_extraction_inputs']['sources']) == 2


def test_retry_can_be_turned_off(p2, tmp_path, monkeypatch):
    seen = _wire(p2, monkeypatch, tmp_path, [EMPTY])
    monkeypatch.setattr(p2, "read_best_text", lambda ref, **kw: ("XML TEXT", "xml"))

    p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                   "{full_text}|{paper_id}|{format_note}", "rct",
                   retry_on_empty=False)

    assert len(seen["prompts"]) == 1


def test_the_chain_reaches_the_native_pdf_rung(p2, tmp_path, monkeypatch):
    """Both text sources came up empty — only the page images are left."""
    monkeypatch.setattr(p2, "effective_provider", lambda: "claude_cli")
    seen = _wire(p2, monkeypatch, tmp_path, [EMPTY, EMPTY, FULL])
    monkeypatch.setattr(p2, "read_best_text",
                        lambda ref, **kw: ("XML TEXT", "xml")
                        if kw.get("xml_first") else ("MD TEXT", "md"))

    p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                   "{full_text}|{paper_id}|{format_note}", "rct")

    assert len(seen["prompts"]) == 3
    assert seen["read_files"][2] is not None, "the model must be handed the PDF"
    rec = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert rec["_text_source"] == "pdf-native"
    # No text haystack exists for page images, so claiming a traceable_rate of
    # zero would read as fabrication rather than "not checkable".
    assert rec["_provenance"]["status"] == "unverifiable"


def test_a_still_empty_fallback_keeps_the_original_record(p2, tmp_path, monkeypatch):
    """Every rung came up empty: report the first verdict, don't invent one."""
    _wire(p2, monkeypatch, tmp_path, [EMPTY, EMPTY, EMPTY])
    monkeypatch.setattr(p2, "read_best_text",
                        lambda ref, **kw: ("XML TEXT", "xml")
                        if kw.get("xml_first") else ("MD TEXT", "md"))

    summary = p2.extract_one(_Ref(tmp_path), 1, 1, "sys",
                             "{full_text}|{paper_id}|{format_note}", "rct")

    rec = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert summary["_status"] == "ok"
    assert rec["_text_source"] == "xml"
    assert not any(w.startswith("refetched_from_") for w in rec.get("_warnings", []))
