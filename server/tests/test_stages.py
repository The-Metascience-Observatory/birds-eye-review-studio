"""Wizard stage registry (services/stages.py).

The load-bearing property is COVERAGE: every canonical step must belong to
exactly one stage. A step claimed by no stage would still run in a full pipeline
run but be invisible in the wizard — the user would be charged for work they
never saw and could never approve. A step claimed by two stages would let the
same work be approved twice with different outcomes.
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_stages_"))
# DATABASE_URL / REDIS_URL come from conftest (settings caches them on first read).
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import stages as st  # noqa: E402
from server.app.services.stage_gates import next_stage  # noqa: E402
from server.app.services import workspace as ws  # noqa: E402


_ws_counter = itertools.count()


def _workspace(**config_overrides) -> Path:
    """A real review workspace on disk (no artifacts, so every step is pending).

    Each call gets a UNIQUE review id: workspaces are keyed by it, and two tests
    sharing one directory would see each other's artifacts.
    """
    n = next(_ws_counter)
    cfg = ws.build_config_dict(
        topic_name=f"Stage Test {n}",
        synonyms=["thing"],
        description="test",
        not_about=[],
        include_observational=config_overrides.pop("include_observational", True),
    )
    cfg.update(config_overrides)
    return ws.create_review_workspace(f"stagetest{n}", cfg)


def test_slugs_are_unique_and_url_safe() -> None:
    slugs = [s.slug for s in st.STAGES]
    assert len(slugs) == len(set(slugs)), "duplicate stage slug"
    for slug in slugs:
        assert slug.replace("-", "").isalnum(), f"{slug!r} is not URL-safe"
        assert slug == slug.lower()


def test_no_step_id_is_claimed_by_two_stages() -> None:
    seen: dict[str, str] = {}
    for stage in st.STAGES:
        for sid in stage.step_ids:
            assert sid not in seen, (
                f"step {sid!r} claimed by both {seen[sid]!r} and {stage.slug!r}"
            )
            seen[sid] = stage.slug


def test_every_stage_has_a_known_phase() -> None:
    """The phase field is presentation-only, but it must stay a closed set.

    A stage with a phase outside PHASES would render into no column of the
    stepper and vanish from the UI — the exact silent-drop failure the stage
    registry exists to prevent, one axis over.
    """
    for stage in st.STAGES:
        assert stage.phase in st.PHASE_SLUGS, (
            f"stage {stage.slug!r} has phase {stage.phase!r} not in {sorted(st.PHASE_SLUGS)}"
        )


def test_phases_partition_the_stage_list() -> None:
    """Every phase is non-empty and every stage lands in exactly one — so the
    stepper columns and the stage list are two views of the same set."""
    by_phase: dict[str, list[str]] = {p: [] for p in st.PHASE_SLUGS}
    for stage in st.STAGES:
        by_phase[stage.phase].append(stage.slug)
    for slug, _label in st.PHASES:
        assert by_phase[slug], f"phase {slug!r} has no stages"
    # No stage counted twice, and the union is the whole registry.
    assert sum(len(v) for v in by_phase.values()) == len(st.STAGES)


def test_phase_order_is_the_declared_order() -> None:
    """Stages never appear out of phase order: the phase of stage N is at or
    after the phase of stage N-1 in PHASES. The stepper reads left to right, so
    a stage in an earlier phase sitting below a later one would look like a bug."""
    order = {slug: i for i, (slug, _label) in enumerate(st.PHASES)}
    seen = -1
    for stage in st.STAGES:
        idx = order[stage.phase]
        assert idx >= seen, (
            f"stage {stage.slug!r} (phase {stage.phase!r}) precedes an earlier phase"
        )
        seen = idx


def test_the_synthesize_stages_are_optional_and_never_gate() -> None:
    """meta is optional/enrichment work: it is not a hard prerequisite for what
    follows (it reads the extraction output directly), so it must be
    requires_approval=False. Otherwise the positional gate walk traps the user
    before Export — the same class of bug as the old "Blocked by Interventions &
    categories" dead end."""
    by_slug = {s.slug: s for s in st.STAGES}
    for slug in ("meta", "report"):
        assert by_slug[slug].requires_approval is False, (
            f"{slug!r} must not gate the phases after it"
        )
    # The dashboard stage IS still a real gate (it bundles the extraction
    # results). Its slug was renamed export -> view-dashboard.
    assert by_slug["view-dashboard"].requires_approval is True


def test_the_report_stage_owns_the_opt_in_terminal_review_step() -> None:
    """The final document stage owns INSPECT-SR when explicitly configured —
    as an OPTIONAL step, so its absence does not skip the stage."""
    report = st.STAGES[-1]
    assert report.slug == "report"
    assert report.step_ids == ()
    assert report.optional_steps == ("17_systematic_review",)
    assert report.hidden is False
    assert report.phase == "publish"
    assert "synthesis" in report.config_keys
    assert "systematic_review" in report.config_keys


def test_resolve_stages_emits_phase() -> None:
    workspace = _workspace(include_observational=True)
    for s in st.resolve_stages(workspace):
        assert s["phase"] in st.PHASE_SLUGS


@pytest.mark.parametrize("include_observational", [True, False])
def test_every_canonical_step_belongs_to_a_stage(include_observational: bool) -> None:
    """No orphans: the '_other' bucket must stay empty for a normal review.

    resolve_stages appends an "_other" stage when a canonical step is claimed by
    nobody, precisely so this is visible rather than silent. Its presence here
    means the registry drifted from pipeline.canonical_steps().
    """
    workspace = _workspace(include_observational=include_observational)
    resolved = st.resolve_stages(workspace)
    orphans = [s for s in resolved if s["slug"] == "_other"]
    assert not orphans, f"canonical steps claimed by no stage: {orphans[0]['step_ids']}"


def test_rct_only_review_skips_the_observational_step() -> None:
    """A declared step absent from THIS review resolves away rather than blocking."""
    workspace = _workspace(include_observational=False)
    by_slug = {s["slug"]: s for s in st.resolve_stages(workspace)}
    assert "9_pass2_observational" not in by_slug["extract"]["step_ids"]
    assert "9_pass2_rct" in by_slug["extract"]["step_ids"]
    # The stage still has work to do, so it must NOT be marked skipped.
    assert by_slug["extract"]["skipped"] is False


def test_status_is_per_step_not_per_script() -> None:
    """Status is per canonical STEP, not per script. `classify.py` backs both
    `3_classify_quick` and `3_classify_screen` in the ONE `classify` stage: a
    finished merged screen (classifications.csv) with the trial-screen unrun must
    make the stage `partial`, not `done` — keying on the shared script would say
    the whole classify stage finished the moment the quick pass wrote its CSV.
    """
    workspace = _workspace()
    project = list((workspace / "projects").iterdir())[0]
    data = project / "data"
    data.mkdir(parents=True, exist_ok=True)
    # Only the merged abstract screen has run (quick → classifications.csv); the
    # trial-screen (trial_screening.csv) has not.
    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type\n10.1/x,yes,Clinical Trial\n", encoding="utf-8")

    by_slug = {s["slug"]: s for s in st.resolve_stages(workspace)}
    assert by_slug["classify"]["status"] == "partial", (
        "classify: quick done, trial-screen + enrich not — must be partial"
    )


def test_screen_stage_shows_no_step_count_details() -> None:
    """The classify (Screen) stage header is intentionally bare: no blurb and no
    "relevance: N | types: N | screened: N" counts, which read as noise. Its
    details list is always empty regardless of which steps have run."""
    workspace = _workspace()
    project = list((workspace / "projects").iterdir())[0]
    data = project / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type\n10.1/x,yes,Clinical Trial\n", encoding="utf-8")

    by_slug = {s["slug"]: s for s in st.resolve_stages(workspace)}
    assert by_slug["classify"]["details"] == []
    assert by_slug["classify"]["description"] == ""


def test_report_stage_is_reachable_without_the_opt_in_step() -> None:
    """C4: `17_systematic_review` only exists when systematic_review.enabled.
    Declared as a required step, its absence marked the whole report stage
    `skipped` — hidden, never landed on — although the page's real work (the
    narrative synthesis) needs no canonical step at all."""
    workspace = _workspace()
    by_slug = {s["slug"]: s for s in st.resolve_stages(workspace)}
    assert by_slug["report"]["skipped"] is False
    assert by_slug["report"]["step_ids"] == []


def test_report_stage_claims_the_opt_in_step_when_enabled() -> None:
    import yaml

    workspace = _workspace()
    cfg_path = workspace / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["systematic_review"] = {"enabled": True}
    cfg_path.write_text(yaml.safe_dump(cfg))
    by_slug = {s["slug"]: s for s in st.resolve_stages(workspace)}
    assert by_slug["report"]["step_ids"] == ["17_systematic_review"]
    assert by_slug["report"]["skipped"] is False
    assert "_other" not in by_slug


def test_current_stage_is_the_first_unfinished_stage() -> None:
    workspace = _workspace()
    resolved = st.resolve_stages(workspace)
    # Nothing has run: the first stage with real work is "search".
    assert next_stage(resolved) == "search"


def test_the_abstracts_stage_is_hidden_and_never_landed_on() -> None:
    """`abstracts` runs in the background (autorun) but is not a walkthrough
    stage: it is marked hidden and next_stage skips it, so the
    wizard never opens on a "watch the fetch" page."""
    import server.app.services.stage_gates as gates

    workspace = _workspace()
    resolved = st.resolve_stages(workspace)
    by_slug = {s["slug"]: s for s in resolved}
    assert by_slug["abstracts"]["hidden"] is True
    assert by_slug["abstracts"]["autorun"] is True  # still auto-runs
    # It owns its canonical step (so no _other orphan) but is never a landing.
    assert "2_fetch_abstracts" in by_slug["abstracts"]["step_ids"]
    assert gates.next_stage(resolved) != "abstracts"
    # Even when abstracts has "run", it is still skipped as a landing target.
    for s in resolved:
        if s["slug"] == "abstracts":
            s["status"] = "done"
    assert gates.next_stage(resolved) != "abstracts"


def test_the_extract1_stage_is_hidden_and_never_landed_on() -> None:
    """`extract1` (the full-text eligibility screen) runs as a BACKGROUND job now,
    not a walkthrough step: it is hidden and never a landing target, and it does
    NOT require approval (a hidden required-approval stage would block the extract stage
    downstream with no UI to clear it). Its steps left the canonical range (they're
    the bg job's now), so it owns no step_ids — it stays only to own the pass-1
    config keys. autorun stays False — the run is the bg job, not stage-autorun."""
    import server.app.services.stage_gates as gates

    workspace = _workspace()
    resolved = st.resolve_stages(workspace)
    by_slug = {s["slug"]: s for s in resolved}
    assert by_slug["extract1"]["hidden"] is True
    assert by_slug["extract1"]["autorun"] is False
    assert by_slug["extract1"]["requires_approval"] is False
    # Steps left canonical → no step_ids, and it still owns the pass-1 config keys.
    assert by_slug["extract1"]["step_ids"] == []
    assert "extraction_pass1_fields" in by_slug["extract1"]["config_keys"]
    # A step-less stage is not "skipped" (skipped means declared-but-vanished).
    assert by_slug["extract1"]["skipped"] is False
    # Never a landing target, even once it has "run".
    assert gates.next_stage(resolved) != "extract1"
    for s in resolved:
        if s["slug"] == "extract1":
            s["status"] = "done"
    assert gates.next_stage(resolved) != "extract1"
    # No _other orphan: 8_extract_pass1/stage_eligible left canonical entirely.
    assert "_other" not in by_slug
    # extract (Extract) keeps the "extract" phase non-empty + visible.
    assert by_slug["extract"]["hidden"] is False
    assert by_slug["extract"]["phase"] == "extract"


def test_stage_config_keys_reference_real_config_paths() -> None:
    """config_keys drive scoped staleness later; typos would silently never match."""
    cfg = ws.build_config_dict(
        topic_name="Key Check",
        synonyms=[],
        description="",
        not_about=[],
        include_observational=True,
    )
    # Keys the seed config is expected to contain at their top level.
    # NOTE: "search" is deliberately absent — the seed config has no queries
    # until they are drafted, so it cannot be asserted against a fresh config.
    seeded = {"topic", "article_types", "include_observational"}
    for stage in st.STAGES:
        for key in stage.config_keys:
            top = key.split(".")[0]
            if top in seeded:
                assert top in cfg, f"{stage.slug}: config key {key!r} not in seed config"
