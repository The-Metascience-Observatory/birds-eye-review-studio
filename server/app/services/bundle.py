"""Read a review's locally-built dashboard bundle.

The pipeline builds a bundle into the review's own workspace
(``<workspace>/projects/<slug>/dashboard/``) and stops; publishing it to the
public site is a separate, permission-gated action. This module is how the
studio reads that bundle so a user can SEE their dashboard before deciding
whether to publish it.

Deliberately read-only and free of aggregation: every number comes from
``summary.json``, which the pipeline computed once. If this module started
deriving counts, the studio and the published dashboard could disagree — the
exact failure ``dashboard_summary.py`` exists to prevent.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()
from ber_core.utils import dashboard_layout_digest

# Files the bundle may contain. `summary.json` and `dashboard_rows.jsonl` are
# the two the studio actually renders from.
SUMMARY = "summary.json"
ROWS = "dashboard_rows.jsonl"
MANIFEST = "dashboard.manifest.json"


def bundle_dir(workspace: Path) -> Path | None:
    """The review's dashboard bundle directory, or None if never built.

    A workspace holds exactly one project, but its slug is derived from the
    topic name, so glob rather than recompute it — a renamed topic would
    otherwise silently point at a directory that does not exist.
    """
    projects = Path(workspace) / "projects"
    if not projects.is_dir():
        return None
    for child in sorted(projects.iterdir()):
        d = child / "dashboard"
        if (d / SUMMARY).exists():
            return d
    return None


def read_summary(workspace: Path) -> dict | None:
    d = bundle_dir(workspace)
    if d is None:
        return None
    try:
        return json.loads((d / SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_manifest(workspace: Path) -> dict | None:
    d = bundle_dir(workspace)
    if d is None:
        return None
    p = d / MANIFEST
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_rows(workspace: Path, *, offset: int = 0, limit: int | None = None) -> list[dict]:
    """The canonical per-trial projection.

    Streamed line by line rather than read whole: the row file is small today
    (every current corpus is under the inline threshold), but a paged consumer
    must not have to re-read this module to stop loading 22 MB.
    """
    d = bundle_dir(workspace)
    if d is None:
        return []
    p = d / ROWS
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < offset:
                continue
            if limit is not None and len(out) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                # A torn final line during an active write. The export rewrites
                # this file while the dashboard page polls it, so an unguarded
                # json.loads turns a mid-write request into a 500. Skipping the
                # partial line yields a row short for one poll instead — the
                # same tolerance evidence._read_jsonl already applies.
                continue
    return out


def _config_dashboard(workspace: Path) -> dict:
    """The ``dashboard`` subtree of the workspace's config.yaml — the same
    subtree ``generate_dashboard.py`` digests into the manifest."""
    p = Path(workspace) / "config.yaml"
    if not p.exists():
        return {}
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return cfg.get("dashboard") or {}


def bundle_state(workspace: Path) -> dict:
    """Enough for the UI to decide what to show, without reading the bundle.

    ``stale`` compares the bundle against the pipeline data it was built from,
    so a user who re-ran extraction sees that the dashboard predates it rather
    than quietly viewing an old picture. ``stale_scope`` distinguishes the two
    causes so the client can pick the fix: ``"data"`` needs the full export
    rebuilt; ``"layout"`` (the config's ``dashboard`` subtree changed since the
    manifest was stamped) only needs the seconds-long manifest step re-run.
    """
    d = bundle_dir(workspace)
    if d is None:
        return {"exists": False, "stale": False, "built_at": None,
                "n_trials": None, "reason": "no dashboard has been built yet",
                "stale_scope": None}

    summary = read_summary(workspace) or {}
    built_mtime = (d / SUMMARY).stat().st_mtime

    # The feed the bundle was built from. Newer source data => stale bundle.
    # Data staleness wins over layout (the full rebuild re-runs the manifest
    # step anyway, so one signal at a time is enough).
    stale, reason, stale_scope = False, None, None
    from ber_core.bundles import INPUT_MANIFEST, INPUTS, source_fingerprint
    inputs = d / INPUT_MANIFEST
    if inputs.exists():
        try:
            cfg = yaml.safe_load((Path(workspace) / "config.yaml").read_text()) or {}
            stale = json.loads(inputs.read_text()) != source_fingerprint(d.parent / "data", cfg)
        except (OSError, ValueError):
            stale = True
    else:
        # Legacy bundles have no fingerprint; watch every known source until
        # their next rebuild stamps the complete contract.
        stale = any((d.parent / "data" / name).is_file()
                    and (d.parent / "data" / name).stat().st_mtime > built_mtime
                    for name in INPUTS)
    if stale:
        reason = "review inputs changed after the dashboard was built"
        stale_scope = "data"

    from ber_core.scientific import RULES_VERSION
    summary = read_summary(workspace) or {}
    if summary.get("scientific_rules_version") != RULES_VERSION:
        stale, reason, stale_scope = True, "scientific validation rules changed; rebuild required", "data"

    if not stale:
        # A manifest with no digest (pre-editor bundle, or missing manifest)
        # reads as layout-stale on purpose: one cheap manifest run stamps it.
        manifest = read_manifest(workspace)
        want = dashboard_layout_digest(_config_dashboard(workspace))
        if manifest is None or manifest.get("layout_digest") != want:
            stale = True
            reason = "the dashboard layout was edited after the last build"
            stale_scope = "layout"

    return {
        "exists": True,
        "stale": stale,
        "reason": reason,
        "stale_scope": stale_scope,
        "built_at": summary.get("generated_at_utc"),
        "n_trials": (summary.get("totals") or {}).get("n_trials"),
        "mode": (summary.get("sizing") or {}).get("mode"),
    }
