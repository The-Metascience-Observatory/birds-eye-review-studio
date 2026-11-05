#!/usr/bin/env python3
"""Re-categorize interventions stuck in a catch-all category ("other", blank, …).

Trial extraction assigns each arm an ``intervention_category`` from the config's
``intervention_categories``; a tail lands in "other". This step asks the model
to reassign each such (canonical) intervention to the best category from the
SAME config vocabulary, so the dashboard groups it with the arms extraction
already categorized instead of in one giant "Other" pile.

Operates on canonical intervention names (``intervention_canonical_map.json``
from canonicalize_interventions.py, falling back to ``intervention_name``),
so all variants of an intervention get one consistent category.

Reads:
    projects/{slug}/data/trial_extractions_pass2.jsonl + observational_extractions.jsonl
Writes:
    projects/{slug}/data/intervention_category_map.json   -- {canonical_name: category}
        (only names reassigned away from a catch-all; applied per-arm at export)
    projects/{slug}/data/intervention_category_unresolved.json
        -- {canonical_name: answer} for names the model judged to stay in a
        catch-all (or answered outside the vocabulary), so they are not re-sent
        and re-billed on every run
    projects/{slug}/data/intervention_category_report.csv  -- name, old, new, trials
Model:
    config.models.canonicalize (default "sonnet"; overridable via --model).
Resumability:
    Names in either map are skipped unless --force; both maps are written
    atomically after every batch.
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
from ber_core.utils import (RESET, YELLOW, load_config, load_prompt_template, log_status, project_paths,
                   read_jsonl)

GREEN = "\033[32m"
CLI_TIMEOUT = 300
MAX_BATCH_RETRIES = 2

SOURCE_FILES = ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl")
MAP_FILE = "intervention_category_map.json"
UNRESOLVED_FILE = "intervention_category_unresolved.json"
REPORT_FILE = "intervention_category_report.csv"

# Categories considered "needs reassignment" (the catch-alls).
REASSIGN_FROM = {"other", "unknown", "", "control"}


def allowed_categories(cfg: dict) -> list[str]:
    """The config's intervention vocabulary (what extraction assigns), plus 'other'."""
    cats = [str(c).strip() for c in (cfg.get("intervention_categories") or []) if str(c).strip()]
    if "other" not in cats:
        cats.append("other")
    return cats


def _collect(data_dir: Path, canon_map: dict[str, str]) -> dict[str, dict]:
    """canonical name -> {"trials": set(paper_id), "cats": Counter}.

    The extraction files have no ``intervention_canonical`` (it is added at
    export), so each raw ``intervention_name`` is resolved through
    ``canon_map`` here — keeping the keys identical to what the dashboard groups by.
    """
    info: dict[str, dict] = defaultdict(lambda: {"trials": set(), "cats": Counter()})
    for fname in SOURCE_FILES:
        fp = data_dir / fname
        if not fp.exists():
            log_status(0, 0, "SKIP", f"missing {fname}")
            continue
        n = 0
        for rec in read_jsonl(fp, on_error="warn"):
            if is_failed_status(rec.get("_status")):
                continue
            pid = str(rec.get("paper_id", "")).split("#")[0]
            for a in ((rec.get("study_design") or {}).get("arms")) or []:
                if a.get("type") != "intervention":
                    continue
                raw = (a.get("intervention_canonical") or a.get("intervention_name") or "").strip()
                if not raw:
                    continue
                name = canon_map.get(raw, raw)  # resolve to canonical
                info[name]["trials"].add(pid)
                info[name]["cats"][(a.get("intervention_category") or "other").strip()] += 1
            n += 1
        log_status(0, 0, "OK", f"{fname}: scanned {n} records")
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--batch-size", type=int, default=120)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = project_paths(cfg)
    data_dir = paths["data_dir"]
    slug = paths["slug"]
    topic_name = paths["topic_name"]
    topic_desc = (cfg.get("topic", {}) or {}).get("description", "") or ""
    model = args.model or (cfg.get("models", {}) or {}).get("canonicalize", "sonnet")
    set_log_dest_dir(paths["project_dir"] / "claude_code_logs")

    allowed = allowed_categories(cfg)
    specific = [c for c in allowed if c not in REASSIGN_FROM]
    print(f"{GREEN}Re-categorizing catch-all interventions for '{topic_name}'{RESET}", file=sys.stderr)
    if not specific:
        print(f"{YELLOW}No specific intervention_categories in the config; nothing to "
              f"reassign to.{RESET}", file=sys.stderr)
        return 0

    canon_map = load_map(data_dir / "intervention_canonical_map.json", label="canonical map")
    if canon_map:
        print(f"  loaded {len(canon_map)} canonical name mappings", file=sys.stderr)

    info = _collect(data_dir, canon_map)
    if not info:
        print(f"{YELLOW}No intervention names found under {data_dir}; nothing to do.{RESET}", file=sys.stderr)
        return 0

    # Dominant current category per canonical name.
    dom = {n: v["cats"].most_common(1)[0][0] for n, v in info.items()}
    targets = sorted([n for n, c in dom.items() if c in REASSIGN_FROM], key=str.lower)
    print(f"  {len(info)} interventions; {len(targets)} in catch-all categories to reassign",
          file=sys.stderr)

    if args.dry_run:
        for n in targets[:50]:
            print(f"    {len(info[n]['trials']):>3}  [{dom[n]}]  {n}", file=sys.stderr)
        return 0

    map_path = data_dir / MAP_FILE
    unresolved_path = data_dir / UNRESOLVED_FILE
    existing: dict[str, str] = {} if args.force else load_map(map_path, label=MAP_FILE)
    unresolved: dict[str, str] = {} if args.force else load_map(unresolved_path, label=UNRESOLVED_FILE)
    if existing or unresolved:
        print(f"  loaded {len(existing)} assignments + {len(unresolved)} kept-as-is (resume)",
              file=sys.stderr)

    todo = [n for n in targets if n not in existing and n not in unresolved]
    print(f"  {len(todo)} to classify ({len(existing) + len(unresolved)} already judged)",
          file=sys.stderr)

    prompt_tmpl = load_prompt_template("categorize_interventions.txt", slug=slug)
    categories_list = "\n".join(f"- {c}" for c in allowed)

    def make_prompt(batch):
        payload = [{"name": n, "category": dom[n], "trials": len(info[n]["trials"])} for n in batch]
        return (prompt_tmpl
                .replace("{topic_name}", topic_name)
                .replace("{topic_description}", topic_desc)
                .replace("{categories_list}", categories_list)
                .replace("{names_json}", json.dumps(payload, ensure_ascii=False)))

    def accept(batch, mapping):
        hits = {}
        for n in batch:
            cat = str(lookup(mapping, n) or "").strip()
            if cat in allowed and cat not in REASSIGN_FROM:
                hits[n] = cat
            elif cat:
                # Judged to stay in a catch-all (or answered off-vocabulary):
                # remember the judgment so the name is not re-sent every run.
                unresolved[n] = cat
        return hits

    def persist(store):
        write_map_atomic(unresolved_path, unresolved)
        return store

    classify_batches(todo, batch_size=args.batch_size, make_prompt=make_prompt, accept=accept,
                     model=model, label="categorize", store=existing, store_path=map_path,
                     timeout=CLI_TIMEOUT, retries=MAX_BATCH_RETRIES, persist=persist)

    # Report: every catch-all intervention and where it went (or stayed).
    rows = []
    for n in targets:
        rows.append({
            "intervention": n,
            "old_category": dom[n],
            "new_category": existing.get(n, dom[n]),
            "trials": len(info[n]["trials"]),
            "reassigned": "yes" if n in existing else ("judged_" + unresolved[n] if n in unresolved else "no"),
        })
    rows.sort(key=lambda r: (-r["trials"], r["intervention"].lower()))
    with open(data_dir / REPORT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["intervention", "old_category", "new_category", "trials", "reassigned"])
        w.writeheader()
        w.writerows(rows)

    off_vocab = sorted({v for v in existing.values() if v not in allowed})
    if off_vocab:
        print(f"{YELLOW}  {len(off_vocab)} existing assignment value(s) are outside the config's "
              f"intervention_categories ({', '.join(off_vocab)}); rerun with --force to "
              f"re-categorize on the current vocabulary.{RESET}", file=sys.stderr)
    n_re = sum(1 for n in targets if n in existing)
    print(f"{GREEN}Done.{RESET} reassigned {n_re}/{len(targets)} catch-all interventions.", file=sys.stderr)
    print(f"  map:    {map_path}", file=sys.stderr)
    print(f"  report: {data_dir / REPORT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
