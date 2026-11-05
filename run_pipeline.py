#!/usr/bin/env python3
"""Run the whole bird's-eye-review pipeline end to end for one project.

Drives the canonical steps in order (see pipeline.py), stopping on the first
failure OR when a step exits 0 but produces no output (per-step output
verification via Step.produces). Because every step is individually resumable and
skips already-processed records, re-running this only spends time/money on the
papers that are genuinely new. The PDF download/convert and full-text
eligibility prerequisites are inserted automatically before pass 2.

Run it from a checkout (the step scripts in steps/ are not an installable package).
Steps are selected by id or number; ``--list`` prints the run order.

Examples:
    # Start a new review from the annotated example config:
    cp configs/example.yaml configs/config_my_topic.yaml   # then edit it
    python run_pipeline.py --config configs/config_my_topic.yaml

    # Incremental update since the last run, then rebuild the local dashboard bundle:
    python run_pipeline.py --config configs/config_my_topic.yaml --since 2026-03-01

    # Full periodic rescan (re-issue every search query):
    python run_pipeline.py --config configs/config_my_topic.yaml --fresh

    # Just re-export the merged feed (step 17), no re-extraction:
    python run_pipeline.py --config configs/config_my_topic.yaml --only 17

    # Show every step (number, id, script) in run order:
    python run_pipeline.py --config configs/config_my_topic.yaml --list

    # Preview the exact commands without running anything:
    python run_pipeline.py --config configs/config_my_topic.yaml --since 2026-03-01 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ber_core import pipeline

BASE_DIR = Path(__file__).resolve().parent


def _shown(step) -> str:
    """The step's script as a user would type it from the repo root."""
    path = Path(step.script_path)
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)

from ber_core.execution import freshness_warning, verify_produces as _verify_produces


def _auto_since(project) -> str | None:
    """Read the last successful search date persisted by search_papers.py."""
    pf = project.progress_dir / "search_progress.json"
    if pf.exists():
        try:
            return json.loads(pf.read_text(encoding="utf-8")).get("last_run_date")
        except Exception:
            return None
    return None


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True,
                   help="The review's config YAML (e.g. configs/config_x.yaml)")
    p.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="Incremental search window. Use 'auto' to read the last "
                        "run date from search_progress.json.")
    p.add_argument("--fresh", action="store_true",
                   help="Full search rescan (ignore completed-query progress)")
    p.add_argument("--no-observational", action="store_true",
                   help="RCT-only topic: skip observational pass2/merge")
    p.add_argument("--meta-analysis", action="store_true",
                   help="Generate forest-plot pooling (meta_analysis.json) in the "
                        "export step; off by default")
    p.add_argument("--from", dest="start", default=None, metavar="STEP",
                   help="Start at this step (id or number; see --list)")
    p.add_argument("--to", dest="stop", default=None, metavar="STEP",
                   help="Stop after this step (id or number)")
    p.add_argument("--only", default=None, metavar="STEPS",
                   help="Run only these steps (comma-separated ids or numbers)")
    p.add_argument("--list", action="store_true",
                   help="List every step in run order and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the commands without running them")
    p.add_argument("--yes", action="store_true",
                   help="Don't prompt for confirmation before running")
    args = p.parse_args()

    project = pipeline.load_project(args.config)
    since = args.since
    if since == "auto":
        since = _auto_since(project)
        print(f"[run_pipeline] --since auto -> {since or '(no prior run; full search)'}")

    steps = pipeline.canonical_steps(
        project, since=since, fresh=args.fresh,
        # Flags override; otherwise follow the config, as the studio does.
        include_observational=(
            not args.no_observational
            and bool(project.config.get("include_observational", True))
            # An RCT-only meta-analysis config turns the observational pass off.
            and (project.config.get("meta_analysis") or {}).get("designs") != "rct_only"),
        generate_meta_analysis=(args.meta_analysis
                                or bool(project.config.get("generate_meta_analysis", False))),
    )
    if args.list:
        print(f"{'num':>4}  {'id':<34} {'script':<40} label")
        for s in pipeline.execution_steps(project, steps):
            print(f"{s.num:>4}  {s.id:<34} {_shown(s):<40} {s.label}")
        return 0

    only = [x.strip() for x in args.only.split(",")] if args.only else None
    steps = pipeline.select_run(project, steps, start=args.start, stop=args.stop, only=only)

    if not steps:
        print("[run_pipeline] No steps selected.")
        return 1

    print(f"\n=== Pipeline for {project.topic_name!r} ({project.slug}) — "
          f"{len(steps)} step(s) ===")
    for s in steps:
        print(f"  [{s.num:2d}] {s.label}")
        print(f"        python {_shown(s)} {' '.join(s.args)}")

    if args.dry_run:
        print("\nDry run; nothing executed.")
        return 0

    if not args.yes and sys.stdin.isatty():
        resp = input("\nProceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    # A config may ship its own prompt overrides (``prompts_dir:``, relative to
    # the config file) — e.g. a review kept outside this repo. An explicit
    # BER_PROMPTS_DIR in the environment still wins.
    env = os.environ.copy()
    prompts_dir = project.config.get("prompts_dir")
    if prompts_dir and not env.get("BER_PROMPTS_DIR"):
        env["BER_PROMPTS_DIR"] = str((project.config_path.parent / prompts_dir).resolve())

    started = time.monotonic()
    for s in steps:
        print(f"\n{'=' * 70}\n[{s.num:2d}] {s.label}\n"
              f"     python {_shown(s)} {' '.join(s.args)}\n{'=' * 70}", flush=True)
        cmd = [sys.executable, "-u", s.script_path, *s.args]
        step_started = time.time()
        rc = subprocess.run(cmd, cwd=str(BASE_DIR), env=env).returncode
        if rc != 0:
            print(f"\n[run_pipeline] STEP {s.num} ({s.id}) FAILED with exit {rc}. "
                  f"Stopping.", flush=True)
            return rc
        # Verify the step actually produced its declared output before advancing.
        err = _verify_produces(s)
        if err:
            print(f"\n[run_pipeline] STEP {s.num} ({s.id}) exited 0 but {err}. "
                  f"Stopping (step produced no usable output).", flush=True)
            return 1
        stale = freshness_warning(s, step_started)
        if stale:
            print(f"[run_pipeline] WARNING: step {s.num} ({s.id}): {stale}.", flush=True)

    mins = (time.monotonic() - started) / 60
    print(f"\n[run_pipeline] All {len(steps)} step(s) completed in {mins:.1f} min.")
    print("Preview the local dashboard bundle. Publishing is a separate approved operation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
