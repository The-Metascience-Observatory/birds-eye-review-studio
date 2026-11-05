"""Small, portable dashboard projection of completed INSPECT-SR decisions.

Uses the manuscript's analysis sets; never invents a check-count quality score or
turns missing assessments into evidence of trustworthiness.
"""
from __future__ import annotations

from ber_core.inspect_sources import digest, paper_key


def dashboard_assessments(decisions, records, *, assessed_at, model):
    from ber_core.inspect_report import synthesis_sets

    sets = {key: {d["paper_id"] for d in members}
            for key, members in synthesis_sets(decisions).items()}
    selection = {r["paper_id"]: r for r in records}
    papers = {}
    for d in decisions:
        pid = d["paper_id"]
        key = paper_key(pid)
        if not key or key in papers:
            raise ValueError("Empty or duplicate dashboard assessment identifier: " + pid)
        a = d.get("assessment") or {}
        overall = a.get("overall") or {}
        scope = (d.get("scope_audit") or {}).get("audit") or {}
        human = d.get("status") == "human_adjudicated"
        record = selection.get(pid, {})
        papers[key] = {
            "paperId": key,
            "disposition": d["synthesis_status"],
            "humanReviewed": human,
            "eligibility": a.get("eligibility", "unclear"),
            "eligibilityReason": a.get("eligibility_reason", ""),
            "judgment": overall.get("judgment", "not assessed"),
            "judgmentReason": overall.get("reason", ""),
            "scope": scope.get("scope"),
            "scopeReason": scope.get("reason", ""),
            "selectionReason": record.get("selection_reason"),
            "duplicateOf": record.get("duplicate_of"),
            "humanReason": (d.get("human_adjudication") or {}).get("reason", ""),
            "retractionFlag": bool(d.get("retraction_flag")),
            "retractionConfirmed": bool(d.get("notice_confirmed")),
            "machineReviewers": d.get("machine_reviewers", 0),
            "disagreementCount": 0 if human else len(d.get("disagreements", [])),
            "withheldFindingCount": 0 if human else sum(len(v) for v in d.get("validation_omissions", {}).values()),
            "analysisSets": [name for name, ids in sets.items() if pid in ids],
        }
    return {"version": 1, "assessedAt": assessed_at, "model": model,
            "guidanceVersion": "1.1.2", "decisionsSha256": digest(decisions),
            "papers": papers}
