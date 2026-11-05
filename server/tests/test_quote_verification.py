"""Quote verification (`ber_core/quote_verification.py`).

Every number in the meta-analysis is an LLM assertion. The pass-2 schema's
``*_source`` fields only ever recorded a provenance CLASS ("verbatim",
"calculated") — a self-report, unverifiable. The prompts now also ask for the
actual span, and this module checks it against the converted paper.

The load-bearing property is that a FABRICATED quote is caught, while a quote
mangled by lossy PDF conversion is not punished as if it were fabricated.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core.quote_verification import (  # noqa: E402
    corpus_summary,
    normalize,
    verify_quote,
    verify_record,
)

# `§` is the pipeline's own conversion artifact for `±` (see utils.preprocess_md).
SOURCE = """Results. At 12 weeks, mean systolic blood pressure fell from 143.2 § 11.4
mmHg to 131.7 § 10.2 mmHg in the nattokinase group (n = 40), compared with
142.8 ± 12.1 to 140.1 ± 11.8 in the placebo group. The between-group difference
was -8.4 mmHg (95% CI -12.1 to -4.7; p = 0.003)."""


def test_an_exact_copy_verifies() -> None:
    got = verify_quote("mean systolic blood pressure fell from 143.2 § 11.4", SOURCE)
    assert got["status"] == "exact"
    assert got["offset"] is not None


def test_conversion_artifacts_do_not_break_a_real_quote() -> None:
    """The model may quote `±` where the converted text has `§`, and a table row
    may be split across lines. Neither means the quote was invented."""
    assert verify_quote(
        "mean systolic blood pressure fell from 143.2 ± 11.4", SOURCE
    )["status"] == "exact"
    assert verify_quote(
        "131.7 § 10.2 mmHg in the nattokinase group (n = 40)", SOURCE
    )["status"] == "exact"


def test_a_fabricated_quote_is_caught() -> None:
    """The whole point: a plausible-sounding sentence that is not in the paper."""
    got = verify_quote("patients received 200mg twice daily for six months", SOURCE)
    assert got["status"] == "absent"
    assert got["ratio"] < 0.85


def test_a_near_miss_is_fuzzy_not_absent() -> None:
    """A digit dropped by conversion should degrade to `fuzzy`, not `absent` —
    the distinction between "check this" and "this is fabricated"."""
    got = verify_quote(
        "between-group difference was -8.4 mmHg (95% CI -12.1 to -4.6)", SOURCE
    )
    assert got["status"] == "fuzzy"
    assert got["ratio"] >= 0.85


def test_unverifiable_inputs_are_skipped_not_failed() -> None:
    """`skipped` must be distinct from `absent`: absent implies fabrication."""
    for q in (None, "", "   ", "n = 40"):
        assert verify_quote(q, SOURCE)["status"] == "skipped"
    # No source text (PDF never converted) is not the extraction's fault.
    assert verify_quote("a perfectly good long quote here", "")["status"] == "skipped"


def test_normalize_folds_formatting_not_content() -> None:
    assert normalize("A  b\nc") == normalize("a b c")
    assert normalize("12.4 § 3.1") == normalize("12.4 ± 3.1")
    assert normalize("“quoted”") == normalize('"quoted"')
    assert normalize("en–dash") == normalize("en-dash")
    assert normalize("143.2") != normalize("143.3")  # digits still matter


def test_record_walk_finds_nested_quotes_and_reports_a_rate() -> None:
    rec = {
        "outcomes": [
            {
                "arm_results": [
                    {"result_quote": "131.7 § 10.2 mmHg in the nattokinase group"},
                    {"result_quote": "totally fabricated sentence about dosing"},
                ],
                "between_group_effects": [
                    {"effect_quote": "between-group difference\nwas -8.4 mmHg"},
                ],
            }
        ]
    }
    got = verify_record(rec, SOURCE)
    assert got["n_quoted"] == 3
    assert got["n_absent"] == 1
    assert got["traceable_rate"] == round(2 / 3, 3)
    paths = {d["path"] for d in got["details"]}
    assert "outcomes.0.arm_results.1.result_quote" in paths


def test_a_record_with_no_quotes_has_no_rate() -> None:
    """An unquoted corpus must report `None`, not a misleading 0% or 100%."""
    got = verify_record({"outcomes": [{"arm_results": [{"mean": 1.0}]}]}, SOURCE)
    assert got["n_quoted"] == 0
    assert got["traceable_rate"] is None


def test_corpus_summary_aggregates_records() -> None:
    a = verify_record(
        {"outcomes": [{"arm_results": [
            {"result_quote": "131.7 § 10.2 mmHg in the nattokinase group"}]}]},
        SOURCE,
    )
    b = verify_record(
        {"outcomes": [{"arm_results": [
            {"result_quote": "an entirely invented sentence about something else"}]}]},
        SOURCE,
    )
    tot = corpus_summary([a, b])
    assert tot["n_records"] == 2
    assert tot["n_exact"] == 1 and tot["n_absent"] == 1
    assert tot["traceable_rate"] == 0.5


def test_a_quote_split_by_markup_verifies_against_the_stripped_text() -> None:
    """Raw XML is the wrong haystack on its own.

    `normalize` unescapes entities and collapses whitespace but does NOT strip
    tags, so a quote that reads contiguously in a rendered table sits either
    side of `</td><td>` in the markup — and a legitimate quote is scored a
    fabrication. The stripped rendering is tried second, never first.
    """
    markup = ("<table><tr><td>Systolic BP</td><td>131.7 ± 10.2 mmHg</td></tr>"
              "</table>")
    stripped = "Systolic BP 131.7 ± 10.2 mmHg"
    rec = {"outcomes": [{"arm_results": [
        {"result_quote": "Systolic BP 131.7 ± 10.2 mmHg"}]}]}

    assert verify_record(rec, markup)["n_absent"] == 1
    assert verify_record(rec, markup, alt_text=stripped)["n_absent"] == 0


def test_the_alt_haystack_cannot_rescue_a_fabrication() -> None:
    """Trying a second haystack must not become a second chance to pass."""
    rec = {"outcomes": [{"arm_results": [
        {"result_quote": "an entirely invented sentence about something else"}]}]}
    got = verify_record(rec, SOURCE, alt_text="unrelated stripped text")
    assert got["n_absent"] == 1


def test_alt_text_leaves_a_passing_record_bit_identical() -> None:
    """Existing md/pdf results must not shift because the parameter exists."""
    rec = {"outcomes": [{"arm_results": [
        {"result_quote": "131.7 § 10.2 mmHg in the nattokinase group"}]}]}
    assert verify_record(rec, SOURCE) == verify_record(rec, SOURCE,
                                                       alt_text="anything")


def test_every_pass2_prompt_asks_for_quotes() -> None:
    """Guards the prompt/verifier contract, INCLUDING per-project overrides.

    A project override that lacks the quote fields produces no quotes at all,
    and the traceable rate silently reads as None — indistinguishable from
    "nothing to check". Overrides are easy to forget, so assert over all of them.
    """
    prompts = sorted((REPO_ROOT / "prompts").glob("**/extract_pass2_*.txt"))
    assert prompts, "no pass-2 prompts found"
    for path in prompts:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        assert "PROVENANCE" in text, f"{rel} lost the provenance rule"
        assert "result_quote" in text, f"{rel} has no result_quote"
        assert "effect_quote" in text, f"{rel} has no effect_quote"
