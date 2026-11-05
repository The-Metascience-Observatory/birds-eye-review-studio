"""Meta-analysis feasibility assessment (`grading.assess_poolability`).

A Bird's Eye Review is a dashboard covering all papers on a topic; a pooled
estimate is an optional extra many topics genuinely cannot support. The verdict
must therefore be honest in BOTH directions — it must not manufacture a diamond
for a literature that has none, and it must not withhold one from a literature
that plainly supports it.

The real CLI corpora under `projects/` are used when present (their expected
values were measured, not guessed); a synthetic mixed corpus stands in for the
live studio workspace an earlier version read by its private review id.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core import grading as M  # noqa: E402

CORPORA = {
    "long_covid": REPO_ROOT / "projects/long_covid/data/trial_extractions_pass2.jsonl",
    "restless_legs": REPO_ROOT
    / "projects/restless_legs_syndrome/data/trial_extractions_pass2.jsonl",
    "get_mecfs": REPO_ROOT / "projects/get_mecfs/data/trial_extractions_pass2.jsonl",
}


def _load(name):
    if name == "mixed":
        return _mixed_corpus()
    p = CORPORA[name]
    if not p.exists():
        pytest.skip(f"corpus not present: {p}")
    return [json.loads(l) for l in p.open()]


def _two_arm(pid, outcome, effect):
    return {
        "_status": "ok", "paper_id": pid,
        "study_design": {"design_type": "RCT", "arms": [
            {"arm_id": 1, "type": "intervention", "intervention_name": "Drug"},
            {"arm_id": 2, "type": "placebo"}]},
        "outcomes": [{"name": outcome, "higher_is_better": False, "arm_results": [],
                      "between_group_effects": [effect]}],
    }


def _mixed_corpus() -> list[dict]:
    """A small corpus mid-extraction: some comparisons with variance, one
    without, one single-arm study and one preclinical record — the mix a live
    review passes through, without depending on anyone's workspace."""
    with_se = {"comparison": "arm1_vs_arm2", "effect_measure": "mean_difference",
               "effect_value": -1.0, "se": 0.4}
    no_var = {"comparison": "arm1_vs_arm2", "effect_measure": "mean_difference",
              "effect_value": 1.2, "se": None, "ci_95_low": None, "ci_95_high": None}
    return [
        _two_arm("10.1/a", "Fibrinogen", with_se),
        _two_arm("10.1/b", "Fibrinogen", dict(with_se, effect_value=-0.6)),
        _two_arm("10.1/c", "Blood pressure", no_var),
        {"_status": "ok", "paper_id": "10.1/d",
         "study_design": {"arms": [{"arm_id": 1, "type": "intervention"}]},
         "outcomes": [{"name": "x", "higher_is_better": True}]},
        {"_status": "ok", "paper_id": "10.1/lab", "study_design": {"arms": []},
         "outcomes": [{"name": "kcat", "higher_is_better": True}]},
    ]


# --- instrument normalization: the thing that makes the verdict correct -------

def test_spellings_of_one_instrument_collapse_to_one_key() -> None:
    """Real extractions write the SAME scale a dozen ways. Counting raw strings
    overstated fragmentation ~5x and wrongly declared restless-legs unpoolable."""
    variants = [
        "IRLS",
        "International Restless Legs Syndrome Rating Scale (IRLS)",
        "IRLS (International RLS Study Group rating scale)",
        "international restless legs scale (irls)",
    ]
    keys = {M.normalize_instrument(v) for v in variants}
    assert keys == {"irls"}, keys


def test_genuinely_different_instruments_stay_apart() -> None:
    """Over-collapsing would hide real fragmentation, which is the opposite bug."""
    assert M.normalize_instrument("HADS depression subscale") != M.normalize_instrument(
        "PSQI (Pittsburgh Sleep Quality Index)"
    )
    assert M.normalize_instrument("") == ""


# --- the five corpora: each verdict was measured, not assumed -----------------

def test_mid_extraction_verdict_is_internally_consistent() -> None:
    """A live review's verdict walks not_possible -> thin -> supported ->
    fragmented as papers land, so pinning one verdict for a corpus in flux
    just re-breaks. Assert the invariants that must hold at EVERY stage of
    extraction instead, on a fixed mixed corpus."""
    got = M.assess_poolability(_load("mixed"))
    assert got["overall"] in ("not_possible", "thin", "fragmented", "supported")
    # Counts must cohere no matter how far extraction has progressed.
    assert 0 <= got["n_pooled"] <= got["n_outcomes"]
    assert 0 <= got["n_with_contrast"] <= got["n_papers"]
    # A paper cannot be poolable without reporting a comparison in the first
    # place — the containment that makes the two stats meaningfully different.
    assert got["n_with_contrast"] <= got["n_with_comparison"] <= got["n_papers"]
    assert got["recommendation"]


def test_the_evidence_map_case_states_pooling_is_impossible() -> None:
    """The verdict the nattokinase corpus used to carry, on a FIXED fixture so it
    cannot drift: nothing pools, and the card says so in plain language."""
    in_vitro = {
        "_status": "ok", "paper_id": "10.1/lab",
        "study_design": {"arms": []},          # preclinical: no arms at all
        "outcomes": [{"name": "kcat", "higher_is_better": True}],
    }
    got = M.assess_poolability([in_vitro])
    assert got["overall"] == "not_possible"
    assert got["n_pooled"] == 0
    assert not [d for d in got["domains"] if d["verdict"] == "poolable"]
    rec = got["recommendation"]
    assert "No meta-analysis is possible" in rec
    assert "evidence map" in rec
    assert got["blockers"], "a not_possible verdict must explain itself"


def test_reported_comparisons_are_not_undercounted_as_poolable_ones() -> None:
    """Regression: `n_with_contrast` counts papers with a POOLABLE outcome, but
    the UI labelled it "papers report a between-group comparison" — so a corpus
    reporting many comparisons that all lack variance rendered as "0 of N papers
    report a between-group comparison", which is false and sends the user off to
    fix their search instead of looking at the missing SDs."""
    effect_no_variance = {
        "_status": "ok",
        "paper_id": "10.1/x",
        "study_design": {"arms": [{"arm_id": 1, "type": "intervention"},
                                  {"arm_id": 2, "type": "control"}]},
        "outcomes": [{
            "name": "Fibrinogen", "higher_is_better": False,
            "arm_results": [],
            # A real reported comparison — but no SD/CI/SE, so unpoolable.
            "between_group_effects": [{"comparison": "arm1_vs_arm2",
                                       "effect_measure": "mean_difference",
                                       "effect_value": 1.26, "ci_95_low": None,
                                       "ci_95_high": None, "se": None,
                                       "p_value": 0.05}],
        }],
    }
    got = M.assess_poolability([effect_no_variance])
    assert got["n_pooled"] == 0
    assert got["n_with_contrast"] == 0        # correctly not poolable
    assert got["n_with_comparison"] == 1      # but it IS a reported comparison
    # The old text asserted no paper reported a comparison. The new text must
    # instead name the real obstacle: the effect has no variance attached.
    rec = got["recommendation"]
    assert "none of the 1 extracted papers report a usable comparison" not in rec
    assert "standard deviation" in rec


@pytest.mark.parametrize("name", ["long_covid", "restless_legs", "get_mecfs"])
def test_legacy_corpora_require_explicit_scientific_semantics(name):
    got = M.assess_poolability(_load(name))
    assert got["overall"] == "not_possible"
    assert got["withheld"] and all(s["issues"] for s in got["withheld"])
    assert any(i["code"] == "unresolved_population" for s in got["withheld"] for i in s["issues"])


# --- structural guarantees ---------------------------------------------------

@pytest.mark.parametrize("name", ["mixed", "get_mecfs"])
def test_every_unpooled_outcome_is_attributed_to_a_blocker(name) -> None:
    """Nothing may be silently dropped — the ethic the dashboards already state
    ("never silently dropped"). Pooled + blocked must equal the total. The
    synthetic "mixed" corpus always runs; a real corpus runs when present."""
    got = M.assess_poolability(_load(name))
    blocked = sum(b["n"] for b in got["blockers"])
    assert blocked == sum(len(s["issues"]) for s in got["withheld"]), name
    assert len({(s["paper_id"], s["outcome_name"]) for s in got["withheld"]}) <= got["n_outcomes"]


def test_blockers_distinguish_cannot_from_missing() -> None:
    """`single_arm_no_contrast` is correct exclusion; `no_variance` is recoverable
    data. Collapsing them would misdescribe the literature."""
    single = {
        "_status": "ok", "paper_id": "10.1/a",
        "study_design": {"arms": [{"arm_id": 1, "type": "intervention"}]},
        "outcomes": [{"name": "x", "higher_is_better": True}],
    }
    got = M.assess_poolability([single])
    assert "no_comparator_role" in {b["code"] for b in got["blockers"]}
    assert got["overall"] == "not_possible"


def test_an_empty_corpus_does_not_crash_or_over_claim() -> None:
    got = M.assess_poolability([])
    assert got["overall"] == "not_possible"
    assert got["n_papers"] == 0
    assert got["recommendation"]


def test_the_recommendation_is_plain_language() -> None:
    """It is rendered verbatim to a non-statistician, so it must be a sentence,
    not a stat dump."""
    for name in ("mixed", *CORPORA):
        rec = M.assess_poolability(_load(name))["recommendation"]
        assert len(rec) > 80 and rec.endswith((".", ")")), name
        assert "None" not in rec and "{" not in rec, f"unrendered value in: {rec}"


# --- the studio service wrapper: reads a workspace, never raises ---------------

from server.app.services import feasibility as feas_svc  # noqa: E402


def test_no_data_dir_returns_the_honest_not_possible_shape(monkeypatch) -> None:
    """A review not set up yet must produce the SAME schema the card renders, so
    the client never special-cases a missing block."""
    monkeypatch.setattr(feas_svc, "_data_dir", lambda w: None)
    got = feas_svc.build_feasibility(Path("/nonexistent"))
    assert got["overall"] == "not_possible"
    assert got["n_papers"] == 0
    assert got["recommendation"]  # non-empty, so the card has something to say
    # Same keys assess_poolability yields, so FeasibilityCard reads no undefined.
    assert set(got) >= {
        "overall", "n_papers", "n_with_contrast", "n_outcomes",
        "n_pooled", "domains", "blockers", "recommendation",
    }


def test_no_extraction_yet_explains_it_and_does_not_crash(tmp_path, monkeypatch) -> None:
    """Data dir exists but no pass-2 file — the pre-extraction state."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(feas_svc, "_data_dir", lambda w: data)
    got = feas_svc.build_feasibility(tmp_path)
    assert got["overall"] == "not_possible"
    assert "extraction" in got["recommendation"].lower()


def test_reads_pass2_records_and_delegates_to_assess_poolability(
    tmp_path, monkeypatch
) -> None:
    """The single-arm record produces the same verdict through the service as it
    does through assess_poolability directly — proof the reader wires up."""
    data = tmp_path / "data"
    data.mkdir()
    rec = {
        "_status": "ok", "paper_id": "10.1/a",
        "study_design": {"arms": [{"arm_id": 1, "type": "intervention"}]},
        "outcomes": [{"name": "x", "higher_is_better": True}],
    }
    (data / "trial_extractions_pass2.jsonl").write_text(
        json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr(feas_svc, "_data_dir", lambda w: data)

    got = feas_svc.build_feasibility(tmp_path)
    assert got == M.assess_poolability([rec])


def test_a_half_written_final_line_is_skipped_not_fatal(tmp_path, monkeypatch) -> None:
    """The evidence table's tolerance to torn lines must hold here too — this
    endpoint is polled WHILE extraction is appending."""
    data = tmp_path / "data"
    data.mkdir()
    good = {
        "_status": "ok", "paper_id": "10.1/a",
        "study_design": {"arms": [{"arm_id": 1, "type": "intervention"}]},
        "outcomes": [{"name": "x", "higher_is_better": True}],
    }
    (data / "trial_extractions_pass2.jsonl").write_text(
        json.dumps(good) + "\n" + '{"paper_id": "10.1/b", "outc',  # torn
        encoding="utf-8")
    monkeypatch.setattr(feas_svc, "_data_dir", lambda w: data)
    got = feas_svc.build_feasibility(tmp_path)
    assert got["n_papers"] == 1  # only the complete record counted
