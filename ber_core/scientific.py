"""Scientific selection contract shared by extraction, synthesis and presentation.

Raw extraction is evidence, never silently corrected here. A rejected estimate
does not reject its study. Broad taxonomy domains are deliberately NOT pool keys.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
import html
from statistics import NormalDist

from ber_core.constants import is_failed_status

RULES_VERSION = "2026-10-03.1"
CONTROL_TYPES = {"placebo", "control", "usual_care", "comparator", "active_comparator",
                 "active_placebo", "sham", "waitlist", "no_treatment", "standard_care",
                 "unexposed", "healthy_control", "historical_control", "pre_treatment"}
RATIOS = {"risk_ratio", "odds_ratio", "hazard_ratio", "rate_ratio", "incidence_rate_ratio"}
MEASURES = RATIOS | {"mean_difference", "smd", "risk_difference", "rate_difference"}
Z95 = NormalDist().inv_cdf(.975)


def objects(value):
    return [v for v in value or [] if isinstance(v, dict)]


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def key(v):
    return re.sub(r"\s+", " ", str("" if v is None else v).strip().casefold())


def measure(v):
    return key(v).removeprefix("adjusted_").replace("standardized_mean_difference", "smd")


def issue(code, path, reason, node=None, record=None, severity="blocking"):
    node, record = node or {}, record or {}
    return {"code": code, "path": path, "reason": reason, "severity": severity,
            "source_file": record.get("_source_file"),
            "source_location": node.get("source_location"),
            "source_quote": node.get("effect_quote") or node.get("result_quote"),
            "extraction_version": record.get("_extraction_version") or record.get("_extracted_at") or "legacy-unversioned",
            "rules_version": RULES_VERSION}


# ── Design classes: THE definition of randomized vs not ──────────────────────
# Every consumer (pooling keys, GRADE starting level, the dashboard's is_rct
# flag, pass-2 bucket routing) classifies designs through `design_label` /
# `design` / `design_class` below. They used to be five separate definitions
# that disagreed (quasi-experimental was an RCT to the stager and observational
# to the pooler; a randomized human-challenge trial was the reverse).
#
# Fine classes: rct | cluster_rct | crossover | observational | unknown.
RANDOMIZED_CLASSES = frozenset({"rct", "cluster_rct", "crossover"})

# Labels that explicitly deny randomization. Checked first: "randomized" is a
# substring of "non-randomized", and quasi-randomization (alternation, date of
# birth, record number) is NOT random allocation.
_NOT_RANDOMIZED = ("non random", "nonrandom", "not random", "quasi", "single arm",
                   "uncontrolled")
# Labels that name the SETTING or a generic trial type rather than the
# allocation: they say nothing about randomization either way, so the record's
# own allocation evidence decides.
_SETTING_LABELS = {"human challenge", "challenge", "controlled trial", "clinical trial",
                   "interventional", "trial", "pilot", "pilot trial", "phase 1", "phase 2",
                   "phase 3", "phase 4"}
_UNRESOLVED_LABELS = {"", "unclear", "unknown", "other", "not reported", "not applicable",
                      "n/a", "na", "none"}
_NO_EVIDENCE = {"", "null", "none", "n/a", "na", "not reported", "not_reported", "unclear",
                "unknown", "not applicable", "not stated"}
_CONCEALMENT_EVIDENCE = {"central", "sealed envelopes", "sealed_envelopes", "pharmacy",
                         "sequentially numbered", "opaque envelopes", "web based", "telephone"}


def has_randomization_evidence(study_design):
    """Does the record itself document random allocation?

    A described randomization procedure, or a real allocation-concealment
    mechanism (not "unclear"). Used only when the design label is silent about
    allocation; an explicit "non-randomized" label always wins.
    """
    sd = study_design or {}
    method = key(sd.get("randomization_method"))
    if method and method not in _NO_EVIDENCE and key(sd.get("randomization_method_source")) != "not_reported":
        return True
    concealment = key(sd.get("allocation_concealment"))
    return concealment in _CONCEALMENT_EVIDENCE


def design_label(label, randomized_evidence=False):
    """Classify one free-text design label into a fine design class.

    Accepts every vocabulary the pipeline emits: pass-2 ``design_type`` ("RCT",
    "cluster-randomized", "quasi-experimental", "human-challenge", ...), pass-1
    ``study_design`` ("RCT", "single-arm", "observational", ...) and the
    screening ``trial_type`` ("rct", "non_randomized_trial", ...).
    """
    d = key(label).replace("_", " ").replace("-", " ")
    d = re.sub(r"\s+", " ", d).strip()
    if any(s in d for s in _NOT_RANDOMIZED):
        return "observational"
    randomized_word = "random" in d or d.split(" ")[0] in {"rct", "crt"} or "rct" in d.split(" ")
    if "cluster" in d:
        return "cluster_rct" if randomized_word or randomized_evidence else "unknown"
    if "crossover" in d or "cross over" in d:
        return "crossover"
    if randomized_word or d in {"factorial", "parallel group", "parallel"}:
        return "rct"
    if d in _UNRESOLVED_LABELS or d in _SETTING_LABELS:
        return "rct" if randomized_evidence else ("observational" if d in _SETTING_LABELS else "unknown")
    return "observational"


def design(record):
    """Fine design class of an extraction record (see ``design_label``)."""
    sd = record.get("study_design") or {}
    return design_label(sd.get("design_type"), has_randomization_evidence(sd))


def design_class(record):
    """Coarse class used for pooling strata, GRADE and the dashboard flag:
    'rct' (any randomized design), 'observational' or 'unknown'."""
    fine = design(record)
    return "rct" if fine in RANDOMIZED_CLASSES else fine


def is_randomized(record):
    return design(record) in RANDOMIZED_CLASSES


def is_baseline(tp):
    role, label = key(tp.get("role")), key(tp.get("label"))
    if role:
        return role in {"baseline", "screening"}
    if re.search(r"baseline|screening|pre[- ]?dose|before (?:treatment|dose|administration|immersion)", label):
        return True
    # A same-day post-dose observation is not a baseline measurement.
    if re.search(r"after|post[- ]?dose|\b(?:min|minutes|hours|hrs?)\b", label):
        return False
    return tp.get("weeks_from_randomization") == 0


def select_timepoint(outcome):
    tps = objects(outcome.get("timepoints"))
    explicit = outcome.get("primary_timepoint_id")
    if explicit is not None:
        return next((t for t in tps if t.get("timepoint_id") == explicit and not is_baseline(t)), None)
    candidates = [t for t in tps if not is_baseline(t) and
                  key(t.get("role")) not in {"washout", "post_treatment"} and
                  not re.search(r"washout|after (?:stopping|cessation|withdrawal)|post[- ]treatment",
                                key(t.get("label")))]
    if not candidates:
        return None
    def timing(t):
        w = t.get("weeks_from_randomization")
        duration = t.get("elapsed_hours")
        return (w if number(w) else -1, duration if number(duration) else -1,
                tps.index(t))
    return max(candidates, key=timing)


def pair_of(e, arms):
    a, b = e.get("intervention_arm_id"), e.get("comparator_arm_id")
    if a is None or b is None:
        match = re.fullmatch(r"arm(.+?)_vs_arm([^_]+)(?:_change_adjusted)?", str(e.get("comparison") or ""))
        if match:
            ids = {str(i): i for i in arms}
            a, b = ids.get(match[1]), ids.get(match[2])
    if a not in arms or b not in arms or a == b:
        return None
    ra, rb = key(arms[a].get("type")), key(arms[b].get("type"))
    if ra == "intervention" and rb in CONTROL_TYPES:
        return a, b, 1
    if rb == "intervention" and ra in CONTROL_TYPES:
        return b, a, -1
    if ra == "intervention" and rb == "intervention":
        return a, b, 1
    return None


def p_operator(e):
    op = e.get("p_value_operator")
    if op in {"=", "<", "<=", "≤", ">", ">=", "≥"}:
        return {"≤": "<=", "≥": ">="}.get(op, op)
    if e.get("p_value_exact") is True:
        return "="
    # Legacy thresholds may be recovered only from the actual quoted threshold.
    quote = html.unescape(str(e.get("effect_quote") or ""))
    match = re.search(r"\bp\s*(<=|>=|[<>=≤≥])\s*(0?\.\d+)", quote, re.I)
    if match and number(e.get("p_value")) and abs(float(match[2]) - e["p_value"]) < 1e-10:
        return {"≤": "<=", "≥": ">="}.get(match[1], match[1])
    return None


def significance(e):
    """Significance of this comparison, not clinical benefit or equivalence."""
    p, op = e.get("p_value"), p_operator(e)
    by_p = None
    if number(p) and 0 <= p <= 1:
        if op == "=": by_p = "significant" if p < .05 else "not_detected"
        elif op == "<" and p <= .05: by_p = "significant"
        elif op == "<=" and p < .05: by_p = "significant"
        elif op in {">", ">="} and p >= .05: by_p = "not_detected"
    lo, hi = e.get("ci_95_low"), e.get("ci_95_high")
    if number(lo) and number(hi) and lo < hi:
        null = 1 if measure(e.get("effect_measure")) in RATIOS else 0
        by_ci = "significant" if hi < null or lo > null else "not_detected"
        if by_p and by_p != by_ci:
            return "conflicting"
        return by_p or by_ci
    return by_p or "unknown"


def validate_record(record):
    findings = [dict(i, rules_version=RULES_VERSION, source_audit_finding=True)
                for i in objects(record.get("_source_findings"))]
    arms = {a.get("arm_id"): a for a in objects((record.get("study_design") or {}).get("arms"))}
    sizes = {a.get("arm_id"): a for a in objects((record.get("sample_sizes") or {}).get("per_arm"))}
    dt = design(record)
    for oi, o in enumerate(objects(record.get("outcomes"))):
        known_tps = {t.get("timepoint_id") for t in objects(o.get("timepoints"))}
        for section in ("arm_results", "between_group_effects"):
            for ni, node in enumerate(objects(o.get(section))):
                path = f"outcomes.{oi}.{section}.{ni}"
                def add(code, message):
                    findings.append(issue(code, path, message, node, record))
                for field in ("mean", "sd", "se", "effect_value", "ci_95_low", "ci_95_high", "p_value", "n_analyzed", "events", "total"):
                    val = node.get(field)
                    if val is not None and not number(val): add("invalid_number", f"{field} must be a finite number")
                for field in ("sd", "se"):
                    if number(node.get(field)) and node[field] <= 0: add("invalid_uncertainty", f"{field} must be positive for pooling")
                lo, hi = node.get("ci_95_low"), node.get("ci_95_high")
                if number(lo) and number(hi):
                    if lo >= hi: add("invalid_interval", "Confidence interval bounds are reversed or identical")
                    val = node.get("effect_value") if section == "between_group_effects" else node.get("mean")
                    if number(val) and not lo <= val <= hi: add("estimate_outside_interval", "Estimate falls outside its reported confidence interval")
                if number(node.get("p_value")) and not 0 <= node["p_value"] <= 1: add("invalid_p_value", "p-value is outside [0,1]")
                if known_tps and node.get("timepoint_id") not in known_tps and node.get("timepoint_id") is not None:
                    add("unknown_timepoint", "Result references an unknown timepoint")
                if section == "arm_results":
                    # A source-audited extraction put SF-36 percentage changes
                    # into raw-score outcomes. Keep the evidence, but prevent
                    # those means from being pooled as points. A matching
                    # percentage in a quote needs review, not silent conversion.
                    unit = key(o.get("unit"))
                    quote = html.unescape(str(node.get("result_quote") or "")).replace("−", "-")
                    quote = re.sub(r"\b\d+(?:[.,]\d+)?\s*%\s*(?:CI\b|confidence(?:\s+interval)?)",
                                   "", quote, flags=re.I)
                    percentages = re.findall(r"(?<![\d.,])(-?\d+(?:[.,]\d+)?)\s*%", quote)
                    if (number(node.get("mean")) and unit and
                            not any(marker in unit for marker in ("%", "percent", "per cent")) and
                            any(math.isclose(float(value.replace(",", ".")), node["mean"],
                                             rel_tol=0, abs_tol=1e-9) for value in percentages)):
                        add("percentage_unit_conflict", "The quoted mean is marked as a percentage but the outcome has non-percentage units; verify its scale before synthesis")
                    aid = node.get("arm_id")
                    n = node.get("n_analyzed") if node.get("n_analyzed") is not None else node.get("total")
                    if aid not in arms: add("unknown_arm", "Result references an unknown arm")
                    if n is not None and (not number(n) or n <= 0 or int(n) != n): add("invalid_sample_size", "Sample size must be a positive integer")
                    limit = sizes.get(aid, {}).get("n_randomized") or sizes.get(aid, {}).get("n_enrolled")
                    if not limit and dt in {"rct", "observational"}:
                        limit = (record.get("sample_sizes") or {}).get("n_randomized_total")
                    if dt in {"rct", "observational"} and number(n) and number(limit) and n > limit:
                        add("arm_n_exceeds_enrolled", f"Outcome n={n} exceeds this arm's enrolled/randomized n={limit}")
                    if node.get("n_unit") in {"visits", "measurements", "clusters"}:
                        add("wrong_sampling_unit", "This count is not an independent participant denominator")
                    total_randomized = (record.get("sample_sizes") or {}).get("n_randomized_total")
                    peers = [c for c in objects(o.get("arm_results")) if
                             c.get("timepoint_id") == node.get("timepoint_id") and
                             c.get("result_type") == node.get("result_type") and
                             c.get("analysis_population") == node.get("analysis_population")]
                    counts = {c.get("arm_id"): c.get("n_analyzed") for c in peers}
                    if dt == "rct" and number(total_randomized) and all(number(n) for n in counts.values()) and sum(counts.values()) > total_randomized:
                        add("arm_counts_exceed_study", "Parallel-arm outcome counts exceed the entire randomized cohort")
                    ev, den = node.get("events"), node.get("total")
                    if ev is not None and (not number(den) or den <= 0 or int(den) != den):
                        add("unresolved_denominator", "Event count requires a positive participant denominator")
                    if number(ev) and (ev < 0 or int(ev) != ev or (number(den) and ev > den)):
                        add("invalid_events", "Events must be integers between zero and the participant denominator")
                else:
                    if pair_of(node, arms) is None: add("unresolved_comparison", "Comparison does not identify an intervention and comparator arm")
                    if node.get("p_value_scope") in {"within_arm", "omnibus", "period", "carryover"}:
                        add("not_comparison_p", "This test is not the selected between-treatment contrast")
                    if significance(node) == "conflicting": add("p_ci_conflict", "Reported p-value and confidence interval disagree")
                    if measure(node.get("effect_measure")) in RATIOS:
                        if any(number(node.get(k)) and node[k] <= 0 for k in ("effect_value", "ci_95_low", "ci_95_high")):
                            add("invalid_ratio", "Ratio estimates and their confidence bounds must be positive")
                    if dt in {"crossover", "cluster_rct"} and node.get("design_adjusted") is not True:
                        add("unverified_design_variance", "Paired or cluster-adjusted comparison variance must be verified")
    return {"rules_version": RULES_VERSION, "status": "issues_found" if findings else "checked",
            "issues": findings, "source_verified": bool(record.get("_source_audit")),
            "note": "Numerical checks and quote matches do not establish correct table-column attribution."}


def _path_contains(parent, child):
    """Match a source finding's scope on complete field-path segments."""
    return (isinstance(parent, str) and bool(parent) and isinstance(child, str)
            and (parent == child or child.startswith(parent + ".")))


def _blocking_findings(findings, path):
    # A finding on an outcome, a result, or a field within that result applies
    # to the result. Sibling outcomes/results remain independent.
    return [i for i in findings if i.get("severity") == "blocking"
            and (_path_contains(i.get("path"), path)
                 or _path_contains(path, i.get("path", "")))]


def raw_cell(cell, record, path, findings):
    if _blocking_findings(findings, path):
        return None
    n, mean = cell.get("n_analyzed"), cell.get("mean")
    if not number(n) or n <= 1 or not number(mean): return None
    if cell.get("mean_type") != "raw" or cell.get("n_unit") != "participants": return None
    if cell.get("uncertainty_type") not in {"sd", "se_mean", "ci_mean"}: return None
    sd = cell.get("sd")
    if not number(sd):
        # Adjusted / least-squares means are NEVER raw SDs via sqrt(n).
        if cell.get("uncertainty_type") == "se_mean" and number(cell.get("se")):
            sd = cell["se"] * math.sqrt(n)
        elif cell.get("uncertainty_type") == "ci_mean":
            lo, hi = cell.get("ci_95_low"), cell.get("ci_95_high")
            if number(lo) and number(hi) and hi > lo:
                from ber_core.meta_analysis import t_ppf
                sd = (hi-lo)/(2*t_ppf(.975, n-1))*math.sqrt(n)
    return (mean, sd, n) if number(sd) and sd > 0 else None


def study_identity(record):
    """Never merge unrelated trials on an author/year/N heuristic."""
    explicit = record.get("verified_study_id")
    if explicit: return str(explicit)
    registration = (record.get("study_design") or {}).get("trial_registration") or {}
    if isinstance(registration, dict) and registration.get("verified") and registration.get("id"):
        return "registry:" + key(registration["id"])
    return key(record.get("paper_id"))


def mapping_for(o, policy):
    mappings = (policy or {}).get("outcome_mappings") or {}
    return next((v for k, v in mappings.items() if key(k) == key(o.get("name")) and isinstance(v, dict)), {})


def treatment_identity(arm):
    """The pooling identity of an arm's treatment.

    An explicit protocol ``synthesis_intervention`` wins; then the canonical
    name from canonicalize_interventions.py (attached at export), so naming
    variants ("Azelastine 0.1%" / "azelastine nasal spray") pool together; the
    verbatim extracted name and arm label are fallbacks only.
    """
    return (arm.get("synthesis_intervention") or arm.get("intervention_canonical")
            or arm.get("intervention_name") or arm.get("label"))


def selected_comparisons(record, policy=None):
    """All prespecified arm comparisons, with both accepted and withheld estimates."""
    policy = policy or {}
    validation = validate_record(record)
    issues = validation["issues"]
    arms = {a.get("arm_id"): a for a in objects((record.get("study_design") or {}).get("arms"))}
    pairs = [(a, b) for a in arms for b in arms if key(arms[a].get("type")) == "intervention"
             and key(arms[b].get("type")) in CONTROL_TYPES]
    results = []
    for oi, o in enumerate(objects(record.get("outcomes"))):
        tp = select_timepoint(o)
        tid = tp.get("timepoint_id") if tp else None
        mapping = mapping_for(o, policy)
        construct = mapping.get("smd_construct") or o.get("smd_construct")
        concept = construct or mapping.get("concept") or o.get("outcome_concept") or key(o.get("name"))
        if not (construct or mapping.get("concept") or o.get("outcome_concept")) and o.get("measurement_instrument"):
            concept += " | " + key(o["measurement_instrument"])
        unit = mapping.get("unit") or o.get("unit")
        window = mapping.get("time_window") or o.get("synthesis_time_window")
        if not window and tp:
            w = tp.get("weeks_from_randomization")
            window = f"week:{w}" if number(w) and w > 0 else key(tp.get("label"))
        population = mapping.get("population") or o.get("synthesis_population") or record.get("synthesis_population")
        outcome_pairs = list(pairs)
        for e in objects(o.get("between_group_effects")):
            pair = pair_of(e, arms)
            if pair and pair[:2] not in outcome_pairs:
                outcome_pairs.append(pair[:2])
        for a, b in outcome_pairs or [(None, None)]:
            iv, ct = arms.get(a, {}), arms.get(b, {})
            item = {"paper_id": record.get("paper_id"), "study_id": study_identity(record),
                    "outcome_id": o.get("outcome_id"), "outcome_name": o.get("name"),
                    "outcome_domain": o.get("outcome_domain") or o.get("symptom_domain") or o.get("category") or "other",
                    "outcome_concept": concept, "unit": unit, "timepoint_id": tid,
                    "timepoint": tp, "time_window": window, "population": population,
                    "intervention_arm_id": a, "comparator_arm_id": b,
                    "intervention": treatment_identity(iv),
                    "comparator": ct.get("synthesis_intervention") or ("placebo" if key(ct.get("type")) == "placebo" else treatment_identity(ct)),
                    "intervention_category": iv.get("intervention_category"),
                    "design_class": design(record), "is_primary": bool(o.get("is_primary")),
                    "higher_is_better": o.get("higher_is_better"), "basis": None,
                    "effect_measure": None, "effect": None, "standard_error": None,
                    "n_total": None, "source": None, "source_path": None,
                    "standardization": None,
                    "source_file": record.get("_source_file"), "source_quote": None,
                    "extraction_version": record.get("_extraction_version") or record.get("_extracted_at") or "legacy-unversioned",
                    "source_location": None, "rules_version": RULES_VERSION,
                    "significance": "unknown", "p_value": None, "p_value_operator": None,
                    "ci_low": None, "ci_high": None, "poolable": False, "issues": []}
            def block(code, reason, node=None, path=None):
                item["issues"].append(issue(code, path or f"outcomes.{oi}", reason, node, record))
            # Outcome-wide source holds apply even when a reported estimate
            # has otherwise valid arithmetic. Do not import findings from
            # unused sibling results or assessments.
            item["issues"].extend(i for i in issues if i.get("severity") == "blocking"
                                  and _path_contains(i.get("path"), f"outcomes.{oi}"))
            es = []
            for ei, e in enumerate(objects(o.get("between_group_effects"))):
                pair = pair_of(e, arms)
                if pair and pair[:2] == (a, b) and (e.get("timepoint_id") == tid or (e.get("timepoint_id") is None and not o.get("timepoints"))):
                    es.append((ei, e, pair[2]))
            # No preference based on p, effect magnitude or standard error.
            es.sort(key=lambda x: (not bool(x[1].get("is_primary_analysis")), not bool(x[1].get("adjusted")), x[0]))
            for ei, e, orientation in es:
                path = f"outcomes.{oi}.between_group_effects.{ei}"
                blocked = _blocking_findings(issues, path)
                if blocked:
                    item["issues"].extend(i for i in blocked if i not in item["issues"])
                    continue
                sig = significance(e)
                if item["significance"] == "unknown":
                    item.update(significance=sig, p_value=e.get("p_value"), p_value_operator=p_operator(e),
                                source_quote=e.get("effect_quote"), source_path=path)
                if key(e.get("effect_source")) in {"calculated", "derived", "estimated", "estimated_from_figure"}:
                    item["basis"] = e.get("result_type") or o.get("analysis_basis")
                    continue  # Recalculate from verified raw cells; do not trust model arithmetic.
                m, val, se = measure(e.get("effect_measure")), e.get("effect_value"), e.get("se")
                lo, hi = e.get("ci_95_low"), e.get("ci_95_high")
                if m not in MEASURES or not number(val): continue
                if m in RATIOS and number(se) and e.get("uncertainty_scale") != "log":
                    block("unresolved_ratio_se_scale", "The ratio's standard error must be explicitly on the log scale", e, path)
                    se = None
                if not number(se) and number(lo) and number(hi) and hi > lo:
                    se = (math.log(hi)-math.log(lo))/(2*Z95) if m in RATIOS else (hi-lo)/(2*Z95)
                if m in RATIOS:
                    y = orientation * math.log(val)
                    effect = math.exp(y)
                else: y = effect = orientation * val
                item.update(effect=effect, effect_measure=m, _y=y, standard_error=se,
                            ci_low=(1/hi if m in RATIOS else -hi) if orientation == -1 and number(hi) else lo,
                            ci_high=(1/lo if m in RATIOS else -lo) if orientation == -1 and number(lo) else hi,
                            significance=sig, p_value=e.get("p_value"), p_value_operator=p_operator(e),
                            source="reported_adjusted" if e.get("adjusted") else "reported",
                            source_path=path, source_quote=e.get("effect_quote"),
                            source_location=e.get("source_location"),
                            basis=e.get("result_type") or o.get("analysis_basis"),
                            standardization=e.get("standardization") or o.get("standardization"),
                            analysis_population=e.get("analysis_population") or o.get("analysis_population"))
                if number(se) and se > 0:
                    break
            # Missing reported uncertainty may be recovered from independently
            # sampled raw arm summaries. Never do this for paired/cluster models.
            if not number(item["standard_error"]) and design(record) in {"rct", "observational"}:
                declared_basis = item["basis"] or o.get("analysis_basis")
                bases = ([declared_basis] if declared_basis in {"endpoint", "change_from_baseline"}
                         else [] if declared_basis and declared_basis != "unknown"
                         else ["endpoint", "change_from_baseline"])
                for basis in bases:
                    cells = {}
                    for ci, c in enumerate(objects(o.get("arm_results"))):
                        if c.get("arm_id") in (a, b) and c.get("timepoint_id") == tid and c.get("result_type") == basis:
                            path = f"outcomes.{oi}.arm_results.{ci}"
                            cell = raw_cell(c, record, path, issues)
                            if cell: cells[c["arm_id"]] = (cell, c, path)
                    if a not in cells or b not in cells: continue
                    (m1,s1,n1), c1, path1 = cells[a]
                    (m2,s2,n2), c2, path2 = cells[b]
                    if c1.get("analysis_population") != c2.get("analysis_population"): continue
                    val, se = m1-m2, math.sqrt(s1*s1/n1+s2*s2/n2)
                    # An explicit same-construct mapping is required for SMD.
                    smd = bool(mapping.get("smd_construct") or o.get("smd_construct"))
                    if smd:
                        sp = math.sqrt(((n1-1)*s1*s1+(n2-1)*s2*s2)/(n1+n2-2))
                        j = 1-3/(4*(n1+n2-2)-1)
                        val = j*val/sp
                        se = math.sqrt((n1+n2)/(n1*n2)+val*val/(2*(n1+n2)))
                        item["unit"] = "standard_deviation"
                        item["standardization"] = "pooled_within_arm_sd"
                    raw_sig = "significant" if abs(val/se) > Z95 else "not_detected"
                    if item["significance"] not in {"unknown", raw_sig}:
                        block("reported_raw_conflict", "Raw-summary inference differs from the reported analysis; the analysis basis needs review")
                        item["significance"] = "conflicting"
                    item.update(effect=val, _y=val, standard_error=se,
                                effect_measure="smd" if smd else "mean_difference", basis=basis,
                                source="raw_arm_summaries", source_path=[path1,path2],
                                source_quote=[c1.get("result_quote"),c2.get("result_quote")],
                                source_location=[c1.get("source_location"),c2.get("source_location")],
                                n_total=n1+n2, ci_low=val-Z95*se, ci_high=val+Z95*se,
                                analysis_population=c1.get("analysis_population"))
                    if item["significance"] == "unknown":
                        item["significance"] = raw_sig
                    break
            if a is None or b is None: block("no_comparator_role", "No explicit intervention/comparator pair is recorded")
            if not item["intervention"] or not item["comparator"]: block("unresolved_treatment", "Treatment and comparator identities must be specified")
            if o.get("organism") and o["organism"] != "human": block("nonhuman_outcome", "This outcome is not a human participant outcome")
            if not item["source"]: block("no_usable_estimate", "No verified contrast or compatible raw arm summaries at the selected timepoint")
            if not number(item["standard_error"]) or item["standard_error"] <= 0:
                block("unresolved_uncertainty", "Usable uncertainty is missing; adjusted SEs cannot be converted into raw SDs")
            if item["effect_measure"] == "smd" and not (mapping.get("smd_construct") or o.get("smd_construct")):
                block("unmapped_standardization", "SMD requires an explicit mapping to the same outcome construct")
            if item["effect_measure"] == "smd" and (item["unit"] != "standard_deviation" or not item["standardization"]):
                block("unresolved_standardization", "Reported SMD requires verified standard-deviation units and standardization basis")
            if not concept or key(concept) in {"other", "mixed", "unknown"}: block("unresolved_outcome", "A specific outcome is required")
            if item["effect_measure"] in {"mean_difference", "rate_difference"} and not item["unit"]:
                block("unresolved_unit", "Outcome unit is not verified")
            if not window: block("unresolved_time_window", "The outcome's assessment time is not identified")
            if key(population) in {"", "unknown", "unclear"}: block("unresolved_population", "A compatible synthesis population has not been specified")
            if key(item["basis"]) in {"", "unknown", "unclear"}: block("unresolved_basis", "Endpoint, change, or model-based analysis basis is unspecified")
            if key(item.get("analysis_population")) in {"", "unknown", "unclear", "not_reported"}: block("unresolved_analysis_population", "The analysis population (for example ITT or complete cases) is unspecified")
            if design(record) == "unknown": block("unresolved_design", "The study's allocation/design is unresolved")
            # Errors in unused fields are recorded in validation, not used to
            # suppress an otherwise independent valid contrast.
            item["poolable"] = number(item["effect"]) and number(item["standard_error"]) and not item["issues"]
            results.append(item)
    return results


def result_verdict(item):
    if any(i.get("source_audit_finding") and i.get("severity") == "blocking"
           for i in item["issues"]):
        return "insufficient_data"
    if any(i["code"] in {"p_ci_conflict", "invalid_interval", "estimate_outside_interval", "reported_raw_conflict", "unverified_design_variance"} for i in item["issues"]):
        return "insufficient_data"
    if item["significance"] == "not_detected": return "no_difference"
    if item["significance"] != "significant" or not number(item["effect"]): return "insufficient_data"
    hib = item.get("higher_is_better")
    if not isinstance(hib, bool): return "insufficient_data"
    null = 1 if item["effect_measure"] in RATIOS else 0
    if item["effect"] == null: return "insufficient_data"
    benefit = item["effect"] > null if hib else item["effect"] < null
    return "favors_treatment" if benefit else "favors_control"


def summarize_results(record):
    selected = selected_comparisons(record)
    primaries = [s for s in selected if s["is_primary"]]
    verdicts = [result_verdict(s) for s in primaries]
    known = set(verdicts) - {"insufficient_data"}
    if not primaries or "insufficient_data" in verdicts:
        verdict = "insufficient_data"
    elif len(known) > 1: verdict = "mixed"
    else: verdict = next(iter(known), "insufficient_data")
    by_arm = {}
    for aid in {s["intervention_arm_id"] for s in primaries}:
        vs = {result_verdict(s) for s in primaries if s["intervention_arm_id"] == aid}
        by_arm[str(aid)] = "insufficient_data" if "insufficient_data" in vs else next(iter(vs)) if len(vs) == 1 else "mixed"
    return {"verdict": verdict, "by_arm": by_arm, "selected": primaries,
            "reason": "Primary outcomes at the selected assessment, evaluated separately for each treatment comparison; incomplete results remain explicit."}


def group_key(s):
    # SMD standardization requires a common basis; MD endpoint/change can target
    # the same effect but remain separate unless explicitly mapped by protocol.
    return tuple(key(s.get(k)) for k in ("intervention", "comparator", "outcome_concept", "unit",
        "effect_measure", "basis", "design_class", "population", "time_window", "analysis_population", "standardization", "higher_is_better"))


def prepare_synthesis(records, policy=None):
    accepted, withheld, validations = [], [], []
    for r in records:
        if is_failed_status(r.get("_status")): continue
        validations.append({"paper_id": r.get("paper_id"), **validate_record(r)})
        for s in selected_comparisons(r, policy):
            (accepted if s["poolable"] else withheld).append(s)
    buckets = defaultdict(list)
    for s in accepted: buckets[(group_key(s), s["study_id"])].append(s)
    kept = []
    for (_, _), candidates in buckets.items():
        # Repeated reports of the exact contrast are chosen by primary analysis
        # status then report ID, never estimated precision or significance.
        pairs = {(s["paper_id"], s["intervention_arm_id"], s["comparator_arm_id"]) for s in candidates}
        same_report = len({p[0] for p in pairs}) == 1
        if len(pairs) > 1 and same_report:
            for s in candidates:
                s["poolable"] = False
                s["issues"].append(issue("shared_control", "comparison", "Multiple arms of this study enter the same pool; specify distinct regimens or a combined-arm analysis"))
                withheld.append(s)
            continue
        ranked = sorted(candidates, key=lambda s: (not s["is_primary"], s["source"] != "reported_adjusted", str(s["paper_id"])))
        kept.append(ranked[0])
        for s in ranked[1:]:
            s["poolable"] = False
            s["issues"].append(issue("duplicate_study_contrast", "comparison", "Another selected report represents this study and contrast"))
            withheld.append(s)
    from ber_core.utils import find_duplicate_candidates
    candidates = find_duplicate_candidates(records)
    return {"rules_version": RULES_VERSION, "selected": kept, "withheld": withheld,
            "validation": validations, "possible_companion_reports": candidates}


def feasibility(records, policy=None, min_trials=2):
    min_trials = max(2, int(min_trials or 2))
    prepared = prepare_synthesis(records, policy)
    groups = defaultdict(list)
    for s in prepared["selected"]: groups[group_key(s)].append(s)
    domains = []
    for entries in groups.values():
        s = entries[0]; n = len({e["study_id"] for e in entries})
        domains.append({"domain": s["outcome_domain"], "outcome": s["outcome_concept"],
                        "intervention": s["intervention"], "comparator": s["comparator"],
                        "n_trials": n, "n_outcomes": len(entries), "n_pooled": len(entries) if n >= min_trials else 0,
                        "n_instruments": 1, "top_instrument": s["outcome_concept"], "top_instrument_share": 1,
                        "verdict": "poolable" if n >= min_trials else "too_few",
                        "reason": f"{n} independent studies with the same outcome, comparison, population and assessment window"})
    counts = Counter(i["code"] for s in prepared["withheld"] for i in s["issues"])
    supported = any(d["verdict"] == "poolable" for d in domains)
    return {"overall": "supported" if supported else "not_possible", "n_papers": len(records),
            "n_outcomes": sum(len(objects(r.get("outcomes"))) for r in records),
            "n_with_contrast": len({s["paper_id"] for s in prepared["selected"]}),
            "n_with_comparison": len({s["paper_id"] for s in prepared["selected"]+prepared["withheld"]
                                      if s["intervention_arm_id"] is not None and s["comparator_arm_id"] is not None}),
            "n_comparisons": len(prepared["selected"])+len(prepared["withheld"]),
            "n_pooled": sum(d["n_pooled"] for d in domains), "domains": domains,
            "blockers": [{"code": c,"n": n,"explanation": c.replace("_"," ")} for c,n in counts.items()],
            "recommendation": "Pool only the compatible comparison groups shown below." if supported else "No meta-analysis is possible from the validated selections yet; retain an evidence map until compatible estimates and uncertainty (standard deviation, SE or CI) are available.",
            "rules_version": RULES_VERSION, "withheld": prepared["withheld"]}
