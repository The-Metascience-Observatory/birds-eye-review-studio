"""Derive PRISMA counts from upstream artifacts."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from .projects import Project

from ber_core.constants import PRISMA_ELIGIBLE_TYPES  # noqa: E402
from ber_core.utils import (  # noqa: E402
    read_jsonl,
    drop_duplicate_records,
    find_duplicate_candidates,
    is_unpublishable_record,
    load_duplicate_dois,
    load_exclusion_dois,
    norm_paper_id,
)


def _load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _jsonl_records(path: Path) -> list[dict]:
    """Every JSON object in a JSONL, in file order. Unreadable file or
    unparseable line degrades to skipping it — a partial funnel beats an error."""
    try:
        return read_jsonl(path, on_error="skip")
    except OSError:
        return []


def derive_counts(project: Project, *, final_records: list[dict] | None = None) -> dict[str, Any]:
    """Reconcile identifier sets; absent work is pending, never an exclusion.

    Historical rows outside the search/screen cohort are reported separately.
    They never enlarge a previous stage or get clipped to make a funnel fit.
    A dashboard export may supply its actual routed feed. Extracted reports
    outside that feed get a separate disposition, rather than being counted as
    included or as awaiting extraction. The studio's unscoped flow is unchanged.
    """
    from .scientific import RULES_VERSION, study_identity
    data = project.data_dir
    cfg = getattr(project, "config", {}) or {}
    issues = []
    def csv_rows(name):
        p = data / name
        return _load_csv(p) if p.exists() else []
    def indexed(rows, artifact):
        result = {}
        missing_rows = []
        for row_number, row in enumerate(rows, 2):
            pid = norm_paper_id(row.get("doi") or row.get("paper_id") or
                                ("pmid_" + str(row["pmid"]) if row.get("pmid") else ""))
            if not pid:
                missing_rows.append(row_number)
            else:
                if artifact != "search_raw.csv" and pid in result and result[pid] != row:
                    issues.append({"artifact": artifact, "code": "conflicting_duplicate", "paper_id": pid})
                result[pid] = row
        if missing_rows:
            issues.append({"artifact": artifact, "code": "missing_identifier", "row_numbers": missing_rows, "reason": f"{len(missing_rows)} rows have no usable DOI/PMID identifier"})
        return result
    def yes(v): return str(v).strip().lower() in {"yes", "true", "1", "eligible", "include"}
    raw = csv_rows("search_raw.csv")
    sources = Counter((r.get("source_api") or r.get("source") or "unknown").split(":")[0] for r in raw)
    search = indexed(csv_rows("search_dois.csv"), "search_dois.csv")
    raw_ids = indexed(raw, "search_raw.csv")
    search_reconciled = bool(raw) and set(search) == set(raw_ids) and len(raw_ids) <= len(raw) and all(r.get("doi") or r.get("paper_id") or r.get("pmid") for r in raw)
    if raw and not search_reconciled:
        issues.append({"artifact": "search_raw.csv", "code": "search_identifier_mismatch", "reason": "Raw search identifiers do not reconcile with the deduplicated cohort; removed duplicate count is unknown"})
    cls = indexed(csv_rows("classifications.csv"), "classifications.csv")
    rel = indexed(csv_rows("relevance.csv"), "relevance.csv")
    scr = indexed(csv_rows("trial_screening.csv"), "trial_screening.csv")
    pass1 = indexed(_jsonl_records(data / "trial_extractions.jsonl"), "trial_extractions.jsonl")
    records = [r for name in ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl")
               for r in _jsonl_records(data / name) if not is_unpublishable_record(r)]
    final = indexed(records, "pass2")
    universe = set(search) if (data / "search_dois.csv").exists() else set(cls) | set(rel) | set(scr) | set(pass1) | set(final)
    if not search and universe:
        issues.append({"code": "missing_search_cohort", "reason": "Earlier search identifiers are unavailable"})
    for name, rows in (("classifications", cls), ("relevance", rel), ("trial_screening", scr), ("pass1", pass1), ("pass2", final)):
        orphan = set(rows) - universe
        if orphan: issues.append({"artifact": name, "code": "outside_search_cohort", "paper_ids": sorted(orphan)})
    excluded = load_exclusion_dois(data / "user_exclusions.json")
    included = load_exclusion_dois(data / "user_inclusions.json")
    overlap = included & excluded
    if overlap: issues.append({"code": "conflicting_manual_decisions", "paper_ids": sorted(overlap)})
    ta_pass, ta_excluded, ta_pending = set(), {}, set()
    for pid in universe:
        row = {}
        for tier in (rel.get(pid, {}), scr.get(pid, {}), cls.get(pid, {})):
            row.update({k:v for k,v in tier.items() if v is not None and v != ""})
        if pid in overlap: ta_pending.add(pid)
        elif pid in excluded: ta_excluded[pid] = "Manually excluded"
        elif pid in included: ta_pass.add(pid)
        elif str(row.get("is_relevant", "")).lower() in {"no", "false", "0"}:
            ta_excluded[pid] = "Not relevant to the review topic"
        elif yes(row.get("is_excluded")):
            ta_excluded[pid] = row.get("exclusion_reason") or "Excluded article type"
        elif str(row.get("is_relevant", "")).lower() in {"unknown", "unclear", "pending"}:
            ta_pending.add(pid)
        elif row.get("article_type") and row["article_type"] not in PRISMA_ELIGIBLE_TYPES:
            ta_excluded[pid] = "Excluded article type"
        elif yes(row.get("is_relevant")) or (pid in scr and str(row.get("is_excluded", "")).lower() in {"no", "false", "0"}):
            ta_pass.add(pid)
        else: ta_pending.add(pid)
    ft_pass, ft_excluded, ft_pending = set(), {}, set()
    for pid in ta_pass:
        if pid in included:
            ft_pass.add(pid)
            continue
        r = pass1.get(pid) or {}
        screening = r.get("screening") or r.get("eligibility") or {}
        verdict = screening.get("eligible_for_pass2")
        if verdict is None: verdict = screening.get("is_eligible")
        if verdict is None: verdict = screening.get("eligible")
        if r.get("_status") not in {None, "ok"}: verdict = None
        if verdict is None:
            ft_pending.add(pid)
        elif yes(verdict):
            from ber_core.stage_eligible import _component_ineligible
            reason = _component_ineligible(screening, bool(cfg.get("include_animal_studies")), bool(cfg.get("include_observational")))
            if reason: ft_excluded[pid] = reason
            else: ft_pass.add(pid)
        else: ft_excluded[pid] = screening.get("exclusion_reason") or screening.get("reason") or "Full-text eligibility exclusion"
    dup_map = load_duplicate_dois(data / "duplicate_dois.json")
    kept = drop_duplicate_records([r for pid, r in final.items() if pid not in excluded and pid not in overlap], dup_map)
    extracted_report_ids = {norm_paper_id(r.get("paper_id") or r.get("doi") or "") for r in kept}
    if final_records is not None:
        kept = final_records
    report_ids = {norm_paper_id(r.get("paper_id") or r.get("doi") or "") for r in kept}
    study_ids = {study_identity(r) for r in kept}
    unlinked = report_ids - ft_pass
    if unlinked:
        issues.append({"code": "extracted_without_confirmed_eligibility", "paper_ids": sorted(unlinked),
                       "reason": "Historical extracted reports lack a consistent preceding eligibility decision"})
    candidates = find_duplicate_candidates(kept)
    if candidates: issues.append({"code": "possible_companion_reports", "candidates": candidates})
    matched = report_ids & ft_pass
    duplicate_reports = (set(final) - extracted_report_ids - excluded - overlap) & ft_pass
    outside_dashboard_scope = (extracted_report_ids - report_ids) & ft_pass
    pending_extraction = ft_pass - report_ids - duplicate_reports - outside_dashboard_scope
    dispositions = {"screening_excluded": sorted(ta_excluded), "screening_pending": sorted(ta_pending),
                    "fulltext_excluded": sorted(ft_excluded), "fulltext_pending": sorted(ft_pending),
                    "extraction_pending": sorted(pending_extraction), "duplicate_reports": sorted(duplicate_reports), "included_reports": sorted(matched)}
    if final_records is not None:
        dispositions["outside_dashboard_scope"] = sorted(outside_dashboard_scope)
    sets = [set(v) for v in dispositions.values()]
    conserved = sum(map(len, sets)) == len(universe) and set().union(*sets) == universe
    out = {"sources": [{"name": k, "n": n} for k,n in sources.most_common()],
           "identified_total": len(raw), "after_dedup": len(search),
           "duplicates": len(raw)-len(search) if search_reconciled else None,
           "screened": len(ta_pass)+len(ta_excluded), "screening_pending": len(ta_pending),
           "screened_for_eligibility": len(ta_pass), "eligible": len(ft_pass) if pass1 else len(ta_pass),
           "title_abstract_included": len(ta_pass), "fulltext_assessed": len(ft_pass)+len(ft_excluded),
           "fulltext_eligible": len(ft_pass), "fulltext_pending": len(ft_pending),
           "fulltext_excluded": len(ft_excluded), "extraction_pending": len(pending_extraction),
           "excluded_relevance": 0, "excluded_type": 0, "excluded_screening": len(ta_excluded),
           "exclusion_reasons": dict(Counter(ta_excluded.values())),
           "fulltext_exclusion_reasons": dict(Counter(ft_excluded.values())),
           "included": len(study_ids), "included_reports": len(report_ids),
           "duplicate_reports": len(duplicate_reports),
           "included_in_flow": len(matched), "included_deduplicated": not candidates,
           "rules_version": RULES_VERSION, "dispositions": dispositions,
           "conservation": {"cohort": len(universe), "assigned": sum(map(len, sets)), "valid": conserved},
           "issues": issues, "has_fulltext_stage": bool(pass1)}
    if final_records is not None:
        out["dashboard_scope"] = {
            "records": len(final_records), "reports": len(report_ids), "studies": len(study_ids),
            "outside_scope_in_flow": len(outside_dashboard_scope),
            "reports_without_reconciled_eligibility": sorted(unlinked),
        }
    if raw and len(search) > len(raw):
        issues.append({"code": "search_count_inconsistent", "reason": "Deduplicated cohort exceeds available raw search rows; duplicate count is unknown"})
        out["duplicates"] = None
    if final: out["included_source"] = "trial_extractions_pass2.jsonl" if (data / "trial_extractions_pass2.jsonl").exists() else "observational_extractions.jsonl"
    if issues:
        out["included_warning"] = "Historical artifacts need reconciliation; see identifier-level discrepancies. Pending work is not counted as an exclusion."
    return out


def to_visualize_json(d: dict) -> dict:
    """Each branch leaves its input stage, with pending work kept explicit."""
    flow, branches, pos = [], [], {}
    def add(k, label, n, stage):
        pos[k] = len(flow)+1
        flow.append({"label": label, "n": n, "stage": stage})
    def branch(k, label, n, details=None):
        if k in pos and n:
            branches.append({"from_step": pos[k], "label": label, "n": n,
                             **({"details": details} if details else {})})
    if d.get("identified_total"): add("identified", "Records identified", d["identified_total"], "identification")
    if d.get("after_dedup"): add("dedup", "After deduplication", d["after_dedup"], "identification")
    branch("identified", "Duplicates removed", d.get("duplicates"))
    if d.get("screened"):
        add("screened", "Screened (title/abstract)", d["screened"], "screening")
        add("ta", "Passed title/abstract screening", d.get("title_abstract_included", d.get("eligible", 0)), "eligibility")
    branch("dedup", "Awaiting screening", d.get("screening_pending"))
    branch("screened", "Excluded at screening", d.get("excluded_screening"),
           [f"{k}: {n}" for k,n in (d.get("exclusion_reasons") or {}).items()])
    if d.get("has_fulltext_stage"):
        add("ft", "Full texts assessed", d.get("fulltext_assessed", 0), "eligibility")
        branch("ta", "Awaiting full-text assessment", d.get("fulltext_pending"))
        add("eligible", "Found eligible", d.get("fulltext_eligible", 0), "eligibility")
        branch("ft", "Excluded at full-text assessment", d.get("fulltext_excluded"))
        if d.get("included_source"):
            add("included", "Included reports with reconciled eligibility", d.get("included_in_flow", 0), "included")
            branch("eligible", "Awaiting extraction", d.get("extraction_pending"))
            branch("eligible", "Duplicate reports retained with linked study", d.get("duplicate_reports"))
            branch("eligible", "Extracted reports outside this dashboard's scope",
                   (d.get("dashboard_scope") or {}).get("outside_scope_in_flow"))
    elif d.get("included_source") and not d.get("issues"):
        add("included", "Included in synthesis", d.get("included", 0), "included")
    return {"main_flow": flow, "exclusions": branches, "sources": d.get("sources") or [],
            "conservation": d.get("conservation"), "issues": d.get("issues") or []}
