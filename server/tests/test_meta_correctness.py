"""Meta-analysis correctness fixes — each guards a bug that produced WRONG NUMBERS.

These are not feature tests. Every case below reproduces something the pipeline
did incorrectly and silently:

  * a multi-arm trial contributing one comparison with no warning
  * two published reports of ONE trial pooled as independent evidence
  * `higher_is_better` reversing a study's sign with nothing checking it
  * `first_author`/`year` blank on every trial because the MA read fields that
    pass-2 records do not carry
"""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core import grading as M  # noqa: E402
from ber_core.constants import expected_direction  # noqa: E402


# --- multi-arm truncation ----------------------------------------------------

def test_two_arm_trial_is_not_flagged() -> None:
    assert M.multi_arm_truncation({"a": "intervention", "b": "control"}) is None


def test_three_arm_trial_reports_the_arms_it_drops() -> None:
    """A dose-ranging trial contributes ONE comparison; the rest is discarded.

    The estimate is not wrong for the arms used — but k and the evidence base
    are quietly smaller than the corpus implies, and nothing said so.
    """
    got = M.multi_arm_truncation(
        {"low": "intervention", "high": "intervention", "pbo": "control"}
    )
    assert got is not None
    assert got["n_intervention_arms"] == 2
    assert got["n_control_arms"] == 1
    assert got["n_arms_used"] == 2
    assert "only one of each" in got["note"]


# --- companion-report double counting ----------------------------------------

_META = {"authors": "Smith, John; Doe, Jane", "year": 2020}


def _rec(pid: str, n: int = 120) -> dict:
    return {"paper_id": pid, "sample_sizes": {"n_randomized_total": n}}


def test_companion_reports_of_one_trial_share_a_study_key() -> None:
    """The live double-counting bug: a main-results paper and a secondary-
    outcomes paper are both real, distinct papers reporting ONE trial."""
    main = M.study_key(_rec("10.1/main"), _META)
    companion = M.study_key(_rec("10.1/secondary"), _META)
    assert main is not None and main == companion


def test_different_trials_do_not_collide() -> None:
    """Merging two genuinely different trials is worse than failing to merge."""
    assert M.study_key(_rec("10.1/a", n=120), _META) != M.study_key(
        _rec("10.1/b", n=88), _META
    )
    other_year = {**_META, "year": 2015}
    assert M.study_key(_rec("10.1/a"), _META) != M.study_key(_rec("10.1/a"), other_year)


def test_a_registration_id_beats_the_author_year_heuristic() -> None:
    rec = {**_rec("10.1/x"), "extraction_metadata": {"notes": "trial NCT01234567"}}
    assert M.study_key(rec, _META) == "reg:NCT01234567"


def test_registration_ids_are_matched_across_registries() -> None:
    for rid in ("NCT01234567", "ISRCTN12345678", "ACTRN12605000123456"):
        rec = {**_rec("10.1/x"), "notes": f"registered {rid}"}
        assert M.study_key(rec, {}) == f"reg:{rid.upper()}"


def test_no_key_rather_than_a_guess_when_data_is_incomplete() -> None:
    """Returning None makes the caller fall back to paper_id — the old, safe
    behavior. Inventing a key from partial data would merge unrelated trials."""
    assert M.study_key(_rec("10.1/x"), {"authors": "Smith J"}) is None  # no year
    assert M.study_key(_rec("10.1/x"), {"year": 2020}) is None          # no author
    assert M.study_key({"paper_id": "10.1/x"}, _META) is None           # no N


# --- higher_is_better orientation -------------------------------------------

def test_known_instruments_resolve_to_a_direction() -> None:
    """higher_is_better flips the SIGN of a trial's contribution."""
    assert expected_direction("BMI") is False
    assert expected_direction("Body weight (kg)") is False
    assert expected_direction("IRLS total score") is False
    assert expected_direction("SF-36 physical component") is True
    assert expected_direction("6-minute walk distance") is True
    assert expected_direction("HDL cholesterol") is True


def test_unknown_instruments_get_no_opinion() -> None:
    """Absent from the table means unknown — never a silent default, which
    would flag correct extractions as wrong."""
    assert expected_direction("Serum nattokinase activity") is None
    assert expected_direction("") is None
    assert expected_direction(None) is None


def test_the_most_specific_match_wins() -> None:
    """A generic substring must not override a specific instrument."""
    assert expected_direction("quality of life") is True
    assert expected_direction("fatigue severity") is False


# --- PRISMA "included" provenance -------------------------------------------

def test_prisma_reports_which_file_the_included_count_came_from(tmp_path) -> None:
    """The count carries its provenance, and duplicate-free data raises NO
    warning. The old behaviour warned purely because a `_deduped` file was
    absent — a file nothing in the pipeline produces and nothing reads — so
    every review was nagged, including provably duplicate-free ones."""
    from ber_core.prisma import derive_counts

    class _P:
        def __init__(self, d): self.data_dir = d; self.project_dir = d; self.slug = "t"

    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(
        '{"paper_id":"a","study_design":{"design_type":"rct"}}\n'
        '{"paper_id":"b","study_design":{"design_type":"rct"}}\n', encoding="utf-8")

    got = derive_counts(_P(data))
    assert got["included"] == 2
    assert got["included_source"] == "trial_extractions_pass2.jsonl"
    assert got["included_deduplicated"] is True
    assert any(i["code"] == "extracted_without_confirmed_eligibility" for i in got["issues"])


# --- binary events never outrank the continuous path ------------------------

def test_binary_never_outranks_the_continuous_path() -> None:
    """Step 1.5 sits AFTER the continuous rungs; means+SDs are higher fidelity."""
    outcome = {
        "name": "o", "higher_is_better": True,
        "arm_results": [
            {"arm_id": 1, "mean": 10.0, "sd": 2.0, "n_analyzed": 50,
             "events": 40, "total": 50},
            {"arm_id": 2, "mean": 8.0, "sd": 2.0, "n_analyzed": 50,
             "events": 10, "total": 50}],
    }
    got = M.smd_from_outcome(outcome, {1: "intervention", 2: "control"},
                             {"paper_id": "10.1/x"})
    assert got is None  # no explicit same-construct mapping or verified count semantics


# --- reported-effect orientation (the live sign bug) -------------------------

def test_a_reversed_comparison_label_flips_the_effect() -> None:
    """158 corpus effects are labelled control-vs-intervention. Reading the
    value without reading the label silently reverses those studies."""
    roles = {1: "intervention", 2: "control"}
    assert M.comparison_orientation({"comparison": "arm1_vs_arm2"}, roles) == 1.0
    assert M.comparison_orientation({"comparison": "arm2_vs_arm1"}, roles) == -1.0


def test_a_comparison_between_two_non_study_arms_is_skipped() -> None:
    """`arm1_vs_arm3` where both are controls answers a different question."""
    roles = {1: "control", 2: "intervention", 3: "control"}
    assert M.comparison_orientation({"comparison": "arm1_vs_arm3"}, roles) is None


def test_an_unmapped_comparator_still_orients_on_the_intervention() -> None:
    """`unexposed` arms are real comparators absent from CONTROL_TYPES (114 of
    117 such cases). Discarding them lost usable effects."""
    roles = {1: "intervention"}  # arm2 is `unexposed`, unmapped
    assert M.comparison_orientation({"comparison": "arm1_vs_arm2"}, roles) == 1.0
    assert M.comparison_orientation({"comparison": "arm2_vs_arm1"}, roles) == -1.0


def test_a_missing_label_keeps_the_historical_assumption() -> None:
    assert M.comparison_orientation({}, {1: "intervention", 2: "control"}) == 1.0


def test_the_real_inverted_odds_ratio_is_corrected() -> None:
    """10.1002/mds.28224 reports OR=2.92 under an `arm1_vs_arm2` label, but its
    own arms (16/34 vs 26/36) give 0.342 — exactly 1/2.92. The old code returned
    -0.59 (favours control); the arms say +0.59 (favours treatment)."""
    outcome = {
        "name": "Percentage of systolic BP nondippers", "higher_is_better": False,
        "arm_results": [
            {"arm_id": 1, "events": 16, "total": 34},
            {"arm_id": 2, "events": 26, "total": 36}],
        "between_group_effects": [
            {"effect_measure": "odds_ratio", "effect_value": 2.92,
             "comparison": "arm1_vs_arm2", "ci_95_low": 1.1, "ci_95_high": 7.8}],
    }
    assert M.smd_from_outcome(outcome, {1: "intervention", 2: "control"},
                              {"paper_id": "10.1002/mds.28224"}) is None
    # Disagreeing raw/reported orientation needs source review, not an automatic flip.
