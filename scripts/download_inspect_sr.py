#!/usr/bin/env python3
"""Download the INSPECT-SR guidance checks used by systematic_review.py.

Fetches the guidance source (``index.qmd`` + the 21 ``chapters/check_*.qmd``,
plus the license and appendices) from github.com/ianhussey/inspect-sr-guidance
at a pinned commit into ``docs/inspect_sr/upstream/`` (git-ignored). The
guidance text is CC BY 4.0 (© Jack Wilkinson); see ``THIRD_PARTY.md``.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import requests

REPO = "ianhussey/inspect-sr-guidance"
# Pinned for reproducibility; pass --commit main to take the latest.
DEFAULT_COMMIT = "f204f85d642651f4bf4585a43f51d8bd47c2a64c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docs/inspect_sr/upstream")
    parser.add_argument("--commit", default=DEFAULT_COMMIT,
                        help="Upstream commit SHA or branch (default: pinned commit)")
    args = parser.parse_args()
    response = requests.get(
        f"https://api.github.com/repos/{REPO}/git/trees/{args.commit}?recursive=1", timeout=30)
    response.raise_for_status()
    tree = response.json()
    paths = [r["path"] for r in tree["tree"] if r["path"].endswith(".qmd")]
    if len([p for p in paths if p.startswith("chapters/check_")]) != 21:
        raise ValueError("Guidance structure changed; review download before use")

    def fetch(path):
        url = f"https://raw.githubusercontent.com/{REPO}/{tree['sha']}/{path}"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dest = args.out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return {"url": url, "path": path, "sha256": hashlib.sha256(r.content).hexdigest()}

    with ThreadPoolExecutor(max_workers=6) as pool:
        files = list(pool.map(fetch, paths))
    (args.out / "manifest.json").write_text(json.dumps({
        "repo": REPO, "commit": tree["sha"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "license": "CC BY 4.0 (guidance text); MIT (code)", "files": files}, indent=2), encoding="utf-8")
    print(f"Downloaded {len(files)} guidance files to {args.out}")


if __name__ == "__main__":
    main()
