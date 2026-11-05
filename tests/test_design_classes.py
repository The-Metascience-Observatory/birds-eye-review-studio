"""One definition of randomized vs not, shared by every consumer.

`ber_core.scientific.design_label` is the single classifier. The pooler, GRADE
(`grading.design_class`), the dashboard's `is_rct` flag and pass-2 bucket
routing (`stage_eligible._route`) all call it; they used to be five separate
definitions that disagreed on quasi-experimental and human-challenge trials.
"""
import importlib.util
from pathlib import Path

import pytest

from ber_core import dashboard_summary as ds
from ber_core import grading
from ber_core.scientific import design, design_class, design_label, is_randomized
from ber_core.constants import FAILED_STATUS, is_failed_status

ROOT = Path(__file__).resolve().parents[1]


def _rec(design_type=None, **sd):
    return {"study_design": {"design_type": design_type, **sd}}


# (label, randomization evidence, fine class)
VOCAB = [
    # pass-2 design_type vocabulary
    ("RCT", False, "rct"),
    ("rct", False, "rct"),
    ("randomized controlled trial", False, "rct"),
    ("Randomised, double-blind, placebo-controlled", False, "rct"),
    ("factorial", False, "rct"),
    ("crossover", False, "crossover"),
    ("cross-over", False, "crossover"),
    ("cluster-randomized", False, "cluster_rct"),
    ("cluster", False, "unknown"),
    ("cluster", True, "cluster_rct"),
    ("quasi-experimental", False, "observational"),
    ("quasi-experimental", True, "observational"),
    ("quasi-randomized", False, "observational"),
    ("non-randomized-comparative", False, "observational"),
    ("non-randomized-comparative", True, "observational"),
    ("non_randomized_trial", False, "observational"),
    ("nonrandomized", False, "observational"),
    ("not randomized", False, "observational"),
    ("non-randomized crossover", False, "observational"),
    ("prospective_cohort", False, "observational"),
    ("retrospective_cohort", False, "observational"),
    ("case_control", False, "observational"),
    ("cross_sectional", False, "observational"),
    ("before_after", False, "observational"),
    ("interrupted_time_series", False, "observational"),
    ("case_series", False, "observational"),
    # pass-1 study_design vocabulary
    ("single-arm", False, "observational"),
    ("observational", False, "observational"),
    # setting labels defer to allocation evidence
    ("human-challenge", True, "rct"),
    ("human-challenge", False, "observational"),
    # unresolved
    ("", False, "unknown"),
    (None, False, "unknown"),
    ("other", False, "unknown"),
    ("unclear", False, "unknown"),
    ("other", True, "rct"),
]


@pytest.mark.parametrize("label,evidence,expected", VOCAB)
def test_design_vocabulary(label, evidence, expected):
    assert design_label(label, evidence) == expected


@pytest.mark.parametrize("label,evidence,expected", VOCAB)
def test_every_consumer_agrees(label, evidence, expected):
    sd = {"randomization_method": "computer-generated list"} if evidence else {}
    rec = _rec(label, **sd)
    randomized = expected in {"rct", "cluster_rct", "crossover"}
    assert design(rec) == expected
    assert is_randomized(rec) is randomized
    assert design_class(rec) == ("rct" if randomized else expected)
    # GRADE starting class: unresolved is conservatively observational.
    assert grading.design_class(rec) == ("rct" if randomized else "observational")
    assert ds.record_is_rct(rec) is randomized


@pytest.mark.parametrize("sd,expected", [
    ({"randomization_method": "computer-generated"}, True),
    ({"randomization_method": "not reported"}, False),
    ({"randomization_method": "Block randomization", "randomization_method_source": "not_reported"}, False),
    ({"allocation_concealment": "central"}, True),
    ({"allocation_concealment": "unclear"}, False),
    ({}, False),
])
def test_allocation_evidence(sd, expected):
    assert is_randomized(_rec("human-challenge", **sd)) is expected


def _stage_eligible():
    spec = importlib.util.spec_from_file_location("stage_eligible_design", ROOT / "ber_core/stage_eligible.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("study_design,trial_type,include_obs,bucket", [
    ("RCT", None, True, "rct"),
    ("crossover", None, True, "rct"),
    ("quasi-experimental", None, True, "observational"),
    # Without an observational extraction the RCT prompt is the only path for a
    # non-randomized TRIAL; analysis still classes it non-randomized.
    ("quasi-experimental", None, False, "rct"),
    ("observational", None, False, "observational"),
    ("single-arm", None, True, "observational"),
    ("other", "rct", True, "rct"),
    ("other", "retrospective_cohort", True, "observational"),
    (None, "non_randomized_trial", True, "observational"),
    (None, None, True, "rct"),
    ("other", "unclear", True, "rct"),
])
def test_stage_routing_uses_the_shared_definition(study_design, trial_type, include_obs, bucket):
    assert _stage_eligible()._route(study_design, trial_type, include_obs) == bucket


def test_failed_status_is_the_single_superset():
    from ber_core import utils
    assert utils.FAILED_EXTRACTION_STATUSES is FAILED_STATUS
    for s in ("no_text", "parse_error", "error", "no_json", "truncated", "policy_skip",
              "refused", "ineligible", "low_quality_text"):
        assert is_failed_status(s)
    assert not is_failed_status("ok") and not is_failed_status(None)
