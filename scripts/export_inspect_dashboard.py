#!/usr/bin/env python3
"""Export completed review decisions for a website, without rerunning an LLM."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ber_core.inspect_dashboard import dashboard_assessments
from ber_core.inspect_sources import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    out = args.review_dir
    with (out / ".run.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        read = lambda name: json.loads((out / name).read_text(encoding="utf-8"))
        status = read("run_status.json")
        if status["state"] != "automated_draft_complete":
            parser.error("Only a completed full review can be exported")
        bundle = dashboard_assessments(read("decisions.json"), read("selection_audit.json"),
                                      assessed_at=status["updated_at"], model=read("manifest.json")["model"])
        write_json(args.output, bundle)
    print(f"Exported {len(bundle['papers'])} report assessments to {args.output}")


if __name__ == "__main__":
    main()
