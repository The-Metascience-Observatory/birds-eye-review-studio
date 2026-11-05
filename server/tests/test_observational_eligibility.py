"""`include_observational: true` must actually admit observational studies.

The bug: pass-1 eligibility hard-required `is_clinical_trial = "yes"` AND
`has_control_arm = true` whenever `study_design_and_control_arm` was on (the
default). Cohort and case-control designs essentially never satisfy that, so
every observational paper was marked ineligible, `eligible_observational/` stayed
empty, and the observational pass-2 step extracted nothing — on reviews that had
explicitly opted IN. Measured: 17 of VNS-for-insomnia's observational papers and
3 of nattokinase's were silently discarded.

The gate lives in THREE places that must agree, or one silently re-blocks what
another allowed:
  1. the prompt rule            (extract_pass1.build_pass1_prompt)
  2. the python enforcement     (extract_pass1._enforce_eligibility)
  3. the staging mirror         (stage_eligible._component_ineligible)

Descriptive observational reports need no comparator. Intervention trials still
require a control arm. Eligibility does not imply that a report can be pooled.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pass1():
    return _load("_p1_mod", "steps/extract_pass1.py")


@pytest.fixture(scope="module")
def staging():
    return _load("_se_mod", "ber_core/stage_eligible.py")


def _obs_record(*, control_arm=False, verdict="no"):
    """A typical observational paper: not a 'trial', no randomized control arm."""
    return {
        "is_protocol_not_article": {"verdict": False},
        "is_clinical_trial": {"verdict": verdict, "study_design": "observational",
                              "has_control_arm": control_arm},
        "is_relevant": {"verdict": "yes"},
        "organism": {"verdict": "human"},
    }


# --- 1. the prompt rule -----------------------------------------------------

def test_prompt_gate_relaxes_only_for_observational_reviews(pass1) -> None:
    # Absolute prompts_dir: pytest runs from server/, the templates live at the
    # repo root, and load_prompt_template resolves relative to the cwd.
    base = pass1.load_prompt_template(
        "extract_pass1.txt", prompts_dir=REPO_ROOT / "prompts")

    def rule(include_observational: bool) -> str:
        flags = dict(pass1.PASS1_FIELD_DEFAULTS)
        flags["include_animal_studies"] = False
        flags["include_observational"] = include_observational
        prompt = pass1.build_pass1_prompt(base, "topic", flags)
        return next(l for l in prompt.splitlines()
                    if "eligible_for_pass2 to true ONLY if" in l)

    trial_only = rule(False)
    assert 'is_clinical_trial = "yes"' in trial_only
    assert "has_control_arm = true" in trial_only
    assert "observational" not in trial_only, "a trial-only review must not relax"

    observational = rule(True)
    assert 'study_design = "observational"' in observational
    # The OR must be PARENTHESISED: conditions are joined with AND, so a bare
    # "X OR Y" would parse as "(… AND X) OR (Y AND …)" and let an observational
    # paper bypass the protocol/relevance/organism checks entirely.
    assert ('(study_design = "observational" OR '
            '(is_clinical_trial = "yes" AND has_control_arm = true))') in observational
    assert "comparison group" not in observational
    for still_required in ("is_protocol_not_article = false", 'organism = "human"'):
        assert still_required in observational


# --- 2. the python enforcement ---------------------------------------------

def test_enforcement_admits_observational_only_when_opted_in(pass1) -> None:
    flags = dict(pass1.PASS1_FIELD_DEFAULTS)
    flags["include_animal_studies"] = False

    # _enforce_eligibility mutates the record in place and returns None; the
    # gate lives under `screening`.
    trial_only = {"screening": {"eligible_for_pass2": True, **_obs_record()}}
    pass1._enforce_eligibility(trial_only, {**flags, "include_observational": False})

    obs_review = {"screening": {"eligible_for_pass2": True, **_obs_record()}}
    pass1._enforce_eligibility(obs_review, {**flags, "include_observational": True})

    assert trial_only["screening"]["eligible_for_pass2"] is False, (
        "a trial-only review must still reject an observational paper")
    assert obs_review["screening"]["eligible_for_pass2"] is True, (
        "an observational review must admit it")


# --- 3. the staging mirror --------------------------------------------------

def test_staging_mirror_agrees_with_the_prompt(staging) -> None:
    """The defense-in-depth gate must not re-block what pass 1 allowed."""
    rec = _obs_record()
    assert staging._component_ineligible(rec, False, False) in (
        "not_a_trial", "no_control_arm")
    assert staging._component_ineligible(rec, False, True) is None


def test_single_arm_study_is_still_rejected(staging) -> None:
    """Observational eligibility must not admit an uncontrolled intervention trial."""
    single_arm = {
        "is_protocol_not_article": {"verdict": False},
        "is_clinical_trial": {"verdict": "no", "study_design": "single-arm",
                              "has_control_arm": False},
        "is_relevant": {"verdict": "yes"},
        "organism": {"verdict": "human"},
    }
    assert staging._component_ineligible(single_arm, False, True) is not None


def test_trial_only_review_is_completely_unchanged(staging) -> None:
    """The default path must behave exactly as before the fix."""
    rct = {
        "is_protocol_not_article": {"verdict": False},
        "is_clinical_trial": {"verdict": "yes", "study_design": "RCT",
                              "has_control_arm": True},
        "is_relevant": {"verdict": "yes"},
        "organism": {"verdict": "human"},
    }
    assert staging._component_ineligible(rct, False, False) is None
    no_control = {**rct, "is_clinical_trial": {"verdict": "yes", "study_design": "RCT",
                                               "has_control_arm": False}}
    assert staging._component_ineligible(no_control, False, False) == "no_control_arm"
