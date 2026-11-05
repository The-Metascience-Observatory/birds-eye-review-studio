#!/usr/bin/env python3
"""Import DOI-associated legacy supplements; dry run unless --execute."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ber_core.supplement_import import import_supplements
from ber_core.utils import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="JSON list of {paper_id, source, sha256, optional filename}")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    entries = json.loads(args.manifest.read_text())
    if not isinstance(entries, list):
        parser.error("Manifest must contain a list")
    result = import_supplements(args.project, load_config(args.config), entries, execute=args.execute)
    print(json.dumps({"executed": args.execute, "files": result}, indent=2))


if __name__ == "__main__":
    main()
