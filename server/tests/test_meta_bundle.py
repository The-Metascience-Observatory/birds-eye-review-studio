"""The meta-analysis bundle reader (`services/meta_bundle.py`).

Powers the studio's forest plots. It must read the pooled groups from a built
bundle and, crucially, return an EMPTY groups list (never raise) for the common
case of a review with no meta-analysis — otherwise the dashboard would error
instead of simply omitting the forest-plot section.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import bundle as bundle_svc  # noqa: E402
from server.app.services import meta_bundle  # noqa: E402


def _bundle(workspace: Path, *, meta: dict | None) -> None:
    bdir = workspace / "projects" / "t" / "dashboard"
    bdir.mkdir(parents=True, exist_ok=True)
    # A summary.json must exist for bundle_dir() to resolve the dir.
    (bdir / bundle_svc.SUMMARY).write_text("{}", encoding="utf-8")
    if meta is not None:
        (bdir / meta_bundle.META).write_text(json.dumps(meta), encoding="utf-8")


def test_no_bundle_returns_empty(tmp_path) -> None:
    got = meta_bundle.read_meta(tmp_path)
    assert got == {"groups": [], "min_trials": None, "methods": None}


def test_bundle_without_meta_file_returns_empty(tmp_path) -> None:
    _bundle(tmp_path, meta=None)  # summary exists, meta_analysis.json does not
    got = meta_bundle.read_meta(tmp_path)
    assert got["groups"] == []


def test_reads_groups_min_trials_and_methods(tmp_path) -> None:
    meta = {
        "min_trials": 3,
        "methods": {"model": "DL", "effect_size": "smd"},
        "groups": [
            {"ingredient": "cabergoline", "outcome_domain": "symptom_severity",
             "n_trials": 4, "pooled": {"effect": -1.9}},
        ],
    }
    _bundle(tmp_path, meta=meta)
    got = meta_bundle.read_meta(tmp_path)
    assert got["min_trials"] == 3
    assert got["methods"]["model"] == "DL"
    assert len(got["groups"]) == 1
    assert got["groups"][0]["ingredient"] == "cabergoline"


def test_min_trials_is_served_so_the_client_filters_at_the_pooled_threshold(
    tmp_path,
) -> None:
    """The client MUST filter forest groups at the threshold the pooling used.

    Regression: the pipeline pools at `min_trials: 2` by default, but the
    dashboard's `poolableGroups` defaulted to 3 and `DashboardShell` called it
    with no argument. A real review pooled into 2-trial groups therefore wrote a
    meta_analysis.json full of genuine estimates and rendered NOTHING — the
    "view the forest plot" link led to an empty dashboard.

    Two thresholds for one decision is the bug. This pins the half the server
    owns: `min_trials` reaches the client, so it can filter at the real value
    instead of guessing.
    """
    meta = {
        "min_trials": 2,
        "groups": [
            {"outcome_domain": "blood_pressure", "n_trials": 2,
             "pooled": {"effect": -1.41, "ci_low": -3.76, "ci_high": 0.95}},
        ],
    }
    _bundle(tmp_path, meta=meta)
    got = meta_bundle.read_meta(tmp_path)

    assert got["min_trials"] == 2, "the client cannot filter correctly without it"
    # The group would be dropped by a hardcoded 3 despite being genuinely pooled.
    assert got["groups"][0]["n_trials"] < 3
    assert got["groups"][0]["pooled"]["effect"] is not None


def test_malformed_meta_file_is_empty_not_fatal(tmp_path) -> None:
    _bundle(tmp_path, meta=None)
    bdir = tmp_path / "projects" / "t" / "dashboard"
    (bdir / meta_bundle.META).write_text("{ not json", encoding="utf-8")
    got = meta_bundle.read_meta(tmp_path)
    assert got["groups"] == []
