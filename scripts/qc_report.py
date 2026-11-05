#!/usr/bin/env python3
"""QC metrics and affiliation analysis for pass-2 extraction results.

Subcommands:
    python scripts/qc_report.py metrics extractions.jsonl        # QC metrics + CSV
    python scripts/qc_report.py affiliations extractions.jsonl   # Institution/funder analysis

Duplicate publications are handled by ``duplicate_dois.json`` (honoured by the
export, the meta-analysis and the PRISMA funnel through
``utils.drop_duplicate_records``), not here. The old ``dedup`` subcommand read
fields pass-2 records do not carry and wrote a ``*_deduped.jsonl`` nothing
consumed, so it was removed.
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.utils import load_config, read_jsonl, slugify

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

# Domains each risk-of-bias tool rates (pass-2 prompts: RoB 2 for RCTs,
# ROBINS-I for observational studies).
ROB_TOOL_DOMAINS = {"rob2": 5, "robins-i": 7}


def _resolve_input(args) -> Path:
    """Resolve the input JSONL path, honouring --config defaults."""
    if args.input:
        return Path(args.input)
    if not args.config:
        print("ERROR: must provide either an input JSONL path or --config",
              file=sys.stderr)
        sys.exit(2)
    cfg = load_config(args.config)
    slug = slugify(cfg.get("topic", {}).get("name", "default"))
    project_dir = PROJECTS_DIR / slug
    candidate = project_dir / "data" / "trial_extractions_pass2.jsonl"
    if not candidate.exists():
        print(f"ERROR: default input not found: {candidate}", file=sys.stderr)
        sys.exit(2)
    print(f"Project: {cfg.get('topic', {}).get('name')!r} -> {project_dir}",
          file=sys.stderr)
    return candidate


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_records(path: Path) -> list[dict]:
    """Load all records from a JSONL file (malformed lines warned and skipped)."""
    return read_jsonl(path, on_error="warn")


# ---------------------------------------------------------------------------
# Per-paper QC metrics (from qc_pass2.py)
# ---------------------------------------------------------------------------

def count_rob_domains(dj) -> int:
    """Number of rated domains in a pass-2 ``domain_judgments`` value."""
    if isinstance(dj, dict):
        return sum(1 for v in dj.values() if str(v or "").strip())
    if isinstance(dj, list):
        return sum(1 for v in dj if v)
    return sum(1 for part in str(dj or "").split(",") if ":" in part and part.split(":", 1)[1].strip())


def paper_metrics(r: dict) -> dict:
    """Compute QC metrics for a single paper."""
    pid = r.get("paper_id", "?")
    # `or {}` / `or []`, not a .get default: the extractor writes explicit nulls.
    sd = r.get("study_design") or {}
    arms = [a for a in sd.get("arms") or [] if isinstance(a, dict)]
    ss = r.get("sample_sizes") or {}
    outcomes = [o for o in r.get("outcomes") or [] if isinstance(o, dict)]
    rob = r.get("risk_of_bias") or {}
    ae = r.get("adverse_events") or {}
    fu = r.get("follow_up") or {}
    warns = [str(w) for w in r.get("_warnings") or []]

    # Intervention info (first non-control arm)
    interv_name = ""
    interv_cat = ""
    for a in arms:
        if a.get("type") in ("intervention", "experimental"):
            interv_name = a.get("intervention_name") or a.get("label") or ""
            interv_cat = a.get("intervention_category") or ""
            break

    # Primary outcome
    primary_name = ""
    primary_instrument = ""
    primary_domain = ""
    primary_effect = None
    primary_ci_low = None
    primary_ci_high = None
    primary_p = None
    primary_measure = ""
    for o in outcomes:
        if o.get("is_primary"):
            primary_name = (o.get("name") or "")[:60]
            primary_instrument = o.get("measurement_instrument") or ""
            primary_domain = (o.get("outcome_domain") or o.get("symptom_domain")
                              or o.get("category") or "")
            bgs = [e for e in o.get("between_group_effects") or [] if isinstance(e, dict)]
            if bgs:
                bg = bgs[0]
                primary_effect = bg.get("effect_value")
                primary_ci_low = bg.get("ci_95_low")
                primary_ci_high = bg.get("ci_95_high")
                primary_p = bg.get("p_value")
                primary_measure = bg.get("effect_measure") or ""
            break

    # Outcome counts
    n_outcomes = len(outcomes)
    n_primary = sum(1 for o in outcomes if o.get("is_primary"))
    n_continuous = sum(1 for o in outcomes if o.get("is_continuous"))
    n_binary = sum(1 for o in outcomes if o.get("is_binary"))

    # Effect completeness
    n_bg_effects = 0
    n_bg_with_ci = 0
    n_arm_results = 0
    n_arm_mean_sd = 0
    n_arm_mean_no_sd = 0
    n_arm_binary = 0
    for o in outcomes:
        for bg in o.get("between_group_effects") or []:
            n_bg_effects += 1
            if bg.get("effect_value") is not None and bg.get("ci_95_low") is not None:
                n_bg_with_ci += 1
        for ar in o.get("arm_results") or []:
            n_arm_results += 1
            has_mean = ar.get("mean") is not None
            has_sd = ar.get("sd") is not None
            has_events = ar.get("events") is not None
            if has_mean and has_sd:
                n_arm_mean_sd += 1
            elif has_mean:
                n_arm_mean_no_sd += 1
            elif has_events:
                n_arm_binary += 1

    # RoB: the pass-2 schema's field is `domain_judgments` (a
    # "domain:judgment, ..." string; models sometimes return an object/list).
    # Reading a non-existent `domains` key flagged every paper ROB_INCOMPLETE(0/5).
    rob_overall = rob.get("overall_judgment") or ""
    rob_domains_filled = count_rob_domains(rob.get("domain_judgments"))
    rob_domains_expected = ROB_TOOL_DOMAINS.get(str(rob.get("tool") or "").strip().lower(), 5)

    # Adverse events (pass-2 `adverse_events` block).
    ae_arms = [a for a in ae.get("per_arm") or [] if isinstance(a, dict)]
    n_ae_arms_any = sum(1 for a in ae_arms if a.get("n_any_ae") is not None)
    n_ae_arms_serious = sum(1 for a in ae_arms if a.get("n_serious_ae") is not None)

    # Quality flags
    flags = []
    if n_outcomes == 0:
        flags.append("NO_OUTCOMES")
    if n_primary == 0:
        flags.append("NO_PRIMARY")
    if n_bg_effects == 0:
        flags.append("NO_EFFECTS")
    if primary_effect is None and n_primary > 0:
        flags.append("PRIMARY_NO_EFFECT")
    if rob_domains_filled < rob_domains_expected:
        flags.append(f"ROB_INCOMPLETE({rob_domains_filled}/{rob_domains_expected})")
    if (ae.get("reporting_quality") or "not_reported") == "not_reported":
        flags.append("AE_NOT_REPORTED")
    if any("orphan_arm_id" in w for w in warns):
        flags.append("ORPHAN_ARM_IDS")
    if any("mismatch" in w for w in warns):
        flags.append("N_MISMATCH")
    if any("pdf_fallback" in w for w in warns):
        flags.append("PDF_FALLBACK")

    # Meta-analysis readiness: has at least one outcome with per-arm mean+SD OR
    # a between-group effect with CI
    ma_ready = n_arm_mean_sd >= 2 or n_bg_with_ci >= 1

    return {
        "paper_id": pid,
        "design": sd.get("design_type", "?"),
        "n_arms": sd.get("num_arms") or len(arms),
        "n_randomized": ss.get("n_randomized_total"),
        "intervention": interv_name[:40],
        "intervention_category": interv_cat,
        "blinding": sd.get("blinding", ""),
        "n_outcomes": n_outcomes,
        "n_primary": n_primary,
        "n_continuous": n_continuous,
        "n_binary": n_binary,
        "n_bg_effects": n_bg_effects,
        "n_bg_with_ci": n_bg_with_ci,
        "n_arm_results": n_arm_results,
        "n_arm_mean_sd": n_arm_mean_sd,
        "n_arm_mean_no_sd": n_arm_mean_no_sd,
        "n_arm_binary": n_arm_binary,
        "primary_outcome": primary_name,
        "primary_instrument": primary_instrument[:40],
        "primary_domain": primary_domain,
        "primary_measure": primary_measure,
        "primary_effect": primary_effect,
        "primary_ci_low": primary_ci_low,
        "primary_ci_high": primary_ci_high,
        "primary_p": primary_p,
        "rob_overall": rob_overall,
        "rob_domains": rob_domains_filled,
        "ae_reporting": ae.get("reporting_quality") or "",
        "n_ae_arms_any": n_ae_arms_any,
        "n_ae_arms_serious": n_ae_arms_serious,
        "follow_up_weeks": fu.get("total_duration_weeks"),
        "treatment_weeks": fu.get("treatment_duration_weeks"),
        "ma_ready": ma_ready,
        "flags": "|".join(flags) if flags else "",
        "n_warnings": len(warns),
        "model": r.get("_model", ""),
        "cost": r.get("_cost", 0),
        "elapsed": r.get("_elapsed", 0),
    }


# ---------------------------------------------------------------------------
# Terminal report (from qc_pass2.py)
# ---------------------------------------------------------------------------

def print_report(metrics: list[dict]):
    n = len(metrics)
    print(f"\n{'='*70}")
    print(f"  QC SUMMARY — {n} papers")
    print(f"{'='*70}\n")
    if not n:
        print("  (no records)")
        return

    # Design types
    designs = Counter(m["design"] for m in metrics)
    print("STUDY DESIGNS:")
    for d, c in designs.most_common():
        print(f"  {d or '(unknown)':<25s} {c:>4d}")

    # Blinding
    blindings = Counter(m["blinding"] for m in metrics)
    print("\nBLINDING:")
    for b, c in blindings.most_common():
        print(f"  {str(b or '(not reported)'):<25s} {c:>4d}")

    # Sample sizes
    ns = [m["n_randomized"] for m in metrics if m["n_randomized"]]
    if ns:
        ns.sort()
        print("\nSAMPLE SIZES (n_randomized):")
        print(f"  n={len(ns)}, median={ns[len(ns)//2]}, "
              f"IQR=[{ns[len(ns)//4]}-{ns[3*len(ns)//4]}], "
              f"min={min(ns)}, max={max(ns)}")

    # Outcome stats
    total_out = sum(m["n_outcomes"] for m in metrics)
    total_fx = sum(m["n_bg_effects"] for m in metrics)
    total_fx_ci = sum(m["n_bg_with_ci"] for m in metrics)
    total_arm_ms = sum(m["n_arm_mean_sd"] for m in metrics)
    print("\nOUTCOMES & EFFECTS:")
    print(f"  Total outcomes:              {total_out:>5d}  ({total_out/n:.1f}/paper)")
    print(f"  Between-group effects:       {total_fx:>5d}  ({total_fx/n:.1f}/paper)")
    print(f"  Effects with CI:             {total_fx_ci:>5d}  ({100*total_fx_ci/max(total_fx,1):.0f}%)")
    print(f"  Arm results with mean+SD:    {total_arm_ms:>5d}")

    # MA readiness
    ready = sum(1 for m in metrics if m["ma_ready"])
    print("\nMETA-ANALYSIS READINESS:")
    print(f"  Papers with usable effect data: {ready}/{n} ({100*ready/n:.0f}%)")

    # Adverse events
    ae_counts = Counter(m["ae_reporting"] or "(missing)" for m in metrics)
    print("\nADVERSE-EVENT REPORTING:")
    for k, c in ae_counts.most_common():
        print(f"  {str(k):<25s} {c:>4d}")

    # RoB
    rob_counts = Counter(m["rob_overall"] for m in metrics)
    print("\nRISK OF BIAS (overall):")
    for r, c in rob_counts.most_common():
        print(f"  {str(r or '(missing)'):<25s} {c:>4d}")

    # Quality flags
    all_flags = []
    for m in metrics:
        if m["flags"]:
            all_flags.extend(m["flags"].split("|"))
    flag_counts = Counter(all_flags)
    print("\nQUALITY FLAGS:")
    if flag_counts:
        for f, c in flag_counts.most_common():
            print(f"  {str(f):<30s} {c:>4d}")
    else:
        print("  (none)")

    # Papers with flags
    flagged = [m for m in metrics if m["flags"]]
    if flagged:
        print(f"\nFLAGGED PAPERS ({len(flagged)}):")
        for m in flagged:
            print(f"  {str(m['paper_id']):<50s}  {m['flags']}")

    # Intervention categories
    cat_papers = defaultdict(list)
    for m in metrics:
        cat = m["intervention_category"] or "uncategorized"
        cat_papers[cat].append(m)

    print("\nINTERVENTION CATEGORIES (for meta-analysis grouping):")
    print(f"  {'Category':<25s} {'Papers':>6s} {'MA-ready':>8s} {'Median N':>8s} {'Domains'}")
    print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*8} {'-'*30}")
    for cat in sorted(cat_papers, key=lambda c: -len(cat_papers[c])):
        papers = cat_papers[cat]
        ready_count = sum(1 for m in papers if m["ma_ready"])
        ns_cat = sorted(m["n_randomized"] for m in papers if m["n_randomized"])
        median_n = ns_cat[len(ns_cat)//2] if ns_cat else 0
        # Symptom domains covered
        domains = Counter()
        for m in papers:
            if m["primary_domain"]:
                domains[m["primary_domain"]] += 1
        top_domains = ", ".join(f"{d}({c})" for d, c in domains.most_common(3))
        print(f"  {str(cat):<25s} {len(papers):>6d} {ready_count:>8d} {median_n:>8d} {top_domains}")

    # Primary outcome domains
    domain_counts = Counter(m["primary_domain"] for m in metrics if m["primary_domain"])
    print("\nPRIMARY OUTCOME DOMAINS:")
    for d, c in domain_counts.most_common():
        print(f"  {str(d):<25s} {c:>4d}")

    # Cost summary
    total_cost = sum(m["cost"] for m in metrics)
    total_time = sum(m["elapsed"] for m in metrics)
    print(f"\nCOST: ${total_cost:.2f}  Time: {total_time/60:.0f} min")
    print()


# ---------------------------------------------------------------------------
# Country / institution / funder normalization (from analyze_extractions.py)
# ---------------------------------------------------------------------------

_COUNTRY_ALIASES = {
    "United States": "USA", "United States of America": "USA", "US": "USA",
    "United Kingdom": "UK", "England": "UK", "Scotland": "UK", "Wales": "UK",
    "Republic of Korea": "South Korea", "Korea": "South Korea",
    "People's Republic of China": "China", "PRC": "China",
    "Russian Federation": "Russia",
    "The Netherlands": "Netherlands",
    "Czech Republic": "Czechia",
    "Islamic Republic of Iran": "Iran",
}

_ORG_KEYWORD_RE = re.compile(
    r"\b(Universit(?:y|e|a|at|eit|et|ade|dad|as)|Universidad|Universite|Universita|Universitat|Universidade|"
    r"Hospital|Hopital|Medical Cent(?:er|re)|"
    r"Institu(?:t[eo]?|te)|Instytut|"
    r"Foundat(?:ion|zione|cion)|Fondation|"
    r"Academ(?:y|ia)|"
    r"Karolinska|Charite|Mayo Clinic|"
    r"(?:Ltd|Inc|GmbH|S\.?A\.?|Co\.))\b",
    re.IGNORECASE,
)


def _normalize_institution(raw: str) -> str:
    """Extract the core org name (university/hospital/institute) from a full affiliation string."""
    s = raw.strip()
    if not s:
        return s

    # Split on commas, scan from RIGHT to LEFT for the first segment with an org keyword.
    segments = [seg.strip() for seg in s.split(",")]

    org = None
    for i in range(len(segments) - 1, -1, -1):
        seg = segments[i]
        m = _ORG_KEYWORD_RE.search(seg)
        if m:
            kw = m.group(1)
            if re.match(r"(?:Ltd|Inc|GmbH|S\.?A\.?|Co\.?)$", kw, re.IGNORECASE):
                continue
            org = ", ".join(segments[i:]).strip(" ,")
            break

    if org is None:
        m = re.search(
            r"((?:\w+\s+){0,5}"
            r"(?:Universit(?:y|e|a|at|eit|et|ade|dad|as)|Universidad|Universite|Universita|Universitat|Universidade|Hospital|Medical Cent(?:er|re)|"
            r"Institu(?:t[eo]?|te))"
            r"(?:\s+\w+){0,6})",
            s,
        )
        org = m.group(1).strip() if m else s

    org = re.sub(r"\s*\([^)]*\)\s*$", "", org).strip(" ,")

    if re.search(r"\b(?:universit|universidad|universite|universita|universitat|universidade)", org, re.IGNORECASE):
        org = re.sub(r",?\s*(?:School|Faculty|College)\s+of\s+Medicine\b", "", org).strip(" ,")
        org = re.sub(r",?\s*Medical\s+School\b", "", org).strip(" ,")
        org = re.sub(r"\s+and\s+\S+.*(?:Hospital|Medical Cent(?:er|re))\b.*$", "", org).strip(" ,")

    return org


def _normalize_country(raw: str) -> str:
    s = raw.strip().strip(".")
    return _COUNTRY_ALIASES.get(s, s)


_FUNDER_ALIASES = {
    "NIH": "National Institutes of Health",
    "National Institute of Health": "National Institutes of Health",
    "National Center for Advancing Translational Sciences, National Institutes of Health":
        "National Institutes of Health (NCATS)",
    "National Center for Research Resources": "National Institutes of Health (NCRR)",
    "NIHR": "National Institute for Health and Care Research (NIHR)",
    "National Institute for Health Research": "National Institute for Health and Care Research (NIHR)",
    "National Institute for Health Research (NIHR)": "National Institute for Health and Care Research (NIHR)",
    "BMBF": "German Federal Ministry of Education and Research (BMBF)",
    "DFG": "German Research Foundation (DFG)",
    "German Research Foundation": "German Research Foundation (DFG)",
    "European Commission": "European Union",
    "European Union Horizon 2020": "European Union",
    "EU Horizon 2020": "European Union",
}


def _normalize_funder(raw: str) -> str:
    s = raw.strip()
    return _FUNDER_ALIASES.get(s, s)


def count_affiliations(records: list[dict]) -> tuple[Counter, Counter, Counter]:
    """Return counters for (institution, country, institution_type)."""
    institutions: Counter = Counter()
    countries: Counter = Counter()
    inst_types: Counter = Counter()
    for rec in records:
        seen_inst: set[str] = set()
        for author in (rec.get("metadata") or {}).get("authors") or []:
            for aff in author.get("affiliations") or []:
                inst = _normalize_institution(aff.get("institution") or "")
                country = _normalize_country(aff.get("country") or "")
                itype = (aff.get("type") or "").strip()
                if inst and inst not in seen_inst:
                    institutions[inst] += 1
                    seen_inst.add(inst)
                if country:
                    countries[country] += 1
                if itype:
                    inst_types[itype] += 1
    return institutions, countries, inst_types


def count_funders(records: list[dict]) -> tuple[Counter, Counter, int]:
    """Return counters for (funder_name, funder_type) and count of unfunded papers."""
    funders: Counter = Counter()
    funder_types: Counter = Counter()
    unfunded = 0
    for rec in records:
        funding = (rec.get("metadata") or {}).get("funding") or []
        if not funding:
            unfunded += 1
            continue
        seen: set[str] = set()
        for f in funding:
            name = _normalize_funder(f.get("funder") or "")
            ftype = (f.get("funder_type") or "").strip()
            if name and name not in seen:
                funders[name] += 1
                seen.add(name)
            if ftype:
                funder_types[ftype] += 1
    return funders, funder_types, unfunded


def print_counter(title: str, counter: Counter, top_n: int = 30) -> None:
    print(f"\n{'=' * 70}")
    print(f" {title} (top {top_n})")
    print(f"{'=' * 70}")
    for i, (name, count) in enumerate(counter.most_common(top_n), 1):
        print(f"  {i:3d}. {count:4d}  {name}")
    total_unique = len(counter)
    total_mentions = sum(counter.values())
    print(f"  --- {total_unique} unique / {total_mentions} total mentions ---")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_metrics(args):
    """QC metrics and quality flags."""
    input_path = _resolve_input(args)
    records = load_records(input_path)
    print(f"Loaded {len(records)} records from {input_path}", file=sys.stderr)

    metrics = [paper_metrics(r) for r in records]

    # Terminal report
    print_report(metrics)

    # CSV output
    if not metrics:
        print("No records; no CSV written.", file=sys.stderr)
    elif not args.no_csv:
        csv_path = Path(args.csv) if args.csv else input_path.with_suffix("").with_name(
            input_path.stem + "_qc.csv")
        fieldnames = list(metrics[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(metrics)
        print(f"CSV written: {csv_path} ({len(metrics)} rows)", file=sys.stderr)


def cmd_affiliations(args):
    """Institution and funder analysis."""
    input_path = _resolve_input(args)
    records = load_records(input_path)
    print(f"Loaded {len(records)} records from {input_path}")

    # Affiliations
    institutions, countries, inst_types = count_affiliations(records)
    print_counter("INSTITUTIONS (papers with at least one author from)", institutions, top_n=args.top)
    print_counter("COUNTRIES (author-level mentions)", countries, top_n=args.top)
    print_counter("INSTITUTION TYPES", inst_types, top_n=10)

    # Funders
    funders, funder_types, unfunded = count_funders(records)
    print(f"\n  Papers with no funding listed: {unfunded}/{len(records)} "
          f"({100*unfunded/max(len(records), 1):.0f}%)")
    print_counter("FUNDERS (papers funded by)", funders, top_n=args.top)
    print_counter("FUNDER TYPES", funder_types, top_n=10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="QC metrics and affiliation analysis")
    subparsers = parser.add_subparsers(dest="command")

    def _add_common(sp):
        sp.add_argument("input", nargs="?", default=None,
                        help="Input JSONL file (default: derived from --config)")
        sp.add_argument("--config", default=None,
                        help="Path to config.yaml. When provided and input is omitted, "
                             "uses projects/{slug}/data/trial_extractions_pass2.jsonl.")

    p_metrics = subparsers.add_parser("metrics", help="QC metrics + CSV")
    _add_common(p_metrics)
    p_metrics.add_argument("--csv", default=None,
                           help="Output CSV path (default: <input_stem>_qc.csv)")
    p_metrics.add_argument("--no-csv", action="store_true",
                           help="Skip CSV output, just print terminal report")

    p_aff = subparsers.add_parser("affiliations", help="Institution/funder analysis")
    _add_common(p_aff)
    p_aff.add_argument("--top", type=int, default=30,
                        help="Show top N entries (default: 30)")

    args = parser.parse_args()
    if args.command == "metrics":
        cmd_metrics(args)
    elif args.command == "affiliations":
        cmd_affiliations(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
