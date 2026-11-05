#!/usr/bin/env python3
"""Collapse piled-up ``*.provenance.json.dup*`` files in a paper store.

WHY THIS EXISTS
---------------
fetchpdf writes a ``<stem>.provenance.json`` per fetch ATTEMPT. When a paper
was re-fetched into ``_incoming/`` and then refiled onto its existing folder,
``paper_store.merge_artifact`` treated the two records as a content collision
and kept the incoming one as ``.dup``, ``.dup2``, ... — one more per re-fetch
(long_covid accumulated 5,060). ``merge_artifact`` now keeps the NEWER record
instead; this script cleans up the ones already written.

WHAT IT DOES
------------
For every paper folder that has ``<name>.provenance.json`` and/or any
``<name>.provenance.json.dup*`` siblings (likewise ``*.manifest.json``), it keeps
the single newest record — by its ``completed_at`` (else ``started_at``, else
the file mtime) — at the canonical name, and deletes the rest.

Dry run by default: prints what it would do. ``--execute`` performs it.

Usage::

    python scripts/maintenance/cleanup_provenance_dups.py --store projects/<slug>/papers
    python scripts/maintenance/cleanup_provenance_dups.py --config configs/config_x.yaml --execute
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ber_core import paper_store  # noqa: E402

_DUP_RE = re.compile(r"^(?P<base>.+(?:\.provenance\.json|\.manifest\.json))\.dup\d*$")


def plan(store: Path) -> list[tuple[Path, Path, list[Path]]]:
    """[(canonical path, newest file, files to delete)] for every affected group."""
    groups: dict[Path, list[Path]] = defaultdict(list)
    for dup in store.rglob("*.dup*"):
        m = _DUP_RE.match(dup.name)
        if not m or not dup.is_file() or dup.is_symlink():
            continue
        canonical = dup.with_name(m.group("base"))
        groups[canonical].append(dup)
    out = []
    for canonical, dups in sorted(groups.items()):
        candidates = list(dups)
        if canonical.is_file() and not canonical.is_symlink():
            candidates.append(canonical)
        newest = max(candidates, key=paper_store._record_time)
        out.append((canonical, newest, [p for p in candidates if p != newest]))
    return out


def apply(groups) -> int:
    removed = 0
    for canonical, newest, losers in groups:
        for path in losers:
            path.unlink()
            removed += 1
        if newest != canonical:
            newest.replace(canonical)
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--store", type=Path, help="The paper store root (papers/)")
    where.add_argument("--config", help="A project config; its store is resolved")
    parser.add_argument("--execute", action="store_true",
                        help="Delete the superseded records (default: dry run)")
    args = parser.parse_args(argv)

    if args.store:
        store = args.store
    else:
        from ber_core.utils import load_config, project_paths
        config = load_config(args.config)
        store = paper_store.papers_root(project_paths(config)["project_dir"], config=config)
    if not store.is_dir():
        print(f"ERROR: no paper store at {store}", file=sys.stderr)
        return 1

    groups = plan(store)
    n_files = sum(len(losers) for _, _, losers in groups)
    print(f"Store: {store}")
    print(f"{len(groups)} record group(s); {n_files} superseded file(s)")
    for canonical, newest, losers in groups[:5]:
        print(f"  {canonical.parent.name}/{canonical.name}: keep "
              f"{newest.name}, drop {len(losers)}")
    if not args.execute:
        print("DRY RUN — nothing deleted. Re-run with --execute.")
        return 0
    removed = apply(groups)
    print(f"Removed {removed} superseded record(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
