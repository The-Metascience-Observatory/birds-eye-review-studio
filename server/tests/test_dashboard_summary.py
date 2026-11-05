"""Precomputed dashboard aggregates (`dashboard_summary.py`).

Every number a Bird's Eye dashboard shows is computed once here, in Python, and
written to `summary.json`. The studio preview and the generated website page
both READ that file. The whole reason this module exists is that two renderers
computing their own aggregates eventually disagree — and the live dashboards
already prove it: `nInstruments` has two definitions in long-covid today (a
global distinct set in `page.tsx`, a sum of per-trial counts in `constants.ts`)
printing numbers 3x apart under one label.

So the load-bearing property is an EQUALITY: the precomputed charts must be
definitionally the same reduce a client would run over the same rows. Not
"close" — identical.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core import dashboard_summary as DS  # noqa: E402
from ber_core import grading as M  # noqa: E402

WEBSITE = REPO_ROOT.parent / "metascience_observatory_website"
DATA = WEBSITE / "data" / "birds_eye_reviews"

CORPORA = ("nattokinase_for_cardiovascular_disease_prevention", "long_covid",
           "restless_legs_syndrome", "antiviral_nasal_sprays")


def _records(slug: str) -> list[dict]:
    feed = DATA / slug / "trial_extractions.jsonl"
    if not feed.exists():
        # These tests read REAL published feeds, which live in the separate
        # website repository, not in this one.
        pytest.skip(f"needs the website repo's published feed for {slug} "
                    f"(expected at {feed}); clone it beside this repo to run")
    return [json.loads(l) for l in feed.open() if l.strip()]


def _built(slug: str):
    recs = _records(slug)
    rows, inclusion = DS.build_rows(recs)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    summary = DS.build_summary(
        recs, slug=slug, rows=rows, inclusion=inclusion,
        outcome_types=DS.derive_outcome_types(recs),
        feasibility=M.assess_poolability(recs),
        rows_bytes=len(text.encode("utf-8")))
    return recs, rows, summary


# --- THE property: precomputed == a fresh reduce ------------------------------

@pytest.mark.parametrize("slug", CORPORA)
def test_precomputed_charts_equal_a_fresh_reduce_over_the_rows(slug) -> None:
    """The studio-vs-website agreement guarantee.

    Each chart must be *definitionally* `count_distinct_trials(rows, facet)`,
    not an independent derivation that happens to agree today. If a client
    reduces the shipped rows with no filters active, it must reproduce these
    numbers exactly — that is what makes the two renderers incapable of
    disagreeing.
    """
    _, rows, s = _built(slug)
    for facet, chart, key in (
        ("countries", "by_country", "country"),
        ("designType", "by_design", "design_type"),
        ("rob", "by_rob", "rob"),
        ("verdict", "by_verdict", "verdict"),
        ("symptomDomains", "by_symptom_domain", "domain"),
        ("outcomeType", "by_outcome_type", "key"),
        ("interventionCategories", "by_intervention_category", "category"),
    ):
        expected = DS.count_distinct_trials(rows, facet)
        got = {e[key]: e["count"] for e in s["charts"][chart]}
        assert got == expected, f"{slug}/{chart} is not the reduce of {facet}"


@pytest.mark.parametrize("slug", CORPORA)
def test_a_chart_segment_count_equals_the_rows_a_click_would_show(slug) -> None:
    """The facet invariant, promoted from a warning to an assertion.

    The website enforces this only via a dev-only `console.warn` comparing two
    independently-built projections (long-covid/page.tsx:494-507). Shipping one
    canonical `facets` object per row makes it hold by construction: the number
    on a chart segment IS the number of rows a filter click selects.
    """
    _, rows, s = _built(slug)
    for entry in s["charts"]["by_country"]:
        matching = [r for r in rows
                    if entry["country"] in DS.facet_values(r["facets"], "countries")]
        assert len(matching) == entry["count"], entry


# --- the three resolved inconsistencies ---------------------------------------

def test_instrument_count_is_a_global_distinct_set_not_a_sum_of_per_trial_counts() -> None:
    """The bug: long-covid's `page.tsx` and `constants.ts` disagreed by 3x.

    A sum of per-trial distinct counts is not a count of instruments at all —
    it counts instrument *uses*. Measured on restless-legs: 529 distinct vs 1572
    summed. The distinct number is also what `assess_poolability` cites when it
    calls a topic fragmented, so the tile and the feasibility verdict now speak
    one vocabulary.
    """
    _, rows, s = _built("restless_legs_syndrome")
    distinct = len({i for r in rows for i in r["instruments"]})
    summed = sum(r["n_instruments"] for r in rows)
    assert s["totals"]["n_instruments"] == distinct
    assert distinct < summed, "corpus shares no instruments; test proves nothing"
    assert s["definitions"]["n_instruments"] == "global_distinct_normalized"


def test_instruments_are_normalized_before_being_counted() -> None:
    """Counting raw strings overstated fragmentation ~5x and wrongly called
    restless-legs unpoolable. The tile must not resurrect that."""
    _, rows, _ = _built("restless_legs_syndrome")
    instruments = {i for r in rows for i in r["instruments"]}
    assert "irls" in instruments
    assert not any(i != i.lower() for i in instruments), "un-normalized spelling leaked"


@pytest.mark.parametrize("slug", CORPORA)
def test_the_scalar_primary_domain_never_contradicts_the_list(slug) -> None:
    """Two definitions of `primaryDomain` existed (trio's scalar vs long-covid's
    all-primaries list). We emit BOTH — the table wants one, the facet wants all
    so a two-domain trial appears under both bars — but they must not disagree."""
    _, rows, _ = _built(slug)
    for r in rows:
        if r["primary_domains"]:
            assert r["primary_domain"] in r["primary_domains"], r["id"]


@pytest.mark.parametrize("slug", CORPORA)
def test_every_verdict_is_in_vocabulary_and_carries_its_source(slug) -> None:
    """Verdicts used to fall back all-or-nothing, so one bad CSV row discarded
    every good verdict in the file. Per-trial precedence is strictly more
    information — but only if the mixture is visible rather than hidden."""
    _, rows, s = _built(slug)
    for r in rows:
        assert r["verdict"] in DS.VERDICTS, r["id"]
        assert r["verdict_source"] in ("csv", "proxy", "none"), r["id"]
    assert sum(s["definitions"]["verdict_source_counts"].values()) == len(rows)


def test_an_unknown_effect_direction_is_never_read_as_a_positive_result() -> None:
    """The over-claiming failure this project exists to prevent: a significant
    p-value says nothing about WHO it favours when `higher_is_better` is unknown."""
    rec = {"_status": "ok", "paper_id": "10.1/x",
           "study_design": {"arms": [{"arm_id": 1, "type": "intervention"}]},
           "outcomes": [{"name": "x", "is_primary": True, "higher_is_better": None,
                         "between_group_effects": [{"effect_value": 5.0,
                                                    "p_value": 0.001}]}]}
    assert DS.structured_verdict(rec) == "insufficient_data"


# --- structural guarantees ----------------------------------------------------

@pytest.mark.parametrize("slug", CORPORA)
def test_every_dropped_record_is_counted_and_attributed(slug) -> None:
    """A silently dropped record makes the headline tiles unexplainable.
    Included + excluded must equal the input, with a reason for each."""
    recs, rows, s = _built(slug)
    inc = s["inclusion"]
    assert inc["n_input"] == len(recs)
    assert inc["n_included"] == len(rows)
    assert inc["n_input"] == inc["n_included"] + sum(inc["excluded"].values())
    assert all(v > 0 for v in inc["excluded"].values())


def test_the_exported_feed_is_not_rescreened() -> None:
    """Regression: re-applying `trial_screening.csv`'s `is_excluded` flag here
    dropped 27 of nattokinase's 30 papers — a second gate silently discarding
    90% of a feed the first gate deliberately kept. `is_excluded` is the
    eligibility gate for the SCREENING FLOW, not a post-export filter.
    """
    _, _, s = _built("nattokinase_for_cardiovascular_disease_prevention")
    assert "screened_out" not in s["inclusion"]["excluded"]
    assert s["inclusion"]["n_included"] >= 0.9 * s["inclusion"]["n_input"]


@pytest.mark.parametrize("slug", CORPORA)
def test_stacked_bars_sum_to_their_own_totals(slug) -> None:
    """A stacked bar whose segments don't sum to its own total is the classic
    chart lie. Both stacked charts are checked."""
    _, rows, s = _built(slug)
    for g in s["charts"]["by_intervention"]:
        assert sum(g["verdicts"].values()) == g["count"], g["name"]
    by_blinding = DS.count_distinct_trials(rows, "blinding")
    for b in s["charts"]["blinding_significance"]:
        total = b["significant"] + b["not_significant"] + b["unknown"]
        assert total == by_blinding[b["blinding"]], b


@pytest.mark.parametrize("slug", CORPORA)
def test_the_year_histogram_is_contiguous_and_accounts_for_missing_years(slug) -> None:
    """Zero-fill so a gap year is a visible trough rather than a missing bar
    that silently compresses the axis. Undated papers must be counted, not
    quietly dropped -- but a sparse year axis usually means the metadata
    backfill did not run (--no-metadata-fetch), NOT that the corpus lacks dates.
    Nattokinase reads 6/30 without the backfill and 29/29 with it.
    """
    _, rows, s = _built(slug)
    years = s["charts"]["by_year"]
    if years:
        span = [e["year"] for e in years]
        assert span == list(range(min(span), max(span) + 1)), "gap in the axis"
        assert sum(e["count"] for e in years) == s["totals"]["n_with_year"]
    assert s["totals"]["n_with_year"] <= len(rows)
    assert sum(s["definitions"]["year_source_counts"].values()) == len(rows)


@pytest.mark.parametrize("slug", CORPORA)
def test_totals_agree_with_the_shrink_guard_metrics(slug) -> None:
    """`chart_metrics` feeds the publish-time information-loss guard. If the
    tiles and the guard developed two truths, a real data loss could pass the
    guard while the dashboard displayed the drop (or vice versa)."""
    recs, rows, s = _built(slug)
    included_ids = {r["id"] for r in rows}
    guard = DS.chart_metrics([r for r in recs
                              if str(r.get("paper_id")) in included_ids])
    assert s["totals"]["n_trials"] == guard["n_records"]
    assert s["totals"]["n_rct"] == guard["n_rct"]
    assert s["totals"]["total_participants"] == guard["total_participants"]
    assert s["totals"]["n_countries"] == guard["n_countries"]


@pytest.mark.parametrize("slug", CORPORA)
def test_option_lists_contain_only_values_that_are_present(slug) -> None:
    """A dropdown offering a value that selects zero rows is a dead end."""
    _, rows, s = _built(slug)
    for facet, key in (("verdict", "verdicts"), ("rob", "rob"),
                       ("designType", "design_types"), ("countries", "countries")):
        present = set(DS.count_distinct_trials(rows, facet))
        assert set(s["vocab"]["present"][key]) == present, key


def test_fixed_vocabularies_keep_their_canonical_order() -> None:
    """`low` must precede `high` regardless of counts — a severity scale sorted
    by frequency is unreadable."""
    _, _, s = _built("restless_legs_syndrome")
    got = s["vocab"]["present"]["rob"]
    assert got == [v for v in DS.ROB_ORDER if v in set(got)]


# --- the size-adaptive contract ----------------------------------------------

@pytest.mark.parametrize("slug", CORPORA)
def test_loading_mode_is_written_not_left_to_the_reader(slug) -> None:
    """`mode` is computed once and recorded, so the two loading paths can never
    disagree about which one they are on. The threshold is echoed so no consumer
    hardcodes it.

    Measured surprise: long-covid's 22 MB feed projects to <1 MB of rows (66% of
    the feed is `outcomes` detail the rows don't carry), so EVERY current corpus
    is `inline`. The paged path is not needed yet — which is worth knowing
    before building it.
    """
    _, _, s = _built(slug)
    sz = s["sizing"]
    assert sz["inline_threshold_bytes"] == DS.INLINE_THRESHOLD_BYTES
    expected = "inline" if sz["rows_bytes"] <= sz["inline_threshold_bytes"] else "paged"
    assert sz["mode"] == expected


def test_summary_stays_small_enough_to_read_eagerly() -> None:
    """Every consumer reads summary.json unconditionally — the preview, the
    panel resolver, the publish gate, the tests. That is why rows live in a
    separate file. Long Covid is the worst case at ~200 KB."""
    _, _, s = _built("long_covid")
    assert len(json.dumps(s).encode("utf-8")) < 2_000_000


# --- the honesty blocks -------------------------------------------------------

def test_a_topic_that_cannot_pool_says_so_in_the_summary() -> None:
    """The dashboard renders the SAME feasibility numbers the publish gate reads,
    so the two can never disagree. For nattokinase the correct claim is that no
    meta-analysis is possible."""
    _, _, s = _built("nattokinase_for_cardiovascular_disease_prevention")
    assert s["feasibility"]["overall"] == "not_possible"
    assert "evidence map" in s["feasibility"]["recommendation"]


@pytest.mark.parametrize("slug", CORPORA)
def test_the_drift_probe_reports_through_the_summary(slug) -> None:
    """`validate_contract` is the codebase's only schema-drift probe and has
    only ever written to stderr inside a job log. summary.json is its output
    surface — deliberately not a second probe that could disagree."""
    recs, _, s = _built(slug)
    expected = DS.validate_contract(recs)
    for k in ("missing_keys", "unknown_rob", "unknown_arm_type"):
        assert s["contract"][k] == expected[k]
    assert s["contract"]["clean"] == (not any(expected[k] for k in expected))


def test_schema_version_is_pinned() -> None:
    """A literal, so bumping it is a deliberate act a reviewer sees in the diff."""
    _, _, s = _built("nattokinase_for_cardiovascular_disease_prevention")
    assert s["schema_version"] == 4


def test_every_row_carries_the_pico_block() -> None:
    """Schema v3: each row has `pico` with the four PICO keys, so the tables'
    Population / Intervention / Comparator / All-outcomes columns auto-populate
    from extraction without a second projection."""
    _, rows, _ = _built("nattokinase_for_cardiovascular_disease_prevention")
    assert rows, "corpus produced no rows"
    for row in rows:
        pico = row["pico"]
        assert set(pico) == {"population", "intervention", "comparator", "outcome"}
        assert isinstance(pico["intervention"], list)
        assert isinstance(pico["comparator"], list)
        assert isinstance(pico["outcome"], list)
