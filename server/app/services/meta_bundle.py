"""Read a review's built meta-analysis (`meta_analysis.json`) from its bundle.

The forest plot's data lives in `meta_analysis.json`, NOT `summary.json` — the
summary carries only the feasibility verdict, not the per-group pooled estimates.
So the studio dashboard needs this separate reader to render forest plots.

Mirrors `bundle.read_summary`: read-only, derives nothing, and returns whatever
the pipeline's meta-analysis step wrote. A review with no pooled meta-analysis
(most corpora) simply has no file here, and this returns an empty groups list —
the dashboard then renders no forest-plot section, unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import bundle as bundle_svc

META = "meta_analysis.json"


def read_meta(workspace: Path) -> dict:
    """`{groups, min_trials, methods}` from the bundle, or empty when absent."""
    d = bundle_svc.bundle_dir(workspace)
    empty = {"groups": [], "min_trials": None, "methods": None}
    if d is None:
        return empty
    p = d / META
    if not p.exists():
        return empty
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    from ber_core.scientific import RULES_VERSION
    current = data.get("rules_version") == RULES_VERSION
    return {
        "rules_version": data.get("rules_version"),
        "validation_status": "validated" if current else "requires_rebuild",
        "withheld": data.get("withheld") or [],
        "validation": data.get("validation") or [],
        "possible_companion_reports": data.get("possible_companion_reports") or [],
        "groups": data.get("groups") or [],
        "min_trials": data.get("min_trials"),
        "methods": data.get("methods") or data.get("options"),
    }
