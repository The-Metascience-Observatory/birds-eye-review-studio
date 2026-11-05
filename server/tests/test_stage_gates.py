"""Approval gates (services/stage_gates.py).

Two properties carry this module. First, the three axes stay separate: a stage
that is DONE on disk can simultaneously be BLOCKED by approval, and a flattened
status enum could not say so. Second, staleness must not cry wolf — the config
version bumps on every PATCH including no-op ones, so version comparison would
mark stages stale on saves that changed nothing, and a user who is shown a false
staleness badge learns to ignore the real ones.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="ber_gates_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import stage_gates as g  # noqa: E402


def _stage(slug, *, status="done", kind="run", requires_approval=True,
           skipped=False, config_keys=(), title=None):
    return {
        "slug": slug, "title": title or slug.title(), "kind": kind,
        "status": status, "requires_approval": requires_approval,
        "skipped": skipped, "config_keys": list(config_keys),
    }


def _approved(digest=None, keys=None, when=None):
    return SimpleNamespace(
        approved_at=when or datetime(2026, 7, 1, tzinfo=timezone.utc),
        approved_by="u1",
        approved_config_version=3,
        stage_config_digest=digest,
        decisions={"key_digests": keys} if keys else None,
    )


# --- the property a flattened enum cannot express ----------------------------

def test_a_finished_stage_can_still_be_blocked() -> None:
    """The normal wizard state: the work ran, an upstream human has not signed
    off. `status` and `gate` must both be reportable, which is the entire reason
    they are separate axes."""
    out = g.apply_gates(
        [_stage("search"), _stage("classify")], states={}, config={})
    assert out[1]["status"] == "done"
    assert out[1]["gate"]["state"] == "blocked"
    assert out[1]["gate"]["blocked_by"] == "search"


def test_approving_upstream_unblocks_exactly_one_stage() -> None:
    """Approval releases the next stage, not the whole pipeline."""
    stages = [_stage("search"), _stage("classify"), _stage("pdfs")]
    out = g.apply_gates(stages, {"search": _approved()}, {})
    assert [s["gate"]["state"] for s in out] == ["approved", "open", "blocked"]
    assert out[2]["gate"]["blocked_by"] == "classify"


def test_a_skipped_stage_does_not_block_what_follows() -> None:
    """An RCT-only review skips the observational pass. Blocking on a stage with
    no work would deadlock the wizard with nothing to approve."""
    stages = [_stage("search"), _stage("extract", skipped=True), _stage("meta")]
    out = g.apply_gates(stages, {"search": _approved()}, {})
    assert out[1]["gate"] is None
    assert out[2]["gate"]["state"] == "open"


def test_an_approval_free_stage_does_not_block() -> None:
    stages = [_stage("search", requires_approval=False), _stage("classify")]
    out = g.apply_gates(stages, {}, {})
    assert out[0]["gate"] is None
    assert out[1]["gate"]["state"] == "open"


def test_optional_synthesize_stages_do_not_block_meta_or_gate_export_on_them() -> None:
    """The Synthesize phase (meta) is optional: it never gates the next stage, so
    Meta is reachable straight after Extract, and Export is blocked by EXTRACT
    (the last real gate) — never by an optional stage. This is the fix for the
    user's "Blocked by Interventions & categories" dead end."""
    stages = [
        _stage("extract"),
        _stage("meta", requires_approval=False),
        _stage("export"),
    ]
    # Extract not yet approved: it is the sole gate; Synthesize stages never gate.
    out = g.apply_gates(stages, {}, {})
    by = {s["slug"]: s for s in out}
    assert by["extract"]["gate"]["state"] == "open"
    assert by["meta"]["gate"] is None
    assert by["export"]["gate"]["state"] == "blocked"
    assert by["export"]["gate"]["blocked_by"] == "extract"

    # Approve Extract → Export unblocks; Meta was never blocked either way.
    out2 = g.apply_gates(stages, {"extract": _approved()}, {})
    by2 = {s["slug"]: s for s in out2}
    assert by2["meta"]["gate"] is None
    assert by2["export"]["gate"]["state"] == "open"


# --- staleness must not cry wolf ---------------------------------------------

def test_editing_another_stages_config_does_not_stale_this_one() -> None:
    """The reason staleness is per-stage-key rather than per-config-version:
    editing the meta-analysis block must not invalidate the search approval."""
    cfg = {"search": {"q": 1}, "meta_analysis": {"model": "random"}}
    stage = _stage("search", config_keys=["search"])
    st = _approved(digest=g.config_digest(cfg, ["search"]))

    cfg2 = {"search": {"q": 1}, "meta_analysis": {"model": "fixed"}}
    out = g.apply_gates([stage], {"search": st}, cfg2)
    assert out[0]["gate"]["state"] == "approved"


def test_a_no_op_save_does_not_stale_anything() -> None:
    """ReviewConfig.version bumps on EVERY patch, including one that changes
    nothing. Comparing versions would fire here; comparing digests does not."""
    cfg = {"search": {"q": 1}}
    st = _approved(digest=g.config_digest(cfg, ["search"]))
    out = g.apply_gates([_stage("search", config_keys=["search"])],
                        {"search": st}, dict(cfg))
    assert out[0]["gate"]["state"] == "approved"


def test_a_real_edit_does_stale_the_owning_stage() -> None:
    cfg = {"search": {"q": 1}}
    st = _approved(digest=g.config_digest(cfg, ["search"]))
    out = g.apply_gates([_stage("search", config_keys=["search"])],
                        {"search": st}, {"search": {"q": 2}})
    assert out[0]["gate"]["state"] == "stale"


def test_editing_away_and_back_is_not_stale() -> None:
    """A digest compares VALUES, so a round trip is correctly a no-op — which a
    version counter could never report."""
    cfg = {"search": {"q": 1}}
    st = _approved(digest=g.config_digest(cfg, ["search"]))
    out = g.apply_gates([_stage("search", config_keys=["search"])],
                        {"search": st}, {"search": {"q": 1}})
    assert out[0]["gate"]["state"] == "approved"


def test_stale_names_the_key_that_moved() -> None:
    """A bare staleness badge is unactionable; naming the key is the point."""
    cfg = {"a": 1, "b": 2}
    keys = ["a", "b"]
    st = _approved(digest=g.config_digest(cfg, keys),
                   keys=g.key_digests(cfg, keys))
    out = g.apply_gates([_stage("s", config_keys=keys)], {"s": st}, {"a": 1, "b": 99})
    assert out[0]["gate"]["stale_keys"] == ["b"]


def test_stale_keys_are_empty_rather_than_guessed() -> None:
    """An approval predating per-key digests can only report THAT something
    moved. Listing every key would be a guess dressed as a fact."""
    cfg = {"a": 1}
    st = _approved(digest=g.config_digest(cfg, ["a"]))  # no key_digests
    out = g.apply_gates([_stage("s", config_keys=["a"])], {"s": st}, {"a": 2})
    assert out[0]["gate"]["state"] == "stale"
    assert out[0]["gate"]["stale_keys"] == []


def test_a_dotted_key_addresses_a_nested_value() -> None:
    cfg = {"models": {"classification": "haiku", "summaries": "sonnet"}}
    a = g.config_digest(cfg, ["models.classification"])
    b = g.config_digest({"models": {"classification": "haiku", "summaries": "opus"}},
                        ["models.classification"])
    assert a == b, "a sibling key must not affect this digest"


def test_a_stage_with_no_config_keys_never_goes_stale() -> None:
    st = _approved(digest=g.config_digest({}, []))
    out = g.apply_gates([_stage("triage", kind="curate")], {"triage": st},
                        {"anything": "changed"})
    assert out[0]["gate"]["state"] == "approved"


# --- `_other` is a drift alarm, not a wizard step ----------------------------

def test_other_never_gates_and_is_never_gated() -> None:
    """`_other` is ordered LAST regardless of where its steps really sit in the
    pipeline, so including it in the upstream walk would give the wrong answer
    for every stage after it.

    Note the fixture sets `requires_approval=True` deliberately: `resolve_stages`
    emits it as False today, so a test relying on that would pass even if the
    `_other` branch were deleted. Forcing the flag on isolates the branch itself.
    """
    stages = [_stage("_other", requires_approval=True, status="pending"),
              _stage("search")]
    out = g.apply_gates(stages, {}, {})
    assert out[0]["gate"] is None, "_other must never carry a gate"
    assert out[1]["gate"]["state"] == "open", "_other must not block"
    assert out[1]["gate"]["blocked_by"] is None


# --- run / approve permission -------------------------------------------------

def test_a_blocked_stage_cannot_run_and_says_why() -> None:
    out = g.apply_gates([_stage("search"), _stage("classify")], {}, {})
    ok, reason = g.can_run(out[1])
    assert not ok
    assert "search" in reason


def test_approving_a_stage_that_never_ran_is_refused() -> None:
    """Approval means "I reviewed the output". With no output there is nothing
    to review, and a rubber-stamped empty stage is worse than none."""
    ok, reason = g.can_approve(_stage("classify", status="pending"))
    assert not ok
    assert "run it before" in reason


def test_a_checkpoint_stage_is_approvable_with_no_artifacts() -> None:
    """setup/triage/meta produce no files by design; the user's judgement is the
    only evidence that exists, so requiring artifacts would make them
    unapprovable and deadlock the wizard."""
    for kind in ("setup", "curate", "review"):
        ok, _ = g.can_approve(_stage("x", status="n/a", kind=kind))
        assert ok, kind


# --- where the wizard lands ---------------------------------------------------

def test_landing_stage_prefers_unfinished_work() -> None:
    stages = [_stage("search"), _stage("classify", status="pending")]
    out = g.apply_gates(stages, {"search": _approved()}, {})
    assert g.next_stage(out) == "classify"


def test_landing_stage_stops_at_a_finished_but_unapproved_stage() -> None:
    """Sharper than artifacts alone: work that is done but unreviewed is exactly
    what the wizard exists to surface, so it must not be walked past."""
    stages = [_stage("search"), _stage("classify")]
    out = g.apply_gates(stages, {}, {})
    assert g.next_stage(out) == "search"


# --- background stages: run, but never land on them --------------------------

def test_a_finished_background_stage_is_skipped_when_choosing_where_to_land() -> None:
    """Fetching abstracts is a pure download with nothing to configure. Landing
    the user on it means watching a progress bar instead of doing the next real
    piece of work — so the wizard steps over it and shows its progress on the
    page the user IS on."""
    stages = [
        _stage("search"),
        {**_stage("abstracts", requires_approval=False), "autorun": True},
        _stage("classify"),
    ]
    out = g.apply_gates(stages, {"search": _approved()}, {})
    assert g.next_stage(out) == "classify"


def test_a_background_stage_that_has_NOT_run_is_still_offered() -> None:
    """Only skip once it is actually under way or finished. If the auto-start
    failed, the user must still be able to land on the page and see why."""
    stages = [
        _stage("search"),
        {**_stage("abstracts", status="pending", requires_approval=False),
         "autorun": True},
        _stage("classify"),
    ]
    out = g.apply_gates(stages, {"search": _approved()}, {})
    assert g.next_stage(out) == "abstracts"


def test_a_background_stage_never_blocks_what_follows() -> None:
    """It takes no approval, so an unapproved one must not gate the pipeline
    while the user is on another page, unable to see why."""
    stages = [
        {**_stage("abstracts", status="pending", requires_approval=False),
         "autorun": True},
        _stage("classify"),
    ]
    out = g.apply_gates(stages, {}, {})
    assert out[0]["gate"] is None
    assert out[1]["gate"]["state"] == "open"
