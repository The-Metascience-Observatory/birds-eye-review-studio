"""Correctness tests for meta_analysis.py.

Every numeric assertion is either a hand-computable closed form or a
well-established statistical property (verifiable against metafor). Run from
the repo root with ``python -m pytest tests/test_meta_analysis.py``.

The central fixture is a deliberately symmetric 3-study set whose DL/PM/REML
tau^2 all collapse to the same fixed point, so one dataset pins every estimator:

    y  = [0.0, 0.5, 1.0],  se = [0.2, 0.2, 0.2]   (v = 0.04, w = 25 each)
    fixed mean = 0.5
    Q          = 25 * (0.25 + 0 + 0.25) = 12.5   (df = 2)
    c          = 75 - 3*625/75 = 50
    tau^2 (DL) = (12.5 - 2)/50 = 0.21
    I^2        = (12.5 - 2)/12.5 = 84.0%
    RE weights = 1/(0.04+0.21) = 4 each; pooled = 0.5; SE = sqrt(1/12)
"""

import math

from ber_core import meta_analysis as ma

TOL = 1e-6
LOOSE = 1e-3

Y3 = [0.0, 0.5, 1.0]
SE3 = [0.2, 0.2, 0.2]
PTS3 = list(zip(Y3, SE3))


def _close(a, b, tol=TOL):
    assert abs(a - b) <= tol, f"{a} != {b} (tol {tol})"


# ── Distribution functions ───────────────────────────────────────────────────

def test_normal():
    _close(ma.norm_ppf(0.975), 1.959963985, LOOSE)
    _close(ma.norm_cdf(1.959963985), 0.975, LOOSE)
    _close(ma.norm_cdf(0.0), 0.5, TOL)


def test_student_t():
    _close(ma.t_ppf(0.975, 10), 2.228138852, LOOSE)
    _close(ma.t_ppf(0.975, 5), 2.570581836, LOOSE)
    _close(ma.t_ppf(0.975, 1), 12.7062047, 1e-2)
    _close(ma.t_cdf(0.0, 7), 0.5, TOL)


def test_chi2():
    # chi-square df=1: 3.8415 is the 0.05 critical value.
    _close(ma.chi2_sf(3.841458821, 1), 0.05, LOOSE)
    _close(ma.chi2_sf(18.30703805, 10), 0.05, LOOSE)
    # df=2 has closed form sf(x) = exp(-x/2).
    _close(ma.chi2_sf(12.5, 2), math.exp(-6.25), 1e-4)


# ── tau^2 estimators all agree on the symmetric fixture ──────────────────────

def test_tau2_estimators_agree():
    _close(ma.tau2_dl(Y3, SE3), 0.21, TOL)
    _close(ma.tau2_paule_mandel(Y3, SE3), 0.21, LOOSE)
    _close(ma.tau2_reml(Y3, SE3), 0.21, LOOSE)


def test_tau2_homogeneous_is_zero():
    ys = [0.3, 0.3, 0.3, 0.3]
    ses = [0.1, 0.15, 0.2, 0.25]
    _close(ma.tau2_dl(ys, ses), 0.0, TOL)
    _close(ma.tau2_reml(ys, ses), 0.0, 1e-4)
    _close(ma.tau2_paule_mandel(ys, ses), 0.0, 1e-4)


# ── Core pooling ─────────────────────────────────────────────────────────────

def test_pool_hand_values():
    r = ma.pool(PTS3, model="random", tau2_method="DL", ci_method="wald")
    _close(r["tau2"], 0.21, TOL)
    _close(r["q"], 12.5, TOL)
    _close(r["i2"], 84.0, 1e-1)
    _close(r["pooled"], 0.5, TOL)
    _close(r["pooled_se"], math.sqrt(1.0 / 12.0), TOL)
    _close(r["q_p"], math.exp(-6.25), 1e-4)
    # Wald CI = 0.5 +/- 1.959964 * SE
    half = 1.959963985 * math.sqrt(1.0 / 12.0)
    _close(r["ci_low"], 0.5 - half, LOOSE)
    _close(r["ci_high"], 0.5 + half, LOOSE)


def test_fixed_effect_zeroes_tau2():
    r = ma.pool(PTS3, model="fixed")
    _close(r["tau2"], 0.0, TOL)
    _close(r["pooled"], 0.5, TOL)
    # fixed SE = sqrt(1/sum w) = sqrt(1/75)
    _close(r["pooled_se"], math.sqrt(1.0 / 75.0), TOL)


def test_hksj_wider_than_wald():
    wald = ma.pool(PTS3, ci_method="wald")
    hksj = ma.pool(PTS3, ci_method="hksj")
    w_width = wald["ci_high"] - wald["ci_low"]
    h_width = hksj["ci_high"] - hksj["ci_low"]
    assert h_width > w_width, (h_width, w_width)
    assert hksj["ci_dist"] == "t"


def test_prediction_interval_wider_than_ci():
    r = ma.pool(PTS3, prediction_interval=True)
    assert r["prediction_low"] is not None
    ci_w = r["ci_high"] - r["ci_low"]
    pi_w = r["prediction_high"] - r["prediction_low"]
    assert pi_w > ci_w, (pi_w, ci_w)
    # No PI for k < 3.
    r2 = ma.pool(PTS3[:2], prediction_interval=True)
    assert r2["prediction_low"] is None


# ── Publication bias ─────────────────────────────────────────────────────────

def test_egger_symmetric_small_intercept():
    # Effect uncorrelated with precision (mild noise) => intercept near 0.
    pts = [(0.5, 0.1), (0.45, 0.2), (0.55, 0.3), (0.5, 0.4)]
    e = ma.eggers_test(pts)
    assert abs(e["intercept"]) < 0.8, e


def test_egger_detects_asymmetry():
    # Small (imprecise) studies carry larger effects: funnel asymmetry.
    pts = [(0.2, 0.05), (0.25, 0.06), (0.8, 0.3), (0.9, 0.35), (1.0, 0.4)]
    e = ma.eggers_test(pts)
    assert abs(e["intercept"]) > 1.5, e


def test_trim_fill_symmetric_imputes_nothing():
    pts = [(-0.5, 0.2), (-0.25, 0.2), (0.0, 0.2), (0.25, 0.2), (0.5, 0.2)]
    tf = ma.trim_and_fill(pts)
    assert tf["n_imputed"] == 0, tf


def test_trim_fill_asymmetric_imputes_left():
    # Right-skewed set (negative studies suppressed) => impute on the left.
    pts = [(0.0, 0.2), (0.25, 0.2), (0.5, 0.2), (0.4, 0.25), (0.6, 0.3)]
    tf = ma.trim_and_fill(pts)
    assert tf["n_imputed"] > 0, tf
    assert tf["side"] == "left", tf


# ── Subgroups ────────────────────────────────────────────────────────────────

def _mk_trial(y, se, subgroup, pid):
    return {"paper_id": pid, "first_author": pid, "year": 2020,
            "effect": y, "ci_low": y - 2 * se, "ci_high": y + 2 * se,
            "p_value": None, "n_total": 100, "outcome_name": "x",
            "_y": y, "_se": se, "_is_primary": True, "_subgroup": subgroup}


def test_subgroup_between_significant_when_divergent():
    trials = [_mk_trial(0.1, 0.1, "low", "a"), _mk_trial(0.15, 0.1, "low", "b"),
              _mk_trial(1.2, 0.1, "high", "c"), _mk_trial(1.3, 0.1, "high", "d")]
    opts = ma.MetaOptions(subgroup_by="rob").normalized()
    sg = ma._subgroups(trials, opts, log_scale=False)
    assert sg is not None and sg["q_between"] > 3.84
    assert sg["p_value"] < 0.05, sg


def test_subgroup_between_ns_when_similar():
    trials = [_mk_trial(0.5, 0.1, "low", "a"), _mk_trial(0.52, 0.1, "low", "b"),
              _mk_trial(0.48, 0.1, "high", "c"), _mk_trial(0.5, 0.1, "high", "d")]
    opts = ma.MetaOptions(subgroup_by="rob").normalized()
    sg = ma._subgroups(trials, opts, log_scale=False)
    assert sg is not None and sg["p_value"] > 0.05, sg


def test_unknown_moderator_is_reported_but_not_a_level_of_the_test():
    """Trials missing the moderator must not manufacture a subgroup difference."""
    similar = [_mk_trial(0.5, 0.1, "low", "a"), _mk_trial(0.52, 0.1, "low", "b"),
               _mk_trial(0.48, 0.1, "high", "c"), _mk_trial(0.5, 0.1, "high", "d")]
    divergent_unknown = [_mk_trial(3.0, 0.1, None, "e"), _mk_trial(3.1, 0.1, None, "f")]
    opts = ma.MetaOptions(subgroup_by="rob").normalized()
    sg = ma._subgroups(similar + divergent_unknown, opts, log_scale=False)
    assert sg["p_value"] > 0.05 and sg["df"] == 1, sg
    assert sg["n_unknown"] == 2
    unknown = [g for g in sg["groups"] if g["subgroup"] == "(unknown)"][0]
    assert unknown["in_between_test"] is False and unknown["n_trials"] == 2
    # One recorded level plus unknowns: groups reported, no test.
    only = ma._subgroups([_mk_trial(0.5, 0.1, "low", "a"), _mk_trial(1.5, 0.1, None, "b")],
                         opts, log_scale=False)
    assert only["q_between"] is None and len(only["groups"]) == 2


# ── End-to-end build_meta_analysis ───────────────────────────────────────────

def _record(pid, cat, md, se, hib=False, rob="low"):
    """A minimal RCT record with one continuous outcome reporting a mean_difference."""
    return {
        "paper_id": pid, "authors": f"{pid} A", "year": 2021,
        "synthesis_population": "adults with fatigue",
        "sample_sizes": {"n_randomized_total": 80},
        "risk_of_bias": {"overall_judgment": rob},
        "study_design": {"design_type": "RCT", "arms": [{"arm_id": 1, "type": "intervention", "intervention_name": cat},
                                   {"arm_id": 2, "type": "placebo"}]},
        "outcomes": [{
            "name": "Fatigue", "outcome_domain": "fatigue", "is_primary": True,
            "unit": "points", "synthesis_time_window": "week8", "analysis_basis": "endpoint", "analysis_population": "ITT",
            "higher_is_better": hib,
            "arm_results": [
                {"arm_id": 1, "timepoint_id": 2, "mean": 10.0 + md, "sd": 3.0, "n_analyzed": 40},
                {"arm_id": 2, "timepoint_id": 2, "mean": 10.0, "sd": 3.0, "n_analyzed": 40},
            ],
            "between_group_effects": [{
                "comparison": "arm1_vs_arm2", "effect_measure": "mean_difference", "effect_value": md,
                "ci_95_low": md - 1.959964 * se, "ci_95_high": md + 1.959964 * se,
                "se": se, "p_value": None,
            }],
        }],
    }


def test_build_meta_analysis_shape_and_backcompat():
    records = [_record("p1", "exercise", -0.4, 0.15, rob="low"),
               _record("p2", "exercise", -0.5, 0.15, rob="low"),
               _record("p3", "exercise", -0.2, 0.15, rob="high"),
               _record("p4", "exercise", -0.6, 0.2, rob="high")]
    opts = ma.MetaOptions(model="random", tau2_method="REML", ci_method="hksj",
                          prediction_interval=True, publication_bias=True,
                          leave_one_out=True, subgroup_by="risk_of_bias.overall_judgment")
    res = ma.build_meta_analysis(records, lambda r: "exercise", opts,
                                 group_field="intervention")
    assert res["options"]["tau2_method"] == "REML"
    md_groups = [g for g in res["groups"]
                 if g["effect_measure"] == "mean_difference" and g.get("pooled")]
    assert md_groups, "expected a pooled mean_difference group"
    g = md_groups[0]
    # Legacy keys still present (backward compatibility with the website).
    for key in ("effect", "ci_low", "ci_high", "i2", "model"):
        assert key in g["pooled"], key
    # New rigor keys present.
    for key in ("tau2", "tau", "q", "q_p", "df", "h2", "p_value",
                "prediction_low", "prediction_high"):
        assert key in g["pooled"], key
    assert "publication_bias" in g and "leave_one_out" in g and "subgroups" in g
    assert g["subgroups"]["by"] == "risk_of_bias.overall_judgment"
    # Common-unit outcomes stay mean differences; no automatic SMD companion.
    assert not any(g2["effect_measure"] == "smd" for g2 in res["groups"])


def test_default_options_reproduce_dl_random():
    """Defaults must equal the legacy DL random-effects Wald pooling."""
    records = [_record("p1", "exercise", 0.0, 0.2),
               _record("p2", "exercise", 0.5, 0.2),
               _record("p3", "exercise", 1.0, 0.2)]
    res = ma.build_meta_analysis(records, lambda r: "exercise", ma.MetaOptions(),
                                 group_field="intervention")
    g = [x for x in res["groups"]
         if x["effect_measure"] == "mean_difference" and x.get("pooled")][0]
    # Same hand-computed fixture: pooled 0.5, I^2 84, tau^2 0.21.
    _close(g["pooled"]["effect"], 0.5, LOOSE)
    _close(g["pooled"]["i2"], 84.0, 1e-1)
    _close(g["pooled"]["tau2"], 0.21, LOOSE)
    assert g["pooled"]["model"] == "random_effects"


# ── GRADE certainty / risk of bias ───────────────────────────────────────────

def _pooled_group(records):
    res = ma.build_meta_analysis(records, None, ma.MetaOptions(), group_field="intervention")
    groups = [g for g in res["groups"] if g.get("pooled")]
    assert len(groups) == 1, res["groups"]
    return groups[0]


def test_pooled_group_carries_grade_certainty_and_rob_mix():
    g = _pooled_group([_record(f"p{i}", "exercise", -0.5, 0.1, rob="low") for i in range(4)])
    cert = g["certainty"]
    assert cert["start"] == "high"
    assert cert["grade"] in {"high", "moderate", "low", "very_low"}
    assert all({"domain", "reason"} <= set(d) for d in cert["downgrades"])
    assert g["rob_summary"] == {"low": 4, "some": 0, "high": 0, "unknown": 0}
    assert g["total_n"] == 320          # 4 x 80 analysed
    assert all(t["rob"] == "low" for t in g["trials"])
    assert not any(d["domain"] == "risk_of_bias" for d in cert["downgrades"])


def test_high_risk_of_bias_studies_downgrade_certainty():
    low = _pooled_group([_record(f"p{i}", "exercise", -0.5, 0.1, rob="low") for i in range(4)])
    high = _pooled_group([_record(f"p{i}", "exercise", -0.5, 0.1, rob="high") for i in range(4)])
    assert any(d["domain"] == "risk_of_bias" for d in high["certainty"]["downgrades"])
    levels = ["high", "moderate", "low", "very_low"]
    assert levels.index(high["certainty"]["grade"]) == levels.index(low["certainty"]["grade"]) + 1


def test_rob_domain_judgments_are_parsed_in_any_shape():
    from ber_core import grading
    for dj in ("randomization:low, deviations:high", {"randomization": "low", "deviations": "high"},
               [{"domain": "randomization", "judgment": "high"}]):
        assert grading.parse_rob({"risk_of_bias": {"domain_judgments": dj}}) == "high"
    assert grading.parse_rob({"risk_of_bias": {"overall_judgment": "serious"}}) == "high"
    assert grading.parse_rob({"risk_of_bias": {"overall_judgment": "moderate"}}) == "some"
    assert grading.parse_rob({"risk_of_bias": None}) == "unknown"


def test_observational_pools_start_low_and_ratio_imprecision_uses_null_one():
    from ber_core import grading
    kw = dict(n_trials=10, total_n=5000, i2=0.0, null_value=1.0,
              rob={"low": 10}, egger={"p_value": 0.5, "df": 8})
    assert grading.grade_certainty(design_cls="observational", ci_low=0.5, ci_high=0.8, **kw)["grade"] == "low"
    rct = grading.grade_certainty(design_cls="crossover", ci_low=0.5, ci_high=0.8, **kw)
    assert rct["grade"] == "high" and rct["downgrades"] == []
    crossing = grading.grade_certainty(design_cls="rct", ci_low=0.8, ci_high=1.2, **kw)
    assert [d["domain"] for d in crossing["downgrades"]] == ["imprecision"]
    biased = grading.grade_certainty(design_cls="rct", ci_low=0.5, ci_high=0.8,
                                      **{**kw, "egger": {"p_value": 0.01, "df": 8}})
    assert [d["domain"] for d in biased["downgrades"]] == ["publication_bias"]


# ── Pooling identity ─────────────────────────────────────────────────────────

def test_canonical_intervention_name_is_the_pooling_identity():
    a = _record("p1", "Azelastine 0.1%", -0.5, 0.2)
    b = _record("p2", "azelastine nasal spray", -0.4, 0.2)
    canon = {"Azelastine 0.1%": "Azelastine", "azelastine nasal spray": "Azelastine"}
    split = ma.build_meta_analysis([a, b], None, ma.MetaOptions(), group_field="intervention")
    assert not any(g.get("pooled") for g in split["groups"])
    assert ma.attach_canonical_interventions([a, b], canon) == 2
    g = _pooled_group([a, b])
    assert g["intervention"] == "Azelastine" and g["n_trials"] == 2
    # An explicit protocol synthesis identity still wins over the canonical name.
    a["study_design"]["arms"][0]["synthesis_intervention"] = "azelastine (protocol)"
    names = {g["spray_type"] for g in ma.build_meta_analysis([a, b], None, ma.MetaOptions())["groups"]}
    assert names == {"azelastine (protocol)", "Azelastine"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
