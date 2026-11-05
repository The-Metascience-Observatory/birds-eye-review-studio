"""The dashboard feed must honor manual exclusions, and the shrink guard must
still protect PUBLISHING.

Two regressions, both found on the nattokinase review.

1. `export_dashboard.py` read only `delivery_exclusions.json` (a nasal-spray
   file), never `user_exclusions.json`. A studio "Exclude" greyed the row in the
   evidence table but the paper stayed in the dashboard — so the Extract table
   and the Export table silently disagreed by exactly the manual exclusions.

2. The information-loss guard blocked the LOCAL preview build. It exists to stop
   a *published* feed losing studies (it once caught a 511-study drop), but a
   local rebuild after a legitimate cleanup shrank 30 -> 13 records and the guard
   wedged the export stage with no way to clear it from the UI. The local build
   now passes `--allow-shrink`; publishing must NOT inherit that.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --- 1. The local build bypasses the shrink guard ---------------------------

def test_local_bundle_step_passes_allow_shrink(tmp_path: Path) -> None:
    """The canonical `13_build_bundle` step must carry --allow-shrink.

    Without it, any legitimate corpus shrink (a cleanup, a re-screen) leaves the
    export stage permanently failing with no UI affordance to clear it.
    """
    os.environ.setdefault("JWT_SECRET", "test-secret")
    from ber_core import pipeline
    from ber_core.projects import Project

    proj = Project(
        slug="t", config_path=tmp_path / "config.yaml",
        config={"topic": {"name": "T"}}, project_dir=tmp_path / "projects" / "t",
    )
    steps = pipeline.canonical_steps(proj)
    build = next(s for s in steps if s.id == "13_build_bundle")
    assert "--allow-shrink" in build.args, (
        "the LOCAL dashboard build must bypass the information-loss guard; "
        f"got {build.args}")


def test_publish_deploy_still_refuses_to_shrink(monkeypatch, tmp_path: Path) -> None:
    """The guard that actually matters must be untouched.

    Bypassing it for the local preview must not weaken publishing, which is what
    protects real published data.
    """
    os.environ.setdefault("JWT_SECRET", "test-secret")
    from server.app.services import publish_deploy

    ws = tmp_path / "ws"
    (ws / "projects" / "t" / "dashboard").mkdir(parents=True)
    (ws / "projects" / "t" / "dashboard" / "summary.json").write_text("{}")

    monkeypatch.setattr(publish_deploy.bundle_svc, "bundle_dir",
                        lambda _ws: ws / "projects" / "t" / "dashboard")
    monkeypatch.setattr(publish_deploy, "diff_against_live",
                        lambda _ws, _slug: {"removed_ids": ["10.1/a"],
                                            "shrunk_metrics": {"n_records": (30, 13)}})

    with pytest.raises(ValueError, match="Refusing to publish"):
        publish_deploy.deploy(ws, "t", jobtag="x")

    # ...and the explicit override still works (two-party approval path).
    monkeypatch.setattr(publish_deploy, "deployed_dir", lambda _s: tmp_path / "live")
    try:
        publish_deploy.deploy(ws, "t", jobtag="x", allow_shrink=True)
    except ValueError as exc:  # pragma: no cover - must not be the shrink refusal
        assert "Refusing to publish" not in str(exc)


# --- 2. The feed honors user_exclusions.json --------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_export_drops_manually_excluded_papers(tmp_path: Path) -> None:
    """A DOI in user_exclusions.json must not reach the dashboard feed.

    Uses the same shared loader `stage_eligible` and pool_meta_analysis read, so
    all three agree about who is in the review.
    """
    from ber_core.utils import load_exclusion_dois, norm_paper_id

    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "user_exclusions.json").write_text(
        json.dumps({"10.1/excluded": "user_excluded"}), encoding="utf-8")

    records = [{"paper_id": "10.1/kept"}, {"paper_id": "10.1/excluded"}]
    excluded = load_exclusion_dois(data / "user_exclusions.json")
    kept = [r for r in records if norm_paper_id(r["paper_id"]) not in excluded]

    assert [r["paper_id"] for r in kept] == ["10.1/kept"]


def test_organism_joins_from_pass1_and_never_invents_a_value(tmp_path: Path) -> None:
    """Organism comes from the pass-1 screen, not the pass-2 schema.

    A paper screened before the organism migration has no verdict; it must stay
    None so the UI renders "—" rather than a fabricated "human".
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_export_mod", REPO_ROOT / "steps/export_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_export_mod"] = mod
    spec.loader.exec_module(mod)

    pass1 = tmp_path / "trial_extractions.jsonl"
    _write_jsonl(pass1, [
        {"paper_id": "10.1/mouse",
         "screening": {"organism": {"verdict": "mouse", "reasoning": "…"}}},
        {"paper_id": "10.1/legacy", "screening": {}},          # pre-migration
    ])

    got = mod._load_organism(pass1)
    assert got == {"10.1/mouse": "mouse"}
    assert "10.1/legacy" not in got, "a missing verdict must not become a value"

    # A missing file is fine — reviews that never ran pass 1 still export.
    assert mod._load_organism(tmp_path / "nope.jsonl") == {}
