"""The server-side atomic deploy (`services/publish_deploy.py`).

Publishing writes to the public site, so its guarantees are load-bearing: the
swap is atomic (no half-updated dashboard), a shrink is refused unless overridden,
and a republish that drops a file (meta_analysis.json) removes it from the live
dir rather than leaving a stale one the site would keep serving.

Everything runs against a TEMP website dir — no test touches the real site.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import bundle as bundle_svc  # noqa: E402
from server.app.services import publish_deploy as pd  # noqa: E402


def _bundle(workspace: Path, *, records: list[dict], with_meta: bool,
            n_trials: int, feasibility: str = "not_possible") -> None:
    """Build a minimal but structurally real dashboard bundle in `workspace`."""
    bdir = workspace / "projects" / "t" / "dashboard"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / bundle_svc.SUMMARY).write_text(json.dumps({
        "totals": {"n_trials": n_trials},
        "feasibility": {"overall": feasibility},
    }), encoding="utf-8")
    (bdir / "trial_extractions.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    if with_meta:
        (bdir / "meta_analysis.json").write_text("{}", encoding="utf-8")


def _point_website(monkeypatch, tmp_path: Path) -> Path:
    site = tmp_path / "site"
    monkeypatch.setattr(pd.settings, "WEBSITE_DIR", site)
    return site


def test_first_publish_has_no_diff_and_writes_the_feed(tmp_path, monkeypatch) -> None:
    _point_website(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    _bundle(ws, records=[{"paper_id": "a"}, {"paper_id": "b"}], with_meta=False, n_trials=2)

    assert pd.diff_against_live(ws, "topic") is None  # nothing deployed yet
    out = pd.deploy(ws, "topic", jobtag="j1")
    assert out["route"] == "topic"
    assert out["n_trials"] == 2
    feed = pd.deployed_dir("topic") / "trial_extractions.jsonl"
    assert feed.exists()
    assert sum(1 for _ in feed.open()) == 2


def test_republish_removes_a_dropped_file(tmp_path, monkeypatch) -> None:
    """meta_analysis.json present in v1, absent in v2 — the swap must delete it,
    or the site's existsSync gate keeps serving a forest plot the data dropped."""
    _point_website(monkeypatch, tmp_path)
    ws = tmp_path / "ws"

    _bundle(ws, records=[{"paper_id": "a"}, {"paper_id": "b"}], with_meta=True, n_trials=2)
    pd.deploy(ws, "topic", jobtag="j1")
    assert (pd.deployed_dir("topic") / "meta_analysis.json").exists()

    # v2: same trials, no meta file.
    import shutil
    shutil.rmtree(ws / "projects")
    _bundle(ws, records=[{"paper_id": "a"}, {"paper_id": "b"}], with_meta=False, n_trials=2)
    pd.deploy(ws, "topic", jobtag="j2")
    assert not (pd.deployed_dir("topic") / "meta_analysis.json").exists()


def test_a_shrink_is_refused_unless_overridden(tmp_path, monkeypatch) -> None:
    _point_website(monkeypatch, tmp_path)
    ws = tmp_path / "ws"

    _bundle(ws, records=[{"paper_id": "a"}, {"paper_id": "b"}, {"paper_id": "c"}],
            with_meta=False, n_trials=3)
    pd.deploy(ws, "topic", jobtag="j1")

    # v2 loses trial c.
    import shutil
    shutil.rmtree(ws / "projects")
    _bundle(ws, records=[{"paper_id": "a"}, {"paper_id": "b"}], with_meta=False, n_trials=2)

    diff = pd.diff_against_live(ws, "topic")
    assert "c" in diff["removed_ids"]

    with pytest.raises(ValueError, match="remove trials"):
        pd.deploy(ws, "topic", jobtag="j2")

    out = pd.deploy(ws, "topic", jobtag="j3", allow_shrink=True)
    assert out["shrink_overridden"] is True
    feed = pd.deployed_dir("topic") / "trial_extractions.jsonl"
    assert sum(1 for _ in feed.open()) == 2


def test_deploy_without_a_bundle_raises(tmp_path, monkeypatch) -> None:
    _point_website(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="No dashboard bundle"):
        pd.deploy(tmp_path / "empty", "topic", jobtag="j1")


def test_confirm_token_changes_when_the_bundle_changes(tmp_path, monkeypatch) -> None:
    """Approve carries the token; a rebuild between preflight and approve must
    invalidate it so an admin can't approve one diff and publish another."""
    _point_website(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    _bundle(ws, records=[{"paper_id": "a"}], with_meta=False, n_trials=1)
    t1 = pd.confirm_token(ws, "topic", None)

    import shutil
    shutil.rmtree(ws / "projects")
    # A different feed ⇒ different mtime + content ⇒ different token.
    _bundle(ws, records=[{"paper_id": "a"}, {"paper_id": "b"}], with_meta=False, n_trials=2)
    t2 = pd.confirm_token(ws, "topic", None)
    assert t1 != t2
