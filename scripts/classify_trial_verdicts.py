#!/usr/bin/env python3
"""Classify each extracted human trial's overall result (direction of effect).

Reads BOTH extraction feeds by default (``trial_extractions_pass2.jsonl`` +
``observational_extractions.jsonl``) and, for
every trial that has extracted outcomes, asks Claude to judge the trial on its
PRIMARY efficacy outcome and emit one verdict label:

    favors_treatment | favors_control | no_difference | mixed | inconclusive

This is a HYBRID pass:
  * The LLM decides DIRECTION (it reads the prose summary + the structured
    primary-outcome stats; arm orientation lives in the prose, not in the
    machine-readable ``comparison`` field, so direction can't be computed
    reliably without it).
  * We compute STATISTICAL SIGNIFICANCE of the primary effect deterministically
    (CI excludes the null, or p < 0.05 — orientation-independent) and use it as
    a guardrail: a non-significant primary downgrades a favors_* verdict to
    no_difference, and a significant primary with an LLM null verdict is flagged
    for review.

Output: ``projects/{slug}/data/trial_verdicts.csv`` with one row per trial.
Resumable: trials already present in the output are skipped.

    python3 scripts/classify_trial_verdicts.py --config configs/config_my_topic.yaml
    python3 scripts/classify_trial_verdicts.py --config configs/config_my_topic.yaml --limit 5   # smoke test
    python3 scripts/classify_trial_verdicts.py --config configs/config_my_topic.yaml --model haiku
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.batch_classify import call_json
from ber_core.scientific import RATIOS, measure
from ber_core.claude_cli import YELLOW, RESET
from ber_core.constants import is_failed_status
from ber_core.utils import load_config, load_prompt_template, project_paths, read_jsonl, write_csv_atomic

VALID_VERDICTS = {
    "favors_treatment", "favors_control", "no_difference", "mixed", "inconclusive",
}


FIELDNAMES = [
    "paper_id", "verdict", "llm_verdict", "stat_significant",
    "primary_outcome", "primary_measure", "primary_value", "primary_ci",
    "primary_p", "direction_basis", "confidence", "rationale", "override_note",
]


# --------------------------------------------------------------------------- #
# Building the trial block fed to the model
# --------------------------------------------------------------------------- #
def _primary_outcomes(rec: dict) -> list[dict]:
    outs = rec.get("outcomes") or []
    prim = [o for o in outs if o.get("is_primary")]
    return prim or (outs[:1] if outs else [])


def _pick_effect(outcome: dict) -> dict | None:
    """Pick the most informative between-group effect for an outcome: prefer one
    with a CI, then a p-value, then any with a numeric effect."""
    effs = outcome.get("between_group_effects") or []
    if not effs:
        return None
    with_ci = [e for e in effs if e.get("ci_95_low") is not None and e.get("ci_95_high") is not None]
    with_p = [e for e in effs if e.get("p_value") is not None]
    with_val = [e for e in effs if e.get("effect_value") is not None]
    return (with_ci or with_p or with_val or effs)[0]


def _fmt_effect(e: dict | None) -> str:
    if not e:
        return "no between-group effect reported"
    parts = [f"{e.get('effect_measure', '?')} = {e.get('effect_value')}"]
    lo, hi = e.get("ci_95_low"), e.get("ci_95_high")
    if lo is not None and hi is not None:
        parts.append(f"95% CI {lo}–{hi}")
    if e.get("p_value") is not None:
        parts.append(f"p={e.get('p_value')}")
    if e.get("comparison"):
        parts.append(f"comparison={e.get('comparison')}")
    if e.get("adjusted"):
        parts.append("adjusted")
    return ", ".join(parts)


def build_trial_block(rec: dict) -> str:
    lines: list[str] = []
    summary = (rec.get("summary") or "").strip()
    lines.append("SUMMARY:")
    lines.append(summary or "(no summary)")

    sd = rec.get("study_design") or {}
    design = sd.get("design_type") or ("RCT" if rec.get("is_rct") else "unknown")
    agent = rec.get("agent_canonical") or rec.get("spray_type") or "?"
    lines.append("")
    lines.append(f"DESIGN: {design}; intervention: {agent}")

    prim = _primary_outcomes(rec)
    lines.append("")
    lines.append("PRIMARY OUTCOME(S):")
    if not prim:
        lines.append("  (none flagged)")
    for o in prim:
        dom = o.get("symptom_domain") or o.get("outcome_domain") or o.get("category") or ""
        lines.append(
            f"  - {o.get('name', '?')} (domain={dom}, "
            f"higher_is_better={o.get('higher_is_better')})"
        )
        lines.append(f"      effect: {_fmt_effect(_pick_effect(o))}")

    # A few secondary outcome names for context only.
    sec = [o.get("name") for o in (rec.get("outcomes") or [])
           if not o.get("is_primary") and o.get("name")]
    if sec:
        lines.append("")
        lines.append("OTHER OUTCOMES (context only, not the basis for the verdict): "
                     + "; ".join(sec[:8]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Deterministic significance guardrail (orientation-independent)
# --------------------------------------------------------------------------- #
def primary_significance(rec: dict) -> tuple[bool | None, dict]:
    """Return (significant?, effect_meta) for the trial's primary outcome.

    significant is True/False from the first primary effect that carries a CI or
    a p-value, or None when neither is available (then the LLM stands alone)."""
    meta = {"outcome": "", "measure": "", "value": "", "ci": "", "p": ""}
    for o in _primary_outcomes(rec):
        e = _pick_effect(o)
        if not e:
            continue
        meta["outcome"] = o.get("name", "")
        meta["measure"] = e.get("effect_measure", "")
        meta["value"] = "" if e.get("effect_value") is None else str(e.get("effect_value"))
        lo, hi = e.get("ci_95_low"), e.get("ci_95_high")
        p = e.get("p_value")
        if lo is not None and hi is not None:
            meta["ci"] = f"{lo}–{hi}"
            # Ratio measures (incl. adjusted_*) take a null of 1; differences/SMD 0.
            null = 1.0 if measure(e.get("effect_measure")) in RATIOS else 0.0
            return (not (min(lo, hi) <= null <= max(lo, hi))), meta
        if p is not None:
            meta["p"] = str(p)
            try:
                return (float(p) < 0.05), meta
            except (TypeError, ValueError):
                return None, meta
    return None, meta


def reconcile(llm_verdict: str, significant: bool | None) -> tuple[str, str]:
    """Apply the significance guardrail. Returns (final_verdict, note)."""
    if significant is False and llm_verdict in ("favors_treatment", "favors_control"):
        return "no_difference", "downgraded: primary CI/p non-significant"
    if significant is True and llm_verdict in ("no_difference", "inconclusive"):
        return llm_verdict, "flag: primary stat-significant but LLM judged null — review"
    return llm_verdict, ""


# --------------------------------------------------------------------------- #
# Per-trial classification
# --------------------------------------------------------------------------- #
def classify_one(rec: dict, prompt_template: str, model: str, timeout: int) -> dict:
    paper_id = str(rec.get("paper_id", ""))
    block = build_trial_block(rec)
    prompt = prompt_template.replace("{trial_block}", block)

    # Shared call + retry-on-unparseable scaffolding (ber_core.batch_classify).
    parsed, _cost = call_json(prompt, model, paper_id, timeout=timeout)
    parsed = parsed or {}
    llm_verdict = parsed.get("verdict", "parse_error")

    if llm_verdict not in VALID_VERDICTS and llm_verdict != "parse_error":
        print(f"  {YELLOW}WARN: unexpected verdict '{llm_verdict}' for {paper_id}{RESET}",
              file=sys.stderr, flush=True)

    significant, meta = primary_significance(rec)
    if llm_verdict in VALID_VERDICTS:
        final, note = reconcile(llm_verdict, significant)
    else:
        final, note = llm_verdict, "llm parse_error"

    return {
        "paper_id": paper_id,
        "verdict": final,
        "llm_verdict": llm_verdict,
        "stat_significant": {True: "yes", False: "no", None: "unknown"}[significant],
        "primary_outcome": meta["outcome"],
        "primary_measure": meta["measure"],
        "primary_value": meta["value"],
        "primary_ci": meta["ci"],
        "primary_p": meta["p"],
        "direction_basis": parsed.get("direction_basis", ""),
        "confidence": parsed.get("confidence", ""),
        "rationale": parsed.get("rationale", ""),
        "override_note": note,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True,
                    help="Topic config YAML (e.g. configs/config_my_topic.yaml)")
    ap.add_argument("--input", default=None,
                    help="Extraction JSONL. Default: both projects/{slug}/data/"
                         "trial_extractions_pass2.jsonl AND observational_extractions.jsonl "
                         "(deduped by paper_id). Pass one file to restrict to it.")
    ap.add_argument("--output", default=None,
                    help="Output CSV (default: projects/{slug}/data/trial_verdicts.csv)")
    ap.add_argument("--model", default="sonnet", help="Claude model (default: sonnet)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--limit", type=int, default=0, help="Cap trials this run (0 = all)")
    ap.add_argument("--force", action="store_true", help="Re-classify even if already in output")
    args = ap.parse_args()

    config = load_config(args.config)
    paths = project_paths(config)
    slug = paths["slug"]
    data_dir = paths["data_dir"]
    # Default to BOTH extraction feeds (RCT + observational) so observational
    # trials are never silently skipped; an explicit --input overrides to one file.
    # Pass-2 is listed first, so it stays authoritative on a paper_id collision
    # (mirrors _union_records in export_dashboard.py).
    if args.input:
        in_paths = [Path(args.input)]
    else:
        in_paths = [p for p in (data_dir / "trial_extractions_pass2.jsonl",
                                data_dir / "observational_extractions.jsonl") if p.exists()]
    out_path = Path(args.output) if args.output else data_dir / "trial_verdicts.csv"

    if not in_paths:
        sys.exit(f"ERROR: no extraction file found in {data_dir} "
                 "(expected trial_extractions_pass2.jsonl / observational_extractions.jsonl)")

    template = load_prompt_template("classify_verdict.txt", slug=slug)

    records = []
    seen: set[str] = set()
    for in_path in in_paths:
        for r in read_jsonl(in_path, on_error="warn"):
            if is_failed_status(r.get("_status")):
                continue
            pid = r.get("paper_id")
            if r.get("outcomes") and pid and pid not in seen:
                seen.add(pid)
                records.append(r)

    # Resume: load existing rows, skip already-done paper_ids.
    existing: dict[str, dict] = {}
    if out_path.exists() and not args.force:
        for row in csv.DictReader(open(out_path, encoding="utf-8")):
            existing[row["paper_id"]] = row

    todo = [r for r in records if str(r.get("paper_id")) not in existing]
    if args.limit:
        todo = todo[:args.limit]

    print(f"Project: {slug} | model: {args.model}", file=sys.stderr)
    print(f"inputs: {', '.join(p.name for p in in_paths)}", file=sys.stderr)
    print(f"Trials with outcomes: {len(records)} | already done: {len(existing)} | "
          f"to classify now: {len(todo)}", file=sys.stderr, flush=True)
    if not todo:
        print("Nothing to do.", file=sys.stderr)
        return

    results: dict[str, dict] = dict(existing)
    done = 0
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(classify_one, r, template, args.model, args.timeout):
                str(r.get("paper_id")) for r in todo}
        try:
            for fut in as_completed(futs):
                row = fut.result()
                results[row["paper_id"]] = row
                done += 1
                tag = row["verdict"]
                if row["override_note"]:
                    tag += f"  [{row['override_note']}]"
                print(f"  [{done}/{len(todo)}] {row['paper_id']}: {tag}",
                      file=sys.stderr, flush=True)
                if done % 10 == 0:
                    write_csv_atomic(out_path, list(results.values()), FIELDNAMES)
        except KeyboardInterrupt:
            print(f"\n  {YELLOW}Interrupted — flushing…{RESET}", file=sys.stderr)
            pool.shutdown(wait=False, cancel_futures=True)

    write_csv_atomic(out_path, list(results.values()), FIELDNAMES)
    elapsed = time.monotonic() - start

    # Summary
    import collections
    dist = collections.Counter(r["verdict"] for r in results.values())
    flagged = [r["paper_id"] for r in results.values()
               if r["override_note"].startswith("flag")]
    downgraded = sum(1 for r in results.values()
                     if r["override_note"].startswith("downgraded"))
    print(f"\nWrote {len(results)} verdicts to {out_path} in {elapsed:.0f}s",
          file=sys.stderr)
    print("verdict distribution:", dict(dist.most_common()), file=sys.stderr)
    print(f"guardrail downgrades (favors_* -> no_difference): {downgraded}", file=sys.stderr)
    if flagged:
        print(f"{YELLOW}flagged for review (stats significant, LLM null): "
              f"{len(flagged)}{RESET}\n  " + "\n  ".join(flagged), file=sys.stderr)


if __name__ == "__main__":
    main()
