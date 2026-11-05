#!/usr/bin/env python3
"""Canonicalize free-text intervention names into deduplicated canonical labels.

Clinical-trial extraction (extract_pass2.py) records each arm's
``intervention_name`` verbatim from the paper, so the same intervention appears
under many spellings (case, punctuation, brand vs generic, dose/route/setting
qualifiers, acronyms). This step asks the model to cluster those raw names and
emit a canonical display name for each. The canonical name is also the
meta-analysis POOLING identity (``ber_core.scientific.treatment_identity``), so
naming variants of one intervention pool together.

Reads:
    projects/{slug}/data/trial_extractions_pass2.jsonl  (RCT)
    projects/{slug}/data/observational_extractions.jsonl (observational)
    -- every intervention arm (type == "intervention") of usable records.
Writes:
    projects/{slug}/data/intervention_canonical_map.json  -- {raw_name: canonical}
    projects/{slug}/data/intervention_canonical_consolidation.json
        -- {first-pass label: consolidated label}: the consolidation cache
    projects/{slug}/data/intervention_canonical_report.csv -- one row per canonical
        (canonical, n_trials, n_variants, raw_names) sorted by n_trials desc, for
        human inspection of merge quality.
Model:
    config.models.canonicalize (default "sonnet"; overridable via --model).
Resumability:
    Names already in the map are skipped; only new raw names are sent. Both
    maps are written atomically after every batch, so an interrupted run keeps
    what it paid for. --force rebuilds from scratch.
LLM passes:
    1) batch pass -- new raw names (sorted, so lexical variants co-locate) in
       chunks of --batch-size; each batch returns {raw: canonical}.
    2) consolidation pass -- only first-pass labels that have never been
       consolidated are sent, interleaved with the already-settled canonical
       labels as anchors so a new label can merge into an existing one. Settled
       labels are never renamed (no drift between runs) and an unchanged corpus
       makes no consolidation call at all. Skip with --no-consolidate (the new
       labels then stay pending for a later run).
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.batch_classify import classify_batches, load_map, lookup, write_map_atomic
from ber_core.claude_cli import set_log_dest_dir
from ber_core.constants import is_failed_status
from ber_core.utils import (RED, RESET, YELLOW, load_config, load_prompt_template, log_status,
                   project_paths, read_jsonl)

GREEN = "\033[32m"
CLI_TIMEOUT = 300
MAX_BATCH_RETRIES = 2

# Extraction files holding intervention arms (RCT + observational).
SOURCE_FILES = ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl")
MAP_FILE = "intervention_canonical_map.json"
CONSOLIDATION_FILE = "intervention_canonical_consolidation.json"
REPORT_FILE = "intervention_canonical_report.csv"


def _collect_intervention_names(data_dir: Path) -> dict[str, dict]:
    """Scan the extraction jsonls -> {raw_name: {"trials": set(paper_id), "cats": Counter}}.

    A name used in several arms of one trial counts once for that trial (we union
    paper_ids), so trial counts match the dashboard's per-intervention counts.
    """
    info: dict[str, dict] = defaultdict(lambda: {"trials": set(), "cats": Counter()})
    for fname in SOURCE_FILES:
        fp = data_dir / fname
        if not fp.exists():
            log_status(0, 0, "SKIP", f"missing {fname}")
            continue
        n_recs = 0
        for rec in read_jsonl(fp, on_error="warn"):
            if is_failed_status(rec.get("_status")):
                continue
            paper_id = str(rec.get("paper_id", "")).split("#")[0]
            arms = ((rec.get("study_design") or {}).get("arms")) or []
            for a in arms:
                if a.get("type") != "intervention":
                    continue
                name = (a.get("intervention_name") or "").strip()
                if not name:
                    continue
                info[name]["trials"].add(paper_id)
                cat = (a.get("intervention_category") or "unknown").strip()
                info[name]["cats"][cat] += 1
            n_recs += 1
        log_status(0, 0, "OK", f"{fname}: scanned {n_recs} records")
    return info


def _payload(names_info: dict, names: list[str]) -> list[dict]:
    return [{
        "name": n,
        "category": (names_info[n]["cats"].most_common(1)[0][0]
                     if names_info.get(n, {}).get("cats") else "unknown"),
        "trials": len(names_info[n]["trials"]) if names_info.get(n) else 1,
    } for n in names]


def _canonicalize(names_info: dict, names: list[str], *, prompt_tmpl: str, topic_name: str,
                  topic_desc: str, model: str, batch_size: int, label: str,
                  store: dict, store_path: Path, accept_only: set | None = None) -> float:
    """Run ``names`` through the model; accepted {name: canonical} go into ``store``.

    ``accept_only`` restricts what is kept (anchors are sent for context but
    their own mapping is ignored)."""
    def make_prompt(batch):
        return (prompt_tmpl
                .replace("{topic_name}", topic_name)
                .replace("{topic_description}", topic_desc)
                .replace("{names_json}", json.dumps(_payload(names_info, batch),
                                                    ensure_ascii=False, indent=0)))

    def accept(batch, mapping):
        out = {}
        for n in batch:
            if accept_only is not None and n not in accept_only:
                continue
            canon = lookup(mapping, n)
            if isinstance(canon, str) and canon.strip():
                out[n] = canon.strip()
        return out

    return classify_batches(names, batch_size=batch_size, make_prompt=make_prompt, accept=accept,
                            model=model, label=label, store=store, store_path=store_path,
                            timeout=CLI_TIMEOUT, retries=MAX_BATCH_RETRIES)


def _write_report(path: Path, effective: dict[str, str], names_info: dict[str, dict]) -> None:
    """One row per canonical: combined distinct-trial count, variant count, members."""
    groups: dict[str, dict] = defaultdict(lambda: {"trials": set(), "variants": []})
    for raw, canon in effective.items():
        g = groups[canon]
        g["trials"] |= names_info.get(raw, {}).get("trials", set())
        g["variants"].append((raw, len(names_info.get(raw, {}).get("trials", set()) or [])))
    rows = []
    for canon, g in groups.items():
        variants = sorted(g["variants"], key=lambda x: -x[1])
        rows.append({
            "canonical": canon,
            "n_trials": len(g["trials"]),
            "n_variants": len(variants),
            "raw_names": " | ".join(f"{nm} ({c})" for nm, c in variants),
        })
    rows.sort(key=lambda r: (-r["n_trials"], -r["n_variants"], r["canonical"].lower()))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["canonical", "n_trials", "n_variants", "raw_names"])
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Project config YAML")
    ap.add_argument("--model", default=None, help="Override model (else config.models.canonicalize or sonnet)")
    ap.add_argument("--batch-size", type=int, default=200, help="Raw names per model call")
    ap.add_argument("--force", action="store_true", help="Ignore existing maps; recanonicalize all")
    ap.add_argument("--no-consolidate", action="store_true", help="Skip cross-batch merge pass")
    ap.add_argument("--dry-run", action="store_true", help="Report unique-name stats and exit (no LLM)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = project_paths(cfg)
    data_dir = paths["data_dir"]
    slug = paths["slug"]
    topic_name = paths["topic_name"]
    topic_desc = (cfg.get("topic", {}) or {}).get("description", "") or ""
    model = args.model or (cfg.get("models", {}) or {}).get("canonicalize", "sonnet")

    set_log_dest_dir(paths["project_dir"] / "claude_code_logs")

    print(f"{GREEN}Canonicalizing interventions for '{topic_name}' (slug={slug}){RESET}",
          file=sys.stderr)

    names_info = _collect_intervention_names(data_dir)
    if not names_info:
        # Clean no-op: topics without free-text intervention arms have nothing to
        # canonicalize. Exit 0 so a universal pipeline run does not fail here.
        print(f"{YELLOW}No intervention names found under {data_dir}; nothing to do.{RESET}",
              file=sys.stderr)
        return 0

    all_names = sorted(names_info.keys(), key=str.lower)
    total_trials = len({pid for v in names_info.values() for pid in v["trials"]})
    print(f"  {len(all_names)} distinct raw intervention names "
          f"across ~{total_trials} trials", file=sys.stderr)

    if args.dry_run:
        for n in all_names[:40]:
            print(f"    {len(names_info[n]['trials']):>3}  {n}", file=sys.stderr)
        return 0

    map_path = data_dir / MAP_FILE
    cons_path = data_dir / CONSOLIDATION_FILE
    existing: dict[str, str] = {} if args.force else load_map(map_path, label=MAP_FILE)
    consolidation: dict[str, str] = {} if args.force else load_map(cons_path, label=CONSOLIDATION_FILE)
    if existing:
        print(f"  loaded {len(existing)} existing mappings (resume)", file=sys.stderr)
    if existing and not consolidation and not cons_path.exists():
        # A map written before the cache existed was fully consolidated at the
        # end of its run: its labels are settled.
        consolidation = {c: c for c in set(existing.values())}
        write_map_atomic(cons_path, consolidation)

    prompt_tmpl = load_prompt_template("canonicalize_interventions.txt", slug=slug)

    # --- Pass 1: batch-canonicalize names not already mapped ---
    todo = [n for n in all_names if n not in existing]
    print(f"  {len(todo)} names to canonicalize ({len(existing)} already done)", file=sys.stderr)
    if todo:
        _canonicalize(names_info, todo, prompt_tmpl=prompt_tmpl, topic_name=topic_name,
                      topic_desc=topic_desc, model=model, batch_size=args.batch_size,
                      label="pass1", store=existing, store_path=map_path)

    if not existing:
        print(f"{RED}No mappings produced.{RESET}", file=sys.stderr)
        return 1

    # --- Pass 2: consolidate only labels that have never been consolidated ---
    settled = set(consolidation.values())
    pending = sorted({c for c in existing.values()} - settled - set(consolidation), key=str.lower)
    if pending and not args.no_consolidate:
        # Aggregate trial info per label so the model sees weights.
        canon_info: dict[str, dict] = defaultdict(lambda: {"trials": set(), "cats": Counter()})
        for raw, canon in existing.items():
            label = consolidation.get(canon, canon)
            canon_info[label]["trials"] |= names_info.get(raw, {}).get("trials", set())
            canon_info[label]["cats"] += names_info.get(raw, {}).get("cats", Counter())
        # Anchors interleave lexically with the pending labels; only batches
        # containing a pending label are sent.
        ordered = sorted(set(pending) | settled, key=str.lower)
        size = max(1, args.batch_size)
        send = [n for i in range(0, len(ordered), size)
                for n in (ordered[i:i + size] if set(ordered[i:i + size]) & set(pending) else [])]
        print(f"  consolidating {len(pending)} new label(s) against {len(settled)} settled "
              f"({len(send)} labels sent)…", file=sys.stderr)
        newly: dict[str, str] = {}
        _canonicalize(canon_info, send, prompt_tmpl=prompt_tmpl, topic_name=topic_name,
                      topic_desc=topic_desc, model=model, batch_size=size, label="consolidate",
                      store=newly, store_path=None, accept_only=set(pending))
        for label in pending:
            if label in newly:
                consolidation[label] = newly[label]
        write_map_atomic(cons_path, consolidation)
    elif pending:
        print(f"  {len(pending)} label(s) left unconsolidated (--no-consolidate)", file=sys.stderr)
    else:
        print("  consolidation: nothing new", file=sys.stderr)

    # Compose raw -> final; a label consolidated in an earlier run resolves too.
    def resolve(label: str) -> str:
        seen = set()
        while label in consolidation and consolidation[label] != label and label not in seen:
            seen.add(label)
            label = consolidation[label]
        return label

    raw_to_canon = {raw: resolve(canon) for raw, canon in existing.items()}
    write_map_atomic(map_path, raw_to_canon)

    # Effective map covers every raw name (unmapped → itself) for the report.
    effective = {n: raw_to_canon.get(n, n) for n in all_names}
    report_path = data_dir / REPORT_FILE
    _write_report(report_path, effective, names_info)

    n_canon = len(set(effective.values()))
    print(f"{GREEN}Done.{RESET} {len(all_names)} raw names → {n_canon} canonical "
          f"(−{len(all_names) - n_canon}).", file=sys.stderr)
    print(f"  map:    {map_path}", file=sys.stderr)
    print(f"  report: {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
