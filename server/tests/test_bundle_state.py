"""Bundle staleness (`services/bundle.py::bundle_state`).

Two independent staleness causes with different fixes, so the scope matters as
much as the flag: ``"data"`` (extraction feed newer than the bundle) needs the
full export re-run; ``"layout"`` (the config's ``dashboard`` subtree no longer
matches the digest stamped into the manifest) only needs the seconds-long
manifest step. The studio's auto-rebuild picks its path from ``stale_scope``,
so a wrong scope silently turns a cheap rebuild into a full one — or worse,
leaves a layout edit doing nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from app.services import bundle
from ber_core.utils import dashboard_layout_digest
from ber_core.scientific import RULES_VERSION


def _workspace(tmp_path: Path, *, dashboard_cfg: dict | None = None,
               digest: str | None = "auto",
               manifest: bool = True) -> Path:
    """A minimal on-disk workspace: config.yaml + a built bundle.

    ``digest="auto"`` stamps the manifest with the digest matching the config —
    the fresh state. Pass an explicit string (or None to omit the field) to
    fabricate a mismatch or a pre-editor manifest.
    """
    ws = tmp_path / "ws"
    dash = ws / "projects" / "x" / "dashboard"
    dash.mkdir(parents=True)
    cfg = {"topic": {"name": "X"}}
    if dashboard_cfg is not None:
        cfg["dashboard"] = dashboard_cfg
    (ws / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (dash / "summary.json").write_text(json.dumps({
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "scientific_rules_version": RULES_VERSION,
        "totals": {"n_trials": 3},
        "sizing": {"mode": "inline"},
    }), encoding="utf-8")
    if manifest:
        m: dict = {"title": "t", "layout_version": 2}
        if digest == "auto":
            m["layout_digest"] = dashboard_layout_digest(dashboard_cfg or {})
        elif digest is not None:
            m["layout_digest"] = digest
        (dash / "dashboard.manifest.json").write_text(json.dumps(m),
                                                      encoding="utf-8")
    return ws


def test_no_bundle_is_not_stale(tmp_path: Path) -> None:
    state = bundle.bundle_state(tmp_path)
    assert state["exists"] is False
    assert state["stale"] is False
    assert state["stale_scope"] is None


def test_matching_digest_is_fresh(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, dashboard_cfg={"panels": ["year_chart"]})
    state = bundle.bundle_state(ws)
    assert state["exists"] is True
    assert state["stale"] is False
    assert state["stale_scope"] is None


def test_layout_edit_marks_layout_stale(tmp_path: Path) -> None:
    """The user toggled a panel after the manifest was written: the config's
    digest no longer matches the stamped one."""
    ws = _workspace(tmp_path, dashboard_cfg={"panels": ["year_chart"]})
    cfg = yaml.safe_load((ws / "config.yaml").read_text())
    cfg["dashboard"]["panels"] = ["year_chart", "country_map"]
    (ws / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    state = bundle.bundle_state(ws)
    assert state["stale"] is True
    assert state["stale_scope"] == "layout"
    assert "layout" in (state["reason"] or "")


def test_data_staleness_wins_over_layout(tmp_path: Path) -> None:
    """Both causes at once report as "data": the full rebuild re-runs the
    manifest step anyway, so the heavier fix subsumes the cheap one."""
    ws = _workspace(tmp_path, dashboard_cfg={"panels": []}, digest="stale-digest")
    data = ws / "projects" / "x" / "data"
    data.mkdir(parents=True)
    feed = data / "trial_extractions_pass2.jsonl"
    feed.write_text("{}\n", encoding="utf-8")
    summary = ws / "projects" / "x" / "dashboard" / "summary.json"
    now = summary.stat().st_mtime
    os.utime(feed, (now + 60, now + 60))

    state = bundle.bundle_state(ws)
    assert state["stale"] is True
    assert state["stale_scope"] == "data"


def test_pre_editor_manifest_is_layout_stale(tmp_path: Path) -> None:
    """A bundle built before the editor existed has no digest; it reads as
    layout-stale ONCE so a single cheap manifest run stamps it (self-heal)."""
    ws = _workspace(tmp_path, digest=None)
    state = bundle.bundle_state(ws)
    assert state["stale"] is True
    assert state["stale_scope"] == "layout"


def test_missing_manifest_is_layout_stale(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, manifest=False)
    state = bundle.bundle_state(ws)
    assert state["stale"] is True
    assert state["stale_scope"] == "layout"


def test_old_scientific_bundle_requires_data_rebuild(tmp_path):
    ws = _workspace(tmp_path)
    p = ws / "projects/x/dashboard/summary.json"
    s = json.loads(p.read_text()); s.pop("scientific_rules_version"); p.write_text(json.dumps(s))
    state = bundle.bundle_state(ws)
    assert state["stale_scope"] == "data" and "scientific validation" in state["reason"]
