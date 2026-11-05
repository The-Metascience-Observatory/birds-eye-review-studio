#!/usr/bin/env python3
"""Quarantine pass-2 extraction records for papers that were never eligible.

WHY THIS EXISTS
---------------
`extract_pass2.py` used to resolve its input directory through
`utils.resolve_input_dir`, whose last-resort fallback is the project's
`converted/` dir. The coupling to the staged bucket lived only in the optional
`dirs.eligible_trials` config key — which nothing in the repo ever set. So pass 2
silently extracted the ENTIRE converted corpus instead of the staged-eligible
subset: ~25x the Sonnet spend, and (because `evidence._screen_state` used to rank
"extracted" above the pass-1 verdict) ineligible papers were laundered into the
included set, the PRISMA counts, and the meta-analysis pool.

The extraction path is fixed. This script cleans up the records that bug wrote.

WHAT IT DOES
------------
Partitions `trial_extractions_pass2.jsonl` (or the observational equivalent)
using the SAME predicate `stage_eligible.py` applies when staging:

    keep  <=>  (pass-1 screening.eligible_for_pass2 is truthy
                OR paper is in user_inclusions.json)
               AND paper is NOT in user_exclusions.json

Kept records stay in place. Everything else moves to a sibling
`*.quarantine.jsonl`. Nothing is deleted.

Records are matched on `norm_doi(paper_id)`, the same normalization the rest of
the pipeline keys on.

WHY NOT JUST DELETE
-------------------
`extract_pass2.py` resumes by reading the output file's `paper_id`s
(`load_done_ids_jsonl`). Dropping a legitimately-extracted paper silently re-bills
it on the next run — so the quarantine file is kept, and a backup is written
before anything is rewritten.

USAGE
-----
    # dry run (default): report only, touch nothing
    python3 scripts/quarantine_pass2.py --config path/to/config.yaml

    # actually rewrite
    python3 scripts/quarantine_pass2.py --config path/to/config.yaml --execute

For a studio review, point BER_PROJECTS_DIR at the review's projects/ dir:
    BER_PROJECTS_DIR=server/_data/reviews/<id>/projects \
        python3 scripts/quarantine_pass2.py --config server/_data/reviews/<id>/config.yaml
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ber_core.utils import (  # noqa: E402
    load_config, project_paths, norm_doi,
    load_exclusion_dois, load_inclusion_dois, read_jsonl,
    RED, YELLOW, RESET,
)

GREEN = "\033[32m"  # utils defines RED/YELLOW/RESET but not GREEN


def eligible_keys(data_dir: Path, pass1_name: str = "trial_extractions.jsonl") -> set:
    """The set of norm_doi keys that were ever legitimately extractable.

    Mirrors stage_eligible.py's staging predicate exactly — see module docstring.
    """
    user_excluded = load_exclusion_dois(data_dir / "user_exclusions.json")
    user_included = load_inclusion_dois(data_dir / "user_inclusions.json") - user_excluded

    keep = set(user_included)
    pass1 = data_dir / pass1_name
    for rec in read_jsonl(pass1, on_error="warn"):
        screening = rec.get("screening") or {}
        if screening.get("eligible_for_pass2"):
            keep.add(norm_doi(str(rec.get("paper_id", "")).split("#")[0]))
    return keep - user_excluded


def partition(jsonl: Path, keep_keys: set) -> tuple[list[str], list[str], int]:
    """Split *jsonl* lines into (keep, quarantine, malformed_count)."""
    keep, drop, bad = [], [], 0
    with jsonl.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Unparseable lines are kept: they cannot be judged, and dropping
                # them would silently change resume behaviour.
                bad += 1
                keep.append(line)
                continue
            key = norm_doi(str(rec.get("paper_id", "")).split("#")[0])
            (keep if key in keep_keys else drop).append(line)
    return keep, drop, bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to the review's config.yaml")
    ap.add_argument("--execute", action="store_true",
                    help="Actually rewrite the files (default: dry run)")
    ap.add_argument("--design-type", choices=("rct", "observational", "both"),
                    default="both", help="Which extraction file(s) to clean")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = project_paths(cfg)
    data_dir = paths["data_dir"]

    keep_keys = eligible_keys(data_dir)
    print(f"Project: {paths['topic_name']!r}")
    print(f"  eligible (pass-1 true + user-included, minus user-excluded): "
          f"{len(keep_keys)}")

    targets = []
    if args.design_type in ("rct", "both"):
        targets.append(data_dir / "trial_extractions_pass2.jsonl")
    if args.design_type in ("observational", "both"):
        targets.append(data_dir / "observational_extractions.jsonl")

    rc = 0
    for jsonl in targets:
        if not jsonl.exists():
            continue
        keep, drop, bad = partition(jsonl, keep_keys)
        total = len(keep) + len(drop)
        print(f"\n{jsonl.name}: {total} records")
        print(f"  {GREEN}keep      : {len(keep)}{RESET}")
        print(f"  {YELLOW}quarantine: {len(drop)}{RESET}"
              + (f"  ({len(drop) / total:.0%} of the file)" if total else ""))
        if bad:
            print(f"  {YELLOW}({bad} unparseable line(s) kept as-is){RESET}")

        if not drop:
            print("  nothing to do.")
            continue

        if not args.execute:
            print(f"  {YELLOW}dry run — pass --execute to apply.{RESET}")
            rc = 1
            continue

        backup = jsonl.with_suffix(jsonl.suffix + ".bak")
        if backup.exists():
            print(f"  {RED}refusing to overwrite existing backup {backup.name}"
                  f"{RESET} — move it aside first.")
            rc = 2
            continue
        shutil.copy2(jsonl, backup)

        quarantine = jsonl.with_name(jsonl.stem + ".quarantine.jsonl")
        # Append: a second run must not clobber an earlier quarantine batch.
        with quarantine.open("a", encoding="utf-8") as f:
            f.writelines(drop)
        tmp = jsonl.with_suffix(jsonl.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(jsonl)  # atomic
        print(f"  {GREEN}wrote{RESET} {jsonl.name} ({len(keep)} records)")
        print(f"  backup   -> {backup.name}")
        print(f"  moved    -> {quarantine.name} ({len(drop)} records)")

    return rc


if __name__ == "__main__":
    sys.exit(main())
