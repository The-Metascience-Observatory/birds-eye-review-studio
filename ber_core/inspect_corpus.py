"""Read-only reconciliation of pipeline screening and the published dashboard."""
from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path
import re
import time

from ber_core import paper_store
from ber_core.evidence import load_exclusion_dois, load_inclusion_dois, load_duplicate_dois
from ber_core.inspect_sources import digest, now, paper_key, write_json
from ber_core.stage_eligible import _component_ineligible


def read_csv(path):
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_jsonl(path):
    """Audit inputs: a malformed line is reported and skipped, not fatal."""
    from ber_core.utils import read_jsonl as _read_jsonl
    return _read_jsonl(Path(path), on_error="warn")


def by_id(rows):
    return {paper_key(r.get("paper_id") or r.get("doi")): r for r in rows}


def dashboard_records(bundle, *, source=lambda path: path):
    """Mirror the long COVID page loader, before optional browser filters.

    Preserve every report in the unfiltered dashboard, including observational
    records; the clinical/RCT distinction is made explicitly later.
    """
    bundle = Path(bundle)
    if not source(bundle / "trial_extractions.jsonl").exists():
        raise FileNotFoundError(bundle / "trial_extractions.jsonl")
    # Match the website before normalizing review identities. In particular,
    # the page trims CSV fields but compares the original DOI strings.
    excluded = {str(r.get("doi") or "").strip() for r in read_csv(source(bundle / "trial_screening.csv"))
                if str(r.get("is_excluded") or "").strip().lower() == "yes"}
    out = []
    for record in read_jsonl(source(bundle / "trial_extractions.jsonl")):
        design = record.get("study_design")
        # An empty object is truthy in the page's JavaScript loader.
        present = isinstance(design, (dict, list)) or bool(design)
        design_type = design.get("design_type", "") if isinstance(design, dict) else ""
        if (present and not re.search(r"not applicable|bioinformatic|not a clinical trial", str(design_type), re.I)
                and str(record.get("paper_id") or "").split("#")[0] not in excluded):
            out.append(record)
    return out


def collect_corpus(project, out, dashboard=None):
    data, out = project.data_dir, Path(out)
    inputs = [data / n for n in ("classifications.csv", "trial_screening.csv", "trial_extractions.jsonl",
              "trial_extractions_pass2.jsonl", "observational_extractions.jsonl", "user_inclusions.json",
              "user_exclusions.json", "duplicate_dois.json")]
    if dashboard:
        inputs += [Path(dashboard) / "trial_extractions.jsonl", Path(dashboard) / "trial_screening.csv"]
    original_inputs, frozen = [], {}
    for p in inputs:
        if not p.exists():
            continue
        for attempt in range(4):
            before = p.stat()
            raw = p.read_bytes()
            after = p.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) == (after.st_mtime_ns, after.st_size, after.st_ino):
                break
            time.sleep(.05)
        else:
            raise RuntimeError(f"Input kept changing while being copied: {p}; retry")
        sha = digest(raw)
        copy = out / "input_snapshots" / sha[:20] / p.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        if not copy.exists():
            copy.write_bytes(raw)
        frozen[str(p)] = copy
        original_inputs.append({"path": str(p.resolve()), "snapshot_path": str(copy.resolve()),
                                "sha256": sha, "modified_at": after.st_mtime,
                                "modified_at_ns": after.st_mtime_ns, "captured_at": now()})
    def source(path):
        # Missing-at-capture sources stay missing even if a worker creates them.
        return frozen.get(str(path), out / "input_snapshots" / "_absent_" / path.name)
    screen = by_id(read_csv(source(data / "trial_screening.csv")))
    classes = by_id(read_csv(source(data / "classifications.csv")))
    pass1 = by_id(read_jsonl(source(data / "trial_extractions.jsonl")))
    extracts = by_id(read_jsonl(source(data / "observational_extractions.jsonl")) +
                     read_jsonl(source(data / "trial_extractions_pass2.jsonl")))
    published_rows = dashboard_records(dashboard, source=source) if dashboard else []
    published = by_id(published_rows)
    excluded = load_exclusion_dois(source(data / "user_exclusions.json"))
    included = load_inclusion_dois(source(data / "user_inclusions.json")) - excluded
    duplicates = load_duplicate_dois(source(data / "duplicate_dois.json"))
    relevance_key = "is_" + project.slug
    positive = {pid for pid, r in screen.items()
                if str(r.get("is_excluded", "")).lower() == "no"
                and str(r.get(relevance_key, r.get("is_relevant", "yes"))).lower() == "yes"
                and str(r.get("studies_treatment", "yes")).lower() == "yes"
                and r.get("trial_type", "").lower() in {"rct", "non_randomized_trial", "crossover", "quasi-experimental"}}
    # Reviews whose merged quick screen is their sole screen remain supported.
    if not screen:
        positive = {pid for pid, r in classes.items() if r.get("is_relevant") == "yes"
                    and r.get("is_excluded") == "no" and "trial" in r.get("article_type", "").lower()}
    positive.update(pid for pid, r in pass1.items()
                    if r.get("screening", {}).get("eligible_for_pass2") is True)
    candidates = positive | set(published) | included
    index = paper_store.build_index(project.project_dir, root=paper_store.papers_root(project.project_dir, config=project.config))
    records, audit = [], []
    for pid in sorted(candidates):
        p1 = pass1.get(pid, {})
        verdict = p1.get("screening", {})
        cls = classes.get(pid, {})
        reason = None
        if pid in excluded:
            reason = "manual_exclusion"
        elif pid not in included:
            if verdict.get("eligible_for_pass2") is False:
                reason = "fulltext_ineligible"
            elif verdict.get("eligible_for_pass2") is True:
                reason = _component_ineligible(verdict, bool(project.config.get("include_animal_studies")), True)
            elif cls.get("is_excluded") == "yes":
                reason = "current_classification_exclusion"
        # A dashboard conflict remains in the assessment inventory. It does not
        # silently erase a published report or undo an upstream decision.
        state = "excluded" if reason else "candidate"
        alias = duplicates.get(pid)
        if not reason and alias in candidates and alias != pid:
            state = "duplicate_report"
        row = {"paper_id": pid, "in_dashboard": pid in published,
               "screen_positive": pid in positive, "manual_include": pid in included,
               "selection_status": state, "selection_reason": reason,
               "duplicate_of": alias if state == "duplicate_report" else None,
               "dashboard_conflict": bool(reason and pid in published)}
        audit.append(row)
        extraction = extracts.get(pid) or published.get(pid) or {}
        meta = p1.get("metadata") or {}
        title = extraction.get("title") or screen.get(pid, {}).get("paper_title") or cls.get("title") or meta.get("title") or pid
        folder = paper_store.paper_dir(project.project_dir, pid, index=index)
        sources = {"screen": screen.get(pid, {}), "classification": cls,
                   "pass1": p1, "extraction": extraction,
                   "dashboard_extraction": published.get(pid, {})}
        texts, files = {}, []
        if folder:
            # Paths originate in the one paper-store resolver, never a type bucket.
            paths = [folder / "body.md", *sorted(folder.glob("*.xml"))]
            for path in paths:
                if path.is_file():
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    if raw.strip():
                        key = "body" if path.name == "body.md" else "xml"
                        texts[key] = raw
                        files.append({"path": str(path.resolve()), "source_id": key, "sha256": digest(path.read_bytes())})
                        if key == "body":
                            break
            if not texts:
                from ber_core.utils import _paper_folder_ref, read_best_text
                ref = _paper_folder_ref(folder, "typed")
                if ref:
                    text, kind = read_best_text(ref, max_chars=500000, reject_boilerplate=True)
                    if text:
                        texts[kind] = text
                        files.append({"path": str(folder.resolve()), "source_id": kind, "sha256": digest(text)})
            prov = folder / "provenance.json"
            sources["conversion"] = json.loads(prov.read_text(encoding="utf-8")) if prov.exists() else {}
        design = (extraction.get("study_design") or {}).get("design_type") or screen.get(pid, {}).get("trial_type") or "unknown"
        record = {**row, "title": title, "design": design, "sources": sources,
                  "texts": texts, "files": files, "paper_dir": str(folder.resolve()) if folder else None}
        records.append(record)
    manifest = {"created_at": now(), "topic": project.topic_name,
                "config": project.config, "selection_unit": "publication report, not necessarily a unique trial",
                "inputs": original_inputs,
                "snapshot_method": "Each input copied while individually stable; per-file capture times retained. Live pipeline changes after capture do not modify this evidence set.",
                "counts": {"screen_positive": len(positive), "dashboard_reports": len(published),
                           "union_reports": len(records), "with_text": sum(bool(r["texts"]) for r in records),
                           **dict(Counter(r["selection_status"] for r in records))},
                "dashboard_missing_from_audit": sorted(set(published) - {r["paper_id"] for r in audit})}
    if manifest["dashboard_missing_from_audit"]:
        raise RuntimeError("Dashboard reconciliation lost reports")
    write_json(out / "manifest.json", manifest)
    write_json(out / "selection_audit.json", audit)
    for record in records:
        write_json(out / "corpus" / (digest(record["paper_id"])[:20] + ".json"), record)
    return records, manifest
