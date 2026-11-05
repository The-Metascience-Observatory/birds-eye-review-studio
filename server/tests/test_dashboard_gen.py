"""Dashboard generation (`generate_dashboard.py`).

A Bird's Eye Review is a dashboard covering all papers on a topic; a pooled
meta-analysis is an optional extra. The generator's job is to emit a dashboard
that is honest about what its data supports — so the load-bearing property is
that a forest plot is offered ONLY when pooling is actually supported, and every
skipped panel states why.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WEBSITE = REPO_ROOT.parent / "metascience_observatory_website"
DATA = WEBSITE / "data" / "birds_eye_reviews"


def _gen():
    """Import the numeric-prefixed script (not a valid module name)."""
    spec = importlib.util.spec_from_file_location(
        "gen_dashboard", REPO_ROOT / "steps/generate_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_dashboard"] = mod   # dataclasses needs it registered
    spec.loader.exec_module(mod)
    return mod


G = _gen()


def _records(slug: str):
    feed = DATA / slug / "trial_extractions.jsonl"
    if not feed.exists():
        # These tests read REAL published feeds, which live in the separate
        # website repository, not in this one.
        pytest.skip(f"needs the website repo's published feed for {slug} "
                    f"(expected at {feed}); clone it beside this repo to run")
    return [json.loads(l) for l in feed.open() if l.strip()]


def _spec(slug: str, panels=None):
    return G.TopicSpec(slug=slug, route=slug.replace("_", "-"), title="t",
                       description="d",
                       panels=list(panels or G.DEFAULT_PANELS) + ["forest_plots"])


def _validated_records(folder):
    records = [{"paper_id": str(i), "synthesis_population": "adults", "study_design": {
        "design_type": "RCT", "arms": [{"arm_id":1,"type":"intervention","intervention_name":"Exercise"}, {"arm_id":2,"type":"placebo"}]},
        "outcomes":[{"name":"Fatigue", "unit":"points", "synthesis_time_window":"8 weeks", "analysis_basis":"endpoint", "analysis_population":"ITT", "between_group_effects":[{"comparison":"arm1_vs_arm2","effect_measure":"mean_difference","effect_value":-1,"se":.3}]}]} for i in range(2)]
    from ber_core import meta_analysis
    (folder / "meta_analysis.json").write_text(json.dumps(meta_analysis.build_meta_analysis(records,None)))
    return records


# --- the property that matters ----------------------------------------------

def test_a_fragmented_topic_is_not_offered_a_forest_plot() -> None:
    """Long Covid has a 946 KB meta_analysis.json on disk and deliberately NO
    meta-analysis route: 402 trials spanning 766 instruments in one domain would
    pool into an estimate in no meaningful unit. Offering the panel because the
    file exists would resurrect exactly that mistake.
    """
    slug = "long_covid"
    recs = _records(slug)
    from ber_core import grading as M

    fz = M.assess_poolability(recs)
    assert fz["overall"] == "not_possible"
    rendered, skipped = G.resolve_panels(_spec(slug), DATA / slug, fz)

    assert "forest_plots" not in {p["key"] for p in rendered}
    reason = next(p["reason"] for p in skipped if p["key"] == "forest_plots")
    assert "not_possible" in reason, reason


def test_a_supported_topic_is_offered_a_forest_plot(tmp_path) -> None:
    """The converse: restless legs pools 225 of 360 papers with a dominant
    IRLS instrument, and has a real published meta-analysis."""
    slug = "validated_fixture"
    recs = _validated_records(tmp_path)
    from ber_core import grading as M

    fz = M.assess_poolability(recs)
    assert fz["overall"] == "supported"
    rendered, _ = G.resolve_panels(_spec(slug), tmp_path, fz)
    assert "forest_plots" in {p["key"] for p in rendered}


def test_pooling_data_without_the_panel_renders_nothing(tmp_path) -> None:
    """Producing the pooled data and DISPLAYING it are two separate switches.

    `generate_meta_analysis` makes the build write meta_analysis.json;
    the `forest_plots` panel makes the dashboard draw it. A review whose saved
    `dashboard.panels` list predates pooling has no forest_plots entry, so
    DashboardShell's `has("forest_plots")` gate stays false and the section never
    renders no matter how good the data is — which is exactly what happened: a
    real pooled blood-pressure estimate existed and the page showed nothing.

    Enabling pooling must therefore also add the panel.
    """
    slug = "validated_fixture"
    recs = _validated_records(tmp_path)
    from ber_core import grading as M

    fz = M.assess_poolability(recs)
    assert fz["overall"] == "supported", "fixture must be a poolable corpus"

    # An explicit panel list WITHOUT forest_plots — the shape a review created
    # before pooling was turned on carries.
    spec = G.TopicSpec(
        slug=slug, route=slug.replace("_", "-"), title="t", description="d",
        panels=[p for p in G.DEFAULT_PANELS if p != "forest_plots"],
    )
    rendered, _ = G.resolve_panels(spec, tmp_path, fz)
    assert "forest_plots" not in {p["key"] for p in rendered}, (
        "the panel must not appear unless the layout asks for it — the studio's "
        "pool action is what adds it"
    )


def test_nattokinase_is_an_evidence_map_with_no_pooling() -> None:
    """The no-meta-analysis archetype: nothing survives to be pooled.

    Asserts `n_pooled`, NOT `n_with_contrast`. This test reads the PUBLISHED
    website feed, so every legitimate republish can move the input counts: the
    corpus was 30 papers with 0 contrasts when this was written and is 12 papers
    with 5 contrasts after the 2026-09-23 refresh, which failed the old
    `n_with_contrast == 0` assertion without anything being wrong. A contrast
    that exists but carries no usable estimate or no uncertainty is still not
    poolable, which is the archetype -- so the stable fact is that nothing pools.
    """
    slug = "nattokinase_for_cardiovascular_disease_prevention"
    recs = _records(slug)
    from ber_core import grading as M

    fz = M.assess_poolability(recs)
    assert fz["overall"] == "not_possible"
    assert fz["n_pooled"] == 0
    assert fz["n_with_contrast"] < 2 or fz["blockers"], (
        "either too few contrasts to pool, or a recorded reason none is usable"
    )
    assert "evidence map" in fz["recommendation"]

    rendered, skipped = G.resolve_panels(_spec(slug), DATA / slug, fz)
    assert "forest_plots" not in {p["key"] for p in rendered}
    # Still a real dashboard: the evidence-map panels render.
    assert {"intervention_grid", "year_chart"} <= {p["key"] for p in rendered}


# --- structural guarantees ---------------------------------------------------

def test_every_skipped_panel_states_a_reason() -> None:
    """A panel that vanishes without explanation is the silent-default pattern
    this whole effort exists to remove."""
    slug = "nattokinase_for_cardiovascular_disease_prevention"
    from ber_core import grading as M

    _, skipped = G.resolve_panels(
        _spec(slug, panels=list(G.DEFAULT_PANELS) + ["heatmap"]),
        DATA / slug, M.assess_poolability(_records(slug)))
    assert skipped
    for p in skipped:
        assert p.get("reason"), f"{p['key']} skipped with no reason"


def test_a_panel_whose_data_is_missing_is_skipped_not_broken(tmp_path) -> None:
    """An absent data file must degrade, never raise."""
    fz = {"overall": "supported", "recommendation": "x"}
    rendered, skipped = G.resolve_panels(_spec("x", ["prisma"]), tmp_path, fz)
    assert rendered == []
    assert "needs prisma.json" in skipped[0]["reason"]


def test_an_unknown_panel_name_is_reported_not_ignored() -> None:
    fz = {"overall": "supported", "recommendation": "x"}
    _, skipped = G.resolve_panels(_spec("x", ["not_a_panel"]), DATA, fz)
    assert any(p["key"] == "not_a_panel" for p in skipped)


def test_outcome_types_are_derived_from_the_corpus() -> None:
    """The domain->bucket map is the most topic-specific thing in a dashboard;
    deriving it from observed data beats asking the user to invent it."""
    got = G.derive_outcome_types(_records("restless_legs_syndrome"))
    assert got, "no buckets derived"
    keys = {b["key"] for b in got}
    assert "symptom_severity" in keys
    for b in got:
        assert b["label"] and b["domains"]


def test_the_manifest_carries_the_honesty_numbers() -> None:
    """The dashboard renders the SAME feasibility numbers a checkpoint would
    gate on, so the two can never disagree."""
    slug = "nattokinase_for_cardiovascular_disease_prevention"
    recs = _records(slug)
    man = G.build_manifest(_spec(slug), DATA / slug, recs)
    fz = man["feasibility"]
    assert fz["overall"] == "not_possible"
    assert fz["n_papers"] == len([r for r in recs if r.get("_status") in (None, "ok")])
    assert fz["recommendation"]
    # And it tells the user what else they could add.
    assert man["available_panels"], "no add-panel options offered"


# --- the dashboard-editor layout contract ------------------------------------

def test_explicit_empty_panels_is_respected() -> None:
    """`panels: []` is a user who turned everything off — it must not snap back
    to the defaults (the old truthiness check did exactly that)."""
    spec = G.spec_from_config(
        {"topic": {"name": "X"}, "dashboard": {"panels": [], "columns": []}},
        Path("/nonexistent"))
    assert spec.panels == []
    assert spec.columns == []


def test_forest_is_not_auto_appended_when_user_owns_panels(tmp_path) -> None:
    """Once `dashboard.panels` exists in config, a missing forest_plots is a
    deliberate removal; the supported-topic auto-append must not resurrect it."""
    slug = "validated_fixture"
    recs = _validated_records(tmp_path)
    spec = _spec(slug, panels=["year_chart"])
    spec.panels = ["year_chart"]  # user-owned list without forest_plots
    G.maybe_auto_append_forest(
        spec, {"dashboard": {"panels": ["year_chart"]}}, recs, tmp_path)
    assert "forest_plots" not in spec.panels

    # Without user ownership the append still happens (supported + file exists).
    spec2 = _spec(slug, panels=["year_chart"])
    spec2.panels = ["year_chart"]
    G.maybe_auto_append_forest(spec2, {}, recs, tmp_path)
    assert "forest_plots" in spec2.panels


def test_manifest_carries_the_layout_contract() -> None:
    """layout_version gates the renderer's strict panel gating; layout_digest is
    what bundle_state compares; panels_requested is the PRE-resolve list the
    editor seeds from (a data-skipped panel must not vanish from config)."""
    from ber_core import utils as U

    slug = "nattokinase_for_cardiovascular_disease_prevention"
    recs = _records(slug)
    dash_cfg = {"panels": list(G.DEFAULT_PANELS), "title": "t"}
    spec = _spec(slug)  # includes forest_plots, which resolve_panels will skip
    man = G.build_manifest(spec, DATA / slug, recs, dashboard_cfg=dash_cfg)
    assert man["layout_version"] == 2
    assert man["layout_digest"] == U.dashboard_layout_digest(dash_cfg)
    assert man["panels_requested"] == spec.panels
    assert "forest_plots" in man["panels_requested"]  # requested, even if skipped
    assert man["results_columns"] == list(G.DEFAULT_COLUMNS)


def test_default_panels_reproduce_the_legacy_shell() -> None:
    """DEFAULT_PANELS must keep rendering exactly what the pre-editor
    DashboardShell showed unconditionally (plus the 3 historically gated charts
    and the manifest-only prisma key). Changing this set changes every review
    that never customized its dashboard.

    `stat_tiles` was deliberately dropped: the headline aggregate-count tiles are
    gone from the product, so the panel key, its registry entry and its component
    no longer exist."""
    assert "stat_tiles" not in G.DEFAULT_PANELS
    assert "stat_tiles" not in G.PANELS_BY_KEY
    assert set(G.DEFAULT_PANELS) == {
        "feasibility",
        "filter_design", "filter_rob", "filter_country", "filter_outcome_type",
        "intervention_grid", "year_chart",
        "design_breakdown", "rob_breakdown", "country_map", "outcome_domains",
        "results_table",
        "prisma",
    }
    # Every default key must exist in the registry (no silent unknown-skips).
    assert set(G.DEFAULT_PANELS) <= set(G.PANELS_BY_KEY)


def test_layout_digest_is_order_insensitive_for_keys_only() -> None:
    """Dict key order must not matter (yaml round-trips reorder); list order
    must (panel order is meaningful)."""
    from ber_core import utils as U

    a = U.dashboard_layout_digest({"panels": ["a", "b"], "title": "t"})
    b = U.dashboard_layout_digest({"title": "t", "panels": ["a", "b"]})
    assert a == b
    assert U.dashboard_layout_digest({"panels": ["b", "a"]}) != \
        U.dashboard_layout_digest({"panels": ["a", "b"]})
