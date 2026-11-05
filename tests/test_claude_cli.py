"""Tests for claude_cli.py response parsing.

Run with ``python -m pytest test_claude_cli.py``.

The cases below are drawn from a real failing run (the nattokinase review): the
type classifier kept emitting a valid JSON answer followed by a self-correcting
"Wait, I need to reconsider…" paragraph, sometimes with a second JSON object
after it. The old first-``{``-to-last-``}`` span swallowed the whole response and
returned None, which surfaced as ``WARN: Could not parse JSON`` + ``RETRY 1/3``.
"""

from ber_core import claude_cli as c
import pytest


def _parse(text: str):
    return c.parse_json_response(text, "10.1000/test")


def test_plain_object():
    assert _parse('{"article_type": "Clinical Trial", "confidence": "high"}') == {
        "article_type": "Clinical Trial",
        "confidence": "high",
    }


def test_markdown_fenced():
    text = '```json\n{"article_type": "Review", "confidence": "high"}\n```'
    assert _parse(text)["article_type"] == "Review"


def test_first_object_wins_over_reconsidered_second():
    """The exact shape that broke the real run: answer, prose, second answer."""
    text = (
        '```json\n'
        '{"article_type": "Other Observational Study", "confidence": "low"}\n'
        '```\n\n'
        "Wait — I need to reconsider. This is a review article, not an "
        "observational study.\n\n"
        '{"article_type": "Review", "confidence": "high"}'
    )
    assert _parse(text)["article_type"] == "Other Observational Study"


def test_trailing_prose_containing_braces():
    """Prose after the answer may contain stray braces (e.g. a placeholder name)."""
    text = (
        '```json\n{"article_type": "Case Report", "confidence": "high"}\n```\n\n'
        "Wait, I need to reconsider — the instructions say {article_types_list}."
    )
    assert _parse(text)["article_type"] == "Case Report"


def test_leading_prose_with_stray_brace():
    """A brace in the preamble must not derail the scan for the real object."""
    text = (
        "Looking at this abstract {carefully}, I conclude:\n"
        '{"article_type": "Case Series", "confidence": "low"}'
    )
    assert _parse(text)["article_type"] == "Case Series"


def test_nested_object_is_not_truncated():
    """raw_decode must consume the whole outer object, nested braces included."""
    assert _parse('{"a": {"b": 1}, "article_type": "Erratum"}') == {
        "a": {"b": 1},
        "article_type": "Erratum",
    }


def test_truncated_json_returns_none():
    assert _parse('{"article_type": "Clinical') is None


def test_truncated_mapping_does_not_return_first_classification():
    # Luna sometimes omits only the last outer brace of an otherwise full map.
    assert _parse('{"10.1000/a":{"indication":"treatment"},'
                  '"10.1000/b":{"indication":"exclude"}') is None


def test_malformed_outer_record_does_not_return_nested_section():
    assert _parse('{"study_design":{"design_type":"RCT"},'
                  '"participants":BROKEN}') is None


@pytest.mark.parametrize('suffix', [',{"outcome_id":2}]}', ',"risk_of_bias":{}}', ']'])
def test_premature_object_close_does_not_discard_remaining_json(suffix):
    assert _parse('{"outcomes":[{"outcome_id":1}]}' + suffix) is None


def test_literal_source_control_characters_preserve_outer_record():
    assert _parse('{"quote":"line one\nline two\x02",'
                  '"study_design":{"design_type":"RCT"}}') == {
        "quote": "line one\nline two\x02", "study_design": {"design_type": "RCT"}}


def test_no_json_returns_none():
    assert _parse("I cannot classify this abstract.") is None


def test_bare_array_is_not_mistaken_for_an_object():
    assert _parse('[1, 2, 3]') is None


# --- is_refusal ------------------------------------------------------------
#
# Refusal detection runs ONLY after parse_json_response failed and is_truncated
# said False. It exists to stop the retry loop from re-buying the same "this
# paper is not a clinical trial" answer three times at full price. The guards
# matter more than the matches: any brace = a real JSON attempt that must still
# be retried, and long prose = analysis, not refusal.

def test_refusal_short_prose_refusal():
    assert c.is_refusal(
        "This paper cannot be extracted as a clinical trial. I won't fabricate "
        "a JSON extraction for a laboratory biochemistry study.")


def test_refusal_i_am_not_going_to():
    assert c.is_refusal(
        "I'm not going to produce this JSON. The paper is a molecular biology "
        "study with no human participants.")


def test_refusal_false_for_json_attempt():
    # A brace anywhere means the model tried to answer — retry, don't skip.
    assert not c.is_refusal('I could not finish. {"paper_id": "10.1/x", "study')


def test_refusal_false_for_truncated_empty():
    # Empty text is is_truncated's territory; is_refusal must not claim it.
    assert not c.is_refusal("")
    assert not c.is_refusal("   \n  ")


def test_refusal_false_for_long_analysis():
    # 3k chars of brace-free prose is an essay, not a refusal — retry it.
    assert not c.is_refusal("I cannot say this is simple. " + "word " * 800)


def test_refusal_false_for_ordinary_prose():
    assert not c.is_refusal(
        "The study enrolled 120 participants across three sites in Japan.")


def test_refusal_quoting_a_schema_fragment_is_still_detected():
    """P3 regression: a brace must not veto the verdict outright. is_refusal is
    only called after parse_json_response failed, so no well-formed object is
    present — a lone brace is the model QUOTING the schema while declining.
    (Before the fix these refusals burned all 3 retries at full price.)"""
    assert c.is_refusal(
        'I won\'t fabricate the extraction. The schema asks for '
        '"outcomes": {...} but this paper is a fermentation study.')


def test_brace_bearing_reply_needs_refusal_language_up_front():
    """The stricter first-200-chars rule: refusal phrasing buried deep in a
    broken payload does not count — that reply should still be retried."""
    filler = "The study data follows. " * 12   # pushes past 200 chars
    assert not c.is_refusal(filler + 'I cannot parse {"outcomes": ')
