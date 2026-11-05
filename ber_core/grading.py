"""Compatibility helpers, risk of bias and GRADE for review analysis.

Scientific comparison selection, design classes and effect-measure
vocabularies are owned by ber_core.scientific; pooling (and Egger's test,
leave-one-out, subgroups) by meta_analysis. This module keeps the GRADE rating,
the per-study risk-of-bias reader, the poolability probe and the legacy
single-SMD interface, which accepts only one validated, explicitly mapped
continuous comparison. Binary/ratio effects are not converted to SMD, and
model-generated arithmetic is not an analysis input.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


def design_class(record: dict, design_of=None) -> str:
    """'rct' for any randomized design, else 'observational'.

    Delegates to ``ber_core.scientific`` — the single definition of
    randomization. Unresolved designs map to the conservative 'observational'
    (GRADE starts low). ``design_of`` (paper_id -> design label, e.g. from
    study_designs.csv) is consulted only when the record has no design_type.
    """
    from ber_core.scientific import design, design_label, has_randomization_evidence, RANDOMIZED_CLASSES
    sd = record.get("study_design") or {}
    if not str(sd.get("design_type") or "").strip() and design_of is not None:
        fine = design_label(design_of(record.get("paper_id")), has_randomization_evidence(sd))
    else:
        fine = design(record)
    return "rct" if fine in RANDOMIZED_CLASSES else "observational"


# ── Timepoint / basis selection (audit-hardened) ──────────────────────────────

def primary_timepoint(outcome: dict):
    from ber_core.scientific import select_timepoint
    return select_timepoint(outcome)


# ── Pooling ───────────────────────────────────────────────────────────────────


# ── Standardized effect size (SMD) ────────────────────────────────────────────


_REGISTRY_RE = re.compile(
    r"\b(NCT\d{8}|ISRCTN\d{8}|ChiCTR[-\w]*\d{6,}|EudraCT\s*\d{4}-\d{6}-\d{2}"
    r"|UMIN\d{9}|ACTRN\d{14}|IRCT\w*\d{6,})\b", re.I)


def study_key(record: dict, meta: dict | None = None) -> str | None:
    """A key identifying the STUDY, so companion reports don't double-count.

    A meta-analysis pools STUDIES; the pipeline's unit is a REPORT (a DOI). Two
    published papers reporting the same trial — a main-results paper and a
    secondary-outcomes paper — otherwise enter the pool as independent evidence,
    inflating k and biasing the estimate. Neither is a duplicate in the usual
    sense: both are real, distinct papers.

    Resolution order:
      1. A trial-registration id found anywhere in the record (NCT/ISRCTN/...).
         Authoritative when present, but pass-1's ``trial_registry`` extraction
         is off by default, so in practice this is usually absent.
      2. ``first-author surname | year | randomized N`` from joined metadata.
         Deliberately conservative: requires ALL THREE, because collapsing two
         genuinely different trials is worse than failing to merge two reports.

    Returns None when no key can be formed — callers should fall back to the
    paper id, which is the current (double-counting) behavior.
    """
    blob = json.dumps(record, default=str)
    m = _REGISTRY_RE.search(blob)
    if m:
        return "reg:" + m.group(1).upper().replace(" ", "")

    meta = meta or {}
    surname = first_author_surname(meta.get("authors") or record.get("authors") or "")
    year = meta.get("year") or record.get("year")
    ss = record.get("sample_sizes") or {}
    n = ss.get("n_randomized_total") or ss.get("n_enrolled_total")
    if surname and year and n:
        return f"aey:{surname.lower()}|{year}|{n}"
    return None


def multi_arm_truncation(roles: dict) -> dict | None:
    """Describe evidence dropped because this trial has >2 arms, else None.

    ``smd_from_outcome`` contributes ONE comparison per trial: every path below
    selects a single intervention arm and a single control arm. For a 3-arm
    dose-ranging trial that silently discards two thirds of the evidence — the
    kind of error that quietly changes a pooled estimate — so callers should
    attach this to the point and footnote it.

    Cochrane Handbook 23.3 would combine the active arms into one synthetic arm
    instead. Until that lands, make the truncation visible rather than invisible.
    """
    n_iv = sum(1 for r in roles.values() if r == "intervention")
    n_ct = sum(1 for r in roles.values() if r == "control")
    if n_iv <= 1 and n_ct <= 1:
        return None
    return {"n_intervention_arms": n_iv, "n_control_arms": n_ct,
            "n_arms_used": 2,
            "note": (f"trial has {n_iv} intervention and {n_ct} control arm(s); "
                     f"only one of each contributes to the pooled estimate")}


_COMPARISON_RE = re.compile(r"^arm(\d+)_vs_arm(\d+)")


def comparison_orientation(effect: dict, roles: dict) -> float | None:
    """+1 if the effect runs intervention-vs-control, -1 if reversed, else None.

    ``between_group_effects[].comparison`` is a label like ``arm1_vs_arm2``, and
    83% of ratio effects carry a parseable one. The reported value is oriented
    according to THAT label, not according to which arm the pipeline considers
    the intervention — 158 effects in the corpus are explicitly
    control-vs-intervention. Reading the value without reading the label
    silently reverses those studies.

    Returns None when the label names a pair that is not the intervention/
    control contrast at all (e.g. ``arm1_vs_arm3`` comparing two active doses):
    that effect answers a different question and must be skipped, not guessed at.
    An absent or unparseable label returns +1 — the historical assumption, kept
    so effects without a label behave as before.
    """
    m = _COMPARISON_RE.match((effect.get("comparison") or "").strip().lower())
    if not m:
        return 1.0
    i, j = int(m.group(1)), int(m.group(2))
    ri, rj = roles.get(i), roles.get(j)
    if ri == "intervention" and rj == "control":
        return 1.0
    if ri == "control" and rj == "intervention":
        return -1.0
    # One side has no mapped role — almost always an `unexposed` comparator in an
    # observational study (114 of 117 such cases), which IS the control in
    # substance but is absent from CONTROL_TYPES. The direction is still readable
    # from whichever side IS the intervention, so orient on that rather than
    # discarding a usable effect.
    if ri == "intervention" and rj is None:
        return 1.0
    if rj == "intervention" and ri is None:
        return -1.0
    # Both roles known but not the intervention/control contrast (e.g. two active
    # doses, or a between-cohort comparison in a factorial design): this effect
    # answers a different question and must not be pooled as if it were ours.
    return None


def first_author_surname(authors: str) -> str:
    if not authors:
        return ""
    first = re.split(r"[;,]| and ", authors.strip())[0].strip()
    parts = first.split()
    if not parts:
        return first
    return parts[0] if len(parts[0]) > 2 else parts[-1]


# ── Risk of bias ──────────────────────────────────────────────────────────────

_ROB_HIGH = {"high", "serious", "critical"}
_ROB_SOME = {"some_concerns", "some concerns", "moderate", "some"}
_ROB_LOW = {"low"}


def _domain_text(dj) -> str:
    """domain_judgments as one lowercase string, whatever shape it arrived in.

    The pass-2 schema asks for "randomization:low, deviations:some_concerns, ..."
    but models also return a {domain: judgment} object or a list of either."""
    if isinstance(dj, dict):
        return ", ".join(f"{k}:{v}" for k, v in dj.items()).lower()
    if isinstance(dj, list):
        return ", ".join(_domain_text(x) if isinstance(x, (dict, list)) else str(x)
                         for x in dj).lower()
    return str(dj or "").lower()


def parse_rob(record: dict) -> str:
    """Overall RoB category for a study: 'low' | 'some' | 'high' | 'unknown'.

    Prefers the extracted overall_judgment; falls back to the worst domain
    judgment in domain_judgments (RoB 2 and ROBINS-I both supported). ROBINS-I
    'serious'/'critical' count as high, 'moderate' as some concerns."""
    rob = record.get("risk_of_bias") or {}
    if not isinstance(rob, dict):
        return "unknown"
    overall = str(rob.get("overall_judgment") or "").strip().lower()
    if overall in _ROB_HIGH:
        return "high"
    if overall in _ROB_SOME:
        return "some"
    if overall in _ROB_LOW:
        return "low"
    dj = _domain_text(rob.get("domain_judgments"))
    if any(h in dj for h in _ROB_HIGH):
        return "high"
    if any(s in dj for s in ("some_concerns", "some concerns", "moderate")):
        return "some"
    if "low" in dj:
        return "low"
    return "unknown"


# ── GRADE certainty ───────────────────────────────────────────────────────────

_GRADE_LEVELS = ["high", "moderate", "low", "very_low"]
# Below this many studies Egger's test has too little power to act on.
EGGER_MIN_K = 10


def grade_certainty(*, design_cls: str, n_trials: int, total_n, i2: float,
                    ci_low: float, ci_high: float, null_value: float,
                    rob: dict, egger: dict | None, indirectness_penalty: int = 0) -> dict:
    """Deterministic GRADE-style certainty for one pooled comparison.

    Starts High for randomized evidence (any class in
    ``ber_core.scientific.RANDOMIZED_CLASSES``) and Low otherwise, then
    downgrades for risk of bias (per-study RoB counts), inconsistency (I²),
    imprecision (CI crosses the null, or the optimal information size is not
    met), publication bias (Egger p < .10 at k >= 10) and any supplied
    indirectness. Returns {grade, start, downgrades: [{domain, reason}]}.

    ``ci_low``/``ci_high``/``null_value`` must be on the same scale (ratios: the
    exponentiated interval with null 1). ``egger`` accepts
    ``meta_analysis.eggers_test`` output ({p_value, df}) or the legacy
    {available, k, p} shape.
    """
    from ber_core.scientific import RANDOMIZED_CLASSES
    start = 0 if design_cls in RANDOMIZED_CLASSES else 2  # index into _GRADE_LEVELS
    downgrades = []

    n_rated = sum(rob.get(k, 0) for k in ("low", "some", "high"))
    if n_rated:
        high_frac = rob.get("high", 0) / n_rated
        some_or_high = (rob.get("high", 0) + rob.get("some", 0)) / n_rated
        if high_frac >= 0.5:
            downgrades.append({"domain": "risk_of_bias",
                               "reason": f"{rob.get('high', 0)}/{n_rated} studies at high risk of bias"})
        elif some_or_high >= 0.5:
            downgrades.append({"domain": "risk_of_bias",
                               "reason": "most studies have some concerns or high risk of bias"})

    if i2 is not None:
        if i2 >= 75:
            downgrades.append({"domain": "inconsistency", "reason": f"I2={i2:.0f}% (considerable)"})
            downgrades.append({"domain": "inconsistency", "reason": "I2>=75% (double downgrade)"})
        elif i2 >= 50:
            downgrades.append({"domain": "inconsistency", "reason": f"I2={i2:.0f}% (substantial)"})

    crosses_null = (ci_low is not None and ci_high is not None
                    and ci_low <= null_value <= ci_high)
    small_ois = (total_n is not None and total_n < 400) or n_trials < 3
    if crosses_null or small_ois:
        reason = []
        if crosses_null:
            reason.append("CI crosses null")
        if small_ois:
            reason.append("optimal information size not met")
        downgrades.append({"domain": "imprecision", "reason": "; ".join(reason)})

    if egger:
        p = egger.get("p", egger.get("p_value"))
        k = egger.get("k") or ((egger["df"] + 2) if egger.get("df") is not None else 0)
        if egger.get("available", p is not None) and k >= EGGER_MIN_K and p is not None and p < 0.10:
            downgrades.append({"domain": "publication_bias",
                               "reason": f"Egger p={p:.3f} (k={k})"})

    for _ in range(max(0, int(indirectness_penalty))):
        downgrades.append({"domain": "indirectness", "reason": "assessed indirect"})

    level = min(len(_GRADE_LEVELS) - 1, start + len(downgrades))
    return {"grade": _GRADE_LEVELS[level], "start": _GRADE_LEVELS[start],
            "downgrades": downgrades}


# ── Meta-analysis feasibility ────────────────────────────────────────────────
# A Bird's Eye Review is a DASHBOARD covering all papers on a topic; a pooled
# estimate is an optional extra that many topics genuinely cannot support.
# ``assess_poolability`` answers "can this literature be meta-analysed, and if
# not, why" by delegating to ``ber_core.scientific.feasibility``, whose overall
# verdict is ``supported`` or ``not_possible`` with per-comparison groups
# (``poolable`` / ``too_few``) and the counted reasons estimates were withheld.


# Acronyms are the reliable identity signal in these strings; everything else is
# prose around them.
_INSTR_PAREN = re.compile(r"\(([a-z][a-z0-9\-]{1,12})\)")
_INSTR_LEAD = re.compile(r"^([a-z]{2,10}[\-0-9]*)\b")
_INSTR_STOPWORDS = {"international", "the", "change", "mean", "total", "score",
                    "number", "percentage", "proportion", "improvement"}


def normalize_instrument(name: str) -> str:
    """Collapse spellings of one instrument to a single key.

    "IRLS", "international restless legs syndrome rating scale (IRLS)" and
    "IRLS (International RLS Study Group rating scale)" are the same scale; a
    fragmentation metric that treats them as three instruments would declare a
    well-poolable domain unpoolable. Verified: 678 raw strings -> 128 keys on
    restless-legs symptom_severity, with IRLS correctly dominant.
    """
    s = (name or "").lower().strip()
    if not s:
        return ""
    paren = _INSTR_PAREN.findall(s)
    if paren:
        return paren[-1].replace("-", "")
    m = _INSTR_LEAD.match(s)
    tok = m.group(1).replace("-", "") if m else ""
    if tok in _INSTR_STOPWORDS:
        tail = re.search(r"\b([a-z]{2,8}\d{0,3})\b(?=[^a-z]*$)", s)
        tok = tail.group(1) if tail else tok
    return tok or s[:20]


def outcome_domain(outcome: dict) -> str:
    """An outcome's navigation domain (never a pool key; see ber_core.scientific)."""
    return (outcome.get("outcome_domain") or outcome.get("symptom_domain")
            or outcome.get("category") or "other")


def outcome_instrument(outcome: dict) -> str:
    return outcome.get("measurement_instrument") or outcome.get("name") or ""


def assess_poolability(records: list[dict], *, policy=None, min_trials=2) -> dict:
    from ber_core.scientific import feasibility
    return feasibility(records, policy, min_trials)


def reported_effects(outcome):
    """Keep endpoint effects; unspecified timepoints are a legacy fallback."""
    candidates = outcome.get("between_group_effects") or []
    if not outcome.get("timepoints"):
        return candidates
    tp = primary_timepoint(outcome)
    if tp is None:
        return []
    matching = [e for e in candidates if e.get("timepoint_id") == tp.get("timepoint_id")]
    return matching or [e for e in candidates if e.get("timepoint_id") is None]


@dataclass(frozen=True)
class SelectedEffect:
    estimate: float | None
    standard_error: float | None
    source: str | None
    study_id: str | None
    timepoint_id: object
    basis: str | None
    direction: str = "positive_favors_intervention"
    exclusion: str | None = None


def select_effect(outcome, roles, record):
    """Legacy single-SMD interface; refuse ambiguous or non-SMD comparisons."""
    from ber_core.scientific import selected_comparisons, select_timepoint
    selected = [s for s in selected_comparisons({**record, "outcomes": [outcome]})
                if s["poolable"] and s["effect_measure"] == "smd"]
    tp = select_timepoint(outcome)
    if len(selected) != 1:
        return SelectedEffect(None, None, None, record.get("paper_id"),
                              tp.get("timepoint_id") if tp else None, None,
                              exclusion="No single validated, explicitly mapped SMD comparison")
    s = selected[0]
    return SelectedEffect(s["effect"], s["standard_error"], s["source"], s["study_id"],
                          s["timepoint_id"], s["basis"], direction="intervention_minus_comparator")


def smd_from_outcome(outcome, roles, record):
    selected = select_effect(outcome, roles, record)
    if selected.exclusion:
        return None
    return selected.estimate, selected.standard_error, selected.source
