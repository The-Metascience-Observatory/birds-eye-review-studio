#!/usr/bin/env python3
"""Standalone meta-analysis report (NOT a canonical pipeline step).

The pipeline pools inside ``export_dashboard.py --meta-analysis``. Run this
script when you want the same pooled analysis written to the project's data
directory together with a human-readable report. Both call
``meta_analysis.build_meta_analysis`` over the same record set, so the numbers
cannot diverge:

* comparison selection is ``ber_core.scientific`` (validated contrasts only; no
  model chooses estimates or reconstructs missing statistics);
* duplicate publications are collapsed with the shared
  ``utils.load_duplicate_dois`` / ``utils.drop_duplicate_records``;
* manual (``user_exclusions.json``) and delivery exclusions are honoured;
* canonical intervention names (``intervention_canonical_map.json``) are the
  pooling identity;
* indication routing matches the export: ``exclude`` and prevention-only
  records are not pooled.

Each pooled group carries GRADE certainty (``certainty``) and the per-study
risk-of-bias mix (``rob_summary``). Unsupported estimates remain in the audit
output (``withheld``).

Outputs: data/meta_analysis.json and data/meta_analysis_report.md.
Usage: python scripts/pool_meta_analysis.py --config configs/config_my_topic.yaml
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core import meta_analysis as ma
from ber_core.constants import is_failed_status
from ber_core.utils import (drop_duplicate_records, is_unpublishable_record, load_config,
                   load_duplicate_dois, load_exclusion_dois, norm_paper_id, read_jsonl,
                   slugify)

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, reporting (not hiding) unparseable lines.

    A truncated write (an interrupted pipeline run) would otherwise pool a
    quietly smaller corpus, visible only as a lower k in the forest plot.
    """
    return read_jsonl(path, on_error="warn")


def _load_paper_meta(path: Path) -> dict:
    """norm_doi -> {authors, year, title} from trial_screening.csv's ``paper_*`` columns.

    Pass-2 records carry no authors/year of their own (enrich_metadata.py puts
    them on trial_screening.csv), so the forest-plot labels are joined here.
    """
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = norm_paper_id(row.get("doi") or "")
            if not doi:
                continue
            year = (row.get("paper_year") or "").strip()
            out[doi] = {
                "authors": (row.get("paper_authors") or "").strip(),
                "year": int(year) if year.isdigit() else None,
                "title": (row.get("paper_title") or "").strip(),
            }
    return out


# Same file format and loader as the export (utils.load_exclusion_dois), so a
# bare {doi: reason} mapping excludes a paper from both or neither.
_load_delivery_exclusions = load_exclusion_dois


def _load_indications(path: Path) -> dict[str, str]:
    """norm paper_id -> indication from classify_intervention_indication.py."""
    if not path.exists():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    out = {}
    for k, v in (m.items() if isinstance(m, dict) else []):
        ind = v.get("indication") if isinstance(v, dict) else v
        if isinstance(ind, str) and ind:
            out[norm_paper_id(str(k))] = ind
    return out


def _load_verdicts(path: Path) -> dict:
    """base paper_id -> verdict label from trial_verdicts.csv."""
    out = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = (row.get("paper_id") or "").strip()
            v = (row.get("verdict") or "").strip()
            if pid and v:
                out[pid.split("#")[0]] = v
    return out


def pool_records(data: Path, pass2: Path, observational: Path) -> list[dict]:
    """The record set the export pools, built from the project's data dir."""
    records, seen = [], set()
    for r in _read_jsonl(pass2) + _read_jsonl(observational):
        # Pass-2 is authoritative on a paper_id collision (as in the export).
        if is_failed_status(r.get("_status")) or is_unpublishable_record(r):
            continue
        pid = norm_paper_id(r.get("paper_id"))
        if pid in seen:
            continue
        seen.add(pid)
        records.append(r)

    excluded = (_load_delivery_exclusions(data / "delivery_exclusions.json")
                | load_exclusion_dois(data / "user_exclusions.json"))
    records = [r for r in records if norm_paper_id(r.get("paper_id")) not in excluded]
    # Shared with the export and PRISMA: a duplicate publication is dropped only
    # when its canonical counterpart is actually present.
    records = drop_duplicate_records(records, load_duplicate_dois(data / "duplicate_dois.json"))

    indication = _load_indications(data / "intervention_indication_map.json")
    if indication:
        records = [r for r in records
                   if indication.get(norm_paper_id(r.get("paper_id"))) not in {"exclude", "prevention"}]

    ma.attach_canonical_interventions(
        records,
        ma.load_name_map(data / "intervention_canonical_map.json"),
        ma.load_name_map(data / "intervention_category_map.json"))

    paper_meta = _load_paper_meta(data / "trial_screening.csv")
    return [{**r, **(paper_meta.get(norm_paper_id(r.get("paper_id") or "")) or {})}
            for r in records]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--pass2", help="override RCT extraction jsonl")
    ap.add_argument("--observational", help="override observational jsonl")
    ap.add_argument("--out", help="override output meta_analysis.json path")
    ap.add_argument("--generated-at", help="ISO timestamp to stamp into the output")
    args = ap.parse_args()

    config = load_config(args.config)
    macfg = config.get("meta_analysis") or {}
    slug = slugify((config.get("topic") or {}).get("name", "default"))
    data = PROJECTS_DIR / slug / "data"

    pass2 = Path(args.pass2) if args.pass2 else data / "trial_extractions_pass2.jsonl"
    obs = Path(args.observational) if args.observational else data / "observational_extractions.jsonl"
    out_path = Path(args.out) if args.out else data / "meta_analysis.json"

    records = pool_records(data, pass2, obs)
    print(f"Pooling over {len(records)} records", file=sys.stderr)

    options = ma.MetaOptions(**{k: v for k, v in macfg.items()
                                if k in ma.MetaOptions.__dataclass_fields__})
    result = ma.build_meta_analysis(records, None, options, policy=macfg)
    result["generated_at"] = args.generated_at
    groups = result["groups"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pooled = [g for g in groups if g.get("pooled")]
    print(f"Meta-analysis: {len(groups)} groups "
          f"({sum(1 for g in groups if g['design_class'] == 'rct')} RCT, "
          f"{sum(1 for g in groups if g['design_class'] == 'observational')} obs); "
          f"{len(pooled)} pooled → {out_path}", file=sys.stderr)

    if macfg.get("emit_report", True):
        verdicts = _load_verdicts(data / "trial_verdicts.csv")
        report = _render_report(result, verdicts)
        (out_path.parent / "meta_analysis_report.md").write_text(report, encoding="utf-8")
        print(f"Report → {out_path.parent / 'meta_analysis_report.md'}", file=sys.stderr)


def _render_report(result: dict, verdicts: dict) -> str:
    m = result["methods"]
    lines = ["# Meta-analysis report", "", f"Scientific rules: {result['rules_version']}",
             f"Model: {m['model']}; tau²: {m['tau2_method']}; CI: {m['ci_method']}; "
             f"minimum independent studies: {result['min_trials']}.",
             "Contrasts retain intervention-versus-comparator orientation. Clinical direction is outcome-specific.",
             "Certainty is GRADE-style: randomized evidence starts High, non-randomized Low; "
             "downgraded for risk of bias, inconsistency, imprecision and publication bias.", "",
             f"| Intervention | Outcome / comparator / assessment | Measure | k | "
             f"Estimate ({100 * m['level']:g}% CI) | Certainty | Flags |",
             "|---|---|---|---|---|---|---|"]
    for g in result["groups"]:
        p = g.get("pooled")
        estimate = f"{p['effect']:.3g} ({p['ci_low']:.3g}, {p['ci_high']:.3g})" if p else "Not pooled"
        cert = (g.get("certainty") or {}).get("grade", "—")
        flags = _verdict_flags(g, verdicts)
        lines.append(f"| {g['ingredient']} | {g['domainLabel']} | {g['effect_measure']} ({g.get('unit')}) | "
                     f"{g['n_trials']} | {estimate} | {cert} | {'; '.join(flags) or '—'} |")
        downs = [d["reason"] for d in (g.get("certainty") or {}).get("downgrades") or []]
        if downs:
            lines.append(f"\nDowngraded: {'; '.join(downs)}.")
        if g.get("ci_sensitivity"):
            lines.append(f"\n{g['small_study_note']} Method-dependent conclusion: {g['method_dependent']}.")
            for ci, p in g["ci_sensitivity"].items():
                lines.append(f"- {ci}: [{p['ci_low']:.3g}, {p['ci_high']:.3g}]")
    lines.extend(["", "## Withheld estimates", ""])
    for s in result["withheld"]:
        lines.append(f"- {s['paper_id']}: {s['outcome_name']}, {s['intervention']} vs {s['comparator']}: "
                     + "; ".join(i["reason"] for i in s["issues"]))
    return "\n".join(lines) + "\n"


def _verdict_flags(g: dict, verdicts: dict) -> list[str]:
    """Pooled direction vs the per-trial verdicts, where clinical direction is known.

    The verdict map is keyed on the bare DOI; arm-split records carry
    ``<doi>#<arm>``, hence the split.
    """
    p = g.get("pooled") or {}
    hib = g.get("reference_higher_is_better")
    if not p or not isinstance(hib, bool):
        return []
    null = 1.0 if g.get("scale") == "log" else 0.0
    if p["ci_low"] <= null <= p["ci_high"]:
        return []
    favors_treatment = (p["effect"] > null) == hib
    vs = [verdicts.get(str(t["paper_id"]).split("#")[0]) for t in g["trials"]]
    favt = sum(1 for v in vs if v == "favors_treatment")
    favc = sum(1 for v in vs if v == "favors_control")
    if favors_treatment and favc > favt:
        return ["pooled favors treatment but trial verdicts favor control"]
    if not favors_treatment and favt > favc:
        return ["pooled favors control but trial verdicts favor treatment"]
    return []


if __name__ == "__main__":
    main()
