"""`grading.design_class` decides where GRADE starts, so it must not invert.

`grade_certainty` starts an RCT body of evidence at "high" and everything else
at "low" (a two-level gap). Two bugs pushed studies the wrong way across it:

1. The substring test `"randomized" in dt` also matched
   `non-randomized-comparative`, so a deliberately NON-randomized design was
   pooled as an RCT and published at inflated certainty. Confirmed on the
   antiviral-nasal-sprays corpus, where two forest groups contained
   `non-randomized-comparative` studies with no randomization method at all.

2. Some design_type vocabularies name the SETTING rather than the allocation
   ("human-challenge"), which is silent about randomization. Treating the bare
   label as observational demoted real RCTs — 41 of 44 human-challenge records
   in that corpus are double-blind with an explicit randomization_method.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ber_core import grading as M  # noqa: E402


def _rec(design_type=None, **study_design):
    return {"study_design": {"design_type": design_type, **study_design}}


@pytest.mark.parametrize("design_type", [
    "rct", "RCT", "crossover", "cluster-randomized", "factorial",
    "randomized controlled trial",
])
def test_randomized_designs_are_rct(design_type):
    assert M.design_class(_rec(design_type)) == "rct"


@pytest.mark.parametrize("design_type", [
    "non-randomized-comparative",
    "non_randomized",
    "nonrandomized",
    "not randomized",
    "quasi-randomized",
])
def test_negated_randomization_is_not_an_rct(design_type):
    """The regression: "randomized" is a substring of "non-randomized"."""
    assert M.design_class(_rec(design_type)) == "observational"


def test_negation_beats_explicit_allocation_evidence():
    """A stated non-randomized design wins over a stray randomization field."""
    assert M.design_class(
        _rec("non-randomized-comparative", randomization_method="computer")
    ) == "observational"


@pytest.mark.parametrize("design_type", [
    "observational", "case-control", "quasi-experimental", "cohort",
])
def test_observational_designs_stay_observational(design_type):
    assert M.design_class(_rec(design_type)) == "observational"


def test_setting_label_defers_to_allocation_evidence():
    """"human-challenge" describes the setting, not the allocation."""
    assert M.design_class(
        _rec("human-challenge", randomization_method="computer-generated")
    ) == "rct"
    assert M.design_class(
        _rec("human-challenge", allocation_concealment="central")
    ) == "rct"
    # With no allocation evidence at all, stay conservative.
    assert M.design_class(_rec("human-challenge")) == "observational"


def test_design_of_fallback_is_used_when_the_record_has_no_design_type():
    """study_designs.csv is the fallback source; it used to be dead code."""
    assert M.design_class(
        {"study_design": {}, "paper_id": "10.1/x"},
        lambda pid: "rct" if pid == "10.1/x" else None,
    ) == "rct"
    assert M.design_class(
        {"study_design": {}, "paper_id": "10.1/y"},
        lambda pid: "observational",
    ) == "observational"


def test_grade_starting_point_differs_by_class():
    """Pin WHY this matters: the class is a two-level GRADE swing."""
    kw = dict(n_trials=10, total_n=5000, i2=0.0, ci_low=0.2, ci_high=0.6,
              null_value=0.0, rob={"low": 10, "some": 0, "high": 0, "unknown": 0},
              egger={"available": False, "reason": "k<min", "k": 10},
              indirectness_penalty=0)
    rct = M.grade_certainty(design_cls="rct", **kw)["grade"]
    obs = M.grade_certainty(design_cls="observational", **kw)["grade"]
    assert rct != obs
