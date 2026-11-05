"""The public filter must match review dispositions without inventing clearance."""
from copy import deepcopy

import pytest

from ber_core.inspect_dashboard import dashboard_assessments


def decision(pid, judgment="some concerns", **updates):
    result = {"paper_id": pid, "status": "awaiting_human_review",
              "synthesis_status": "provisional_synthesis", "disagreements": [],
              "assessment": {"eligibility": "randomized_trial", "overall": {"judgment": judgment}},
              "machine_reviewers": 2, "retraction_flag": False,
              "validation_omissions": {}}
    result.update(updates)
    return result


def test_analysis_sets_keep_uncertainty_and_nonrandomized_reports_separate():
    primary = decision("10.test/primary")
    unresolved = decision("10.test/unresolved", disagreements=["overall"])
    nonrandom = decision("10.test/cohort", "not assessed")
    nonrandom["assessment"]["eligibility"] = "nonrandomized_trial"
    retracted = decision("10.test/retracted", synthesis_status="excluded_retracted",
                         retraction_flag=True, notice_confirmed=True)
    missing = decision("10.test/missing", synthesis_status="awaiting_classification", assessment=None)
    no_concerns = decision("10.test/clear", "no concerns")
    papers = dashboard_assessments([primary, unresolved, nonrandom, retracted, missing, no_concerns], [],
                                  assessed_at="2026-09-19", model="gpt-5.6-terra")["papers"]
    assert "rct_primary_no_or_some_concerns" in papers[primary["paper_id"]]["analysisSets"]
    assert papers[unresolved["paper_id"]]["analysisSets"] == ["all_provisional_reports"]
    assert papers[nonrandom["paper_id"]]["analysisSets"] == ["all_provisional_reports", "nonrandomized_reports"]
    assert papers[retracted["paper_id"]]["analysisSets"] == []
    assert papers[retracted["paper_id"]]["retractionConfirmed"] is True
    assert papers[missing["paper_id"]]["judgment"] == "not assessed"
    assert papers[missing["paper_id"]]["analysisSets"] == []
    assert "rct_sensitivity_no_concerns" in papers[no_concerns["paper_id"]]["analysisSets"]


def test_human_resolution_reenters_sets_without_leaking_full_assessments():
    d = decision("10.test/a", disagreements=["overall"], status="human_adjudicated",
                 synthesis_status="synthesis", human_adjudication={"reason": "Verified sources"},
                 validation_omissions={"reviewer_1": [{"finding": "unverified"}]})
    original = deepcopy(d)
    bundle = dashboard_assessments([d], [{"paper_id": d["paper_id"], "selection_reason": "manual_exclusion"}],
                                  assessed_at="2026-09-19", model="gpt-5.6-terra")
    report = bundle["papers"][d["paper_id"]]
    assert report["humanReviewed"] and report["disagreementCount"] == report["withheldFindingCount"] == 0
    assert "rct_primary_no_or_some_concerns" in report["analysisSets"]
    assert report["humanReason"] == "Verified sources"
    assert report["selectionReason"] == "manual_exclusion"
    assert "assessment" not in report and "sources" not in report
    assert bundle["assessedAt"] == "2026-09-19" and len(bundle["decisionsSha256"]) == 64
    assert d == original


def test_normalized_identifier_collision_cannot_silently_replace_a_decision():
    with pytest.raises(ValueError, match="duplicate"):
        dashboard_assessments([decision("10.test/a"), decision("https://doi.org/10.TEST/A#arm")], [],
                              assessed_at="2026-09-19", model="gpt-5.6-terra")
