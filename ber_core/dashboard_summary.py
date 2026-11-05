"""Dashboard aggregates, computed ONCE in Python at bundle-build time.

Every number a Bird's Eye dashboard displays is derived here and written to
``summary.json``. The studio preview and the generated website pages both READ
that file; neither re-derives anything. That is the whole point: two renderers
that each compute their own aggregates will eventually disagree (a hand-built
dashboard once showed an instrument count under one label from two different
definitions, 2-3x apart).

THE CORE DISTINCTION
--------------------
* **Per-trial derived values** (a trial's facets, verdict, primary domain, year,
  instruments) are *filter-independent* — pure functions of one record. Every
  disagreement bug found in the existing dashboards lives in this layer, so it
  is precomputed exhaustively into ``dashboard_rows.jsonl``.
* **Aggregates over a set of trials** are *filter-dependent*. A dashboard
  genuinely must recompute "trials per intervention" when the user ticks a
  checkbox, because the count is over rows passing every *other* filter. Those
  are precomputed only at the unfiltered baseline (``summary.charts``).

This preserves the project's stated ethic — "the end-user decides how to filter"
(``about-birds-eye-reviews.md``) — because the rows still ship, canonically
projected. Nothing is collapsed; ``summary.charts`` is just the ``filters = {}``
snapshot, and a test asserts a fresh reduce over the rows reproduces it exactly.

WHY THIS IS AN IMPORTABLE MODULE
--------------------------------
``export_dashboard.py`` and ``generate_dashboard.py`` both need this logic,
and numeric-prefixed scripts cannot be imported normally (the tests need an
``importlib.util.spec_from_file_location`` shim to load them at all). Anything
shared between those two — and exercised directly by tests — has to live in a
real module. Same rationale as ``grading.py``, which holds the statistics shared
by the export CLI and the GUI "so the two never drift".
"""
from __future__ import annotations

import re

from ber_core import grading as M
from ber_core.constants import is_failed_status
# The canonical DOI join key. Every cross-file lookup in the pipeline goes
# through this — see the contract note in utils.py: comparing a RAW paper_id
# against a normalized map silently never matches.
from ber_core.utils import norm_paper_id

# ── Schema/contract vocabulary ───────────────────────────────────────────────
# Per-record keys the dashboard reads. Missing keys are reported (not fatal):
# the dashboard tolerates nulls, but a *whole missing key* across many records
# usually means upstream schema drift worth surfacing.
REQUIRED_KEYS = (
    "paper_id", "study_design", "participants", "sample_sizes",
    "outcomes", "risk_of_bias",
)

# Known enum vocabularies the dashboard buckets on. Values outside these sets
# still pass through, but are reported so we know what the dashboard won't show.
KNOWN_ROB = {
    "low", "some_concerns", "high",            # RoB2 (RCT)
    "moderate", "serious", "critical", "no_information",  # ROBINS-I (observational)
    "unknown", None,
}
KNOWN_ARM_TYPE = {
    "intervention", "active_comparator", "placebo", "usual_care", "waitlist",
    "sham", "control", "comparator", "unexposed", "healthy_control",
    "historical_control", "pre_treatment", "post_treatment", "by_exposure", None,
}


# ── Schema-drift probe ───────────────────────────────────────────────────────

def validate_contract(records: list[dict]) -> dict:
    """Report missing required keys and out-of-vocabulary enum values.

    This is the codebase's schema-drift probe. It has historically written to
    stderr inside a job log where nobody sees it; ``summary.json`` gives it an
    output surface. Deliberately ONE probe with two consumers rather than a
    second implementation that could disagree with this one.
    """
    missing_key_counts: dict[str, int] = {}
    unknown_rob: dict[str, int] = {}
    unknown_arm_type: dict[str, int] = {}
    for r in records:
        for k in REQUIRED_KEYS:
            if k not in r or r[k] is None:
                missing_key_counts[k] = missing_key_counts.get(k, 0) + 1
        rob = (r.get("risk_of_bias") or {}).get("overall_judgment")
        if rob not in KNOWN_ROB:
            unknown_rob[rob] = unknown_rob.get(rob, 0) + 1
        for a in ((r.get("study_design") or {}).get("arms") or []):
            t = a.get("type")
            if t not in KNOWN_ARM_TYPE:
                unknown_arm_type[t] = unknown_arm_type.get(t, 0) + 1
    return {
        "missing_keys": missing_key_counts,
        "unknown_rob": unknown_rob,
        "unknown_arm_type": unknown_arm_type,
    }


# ── Headline metrics (also the information-loss guard) ───────────────────────

def chart_metrics(records: list[dict]) -> dict:
    """Headline metrics that back the dashboard's charts/summary tiles.

    A drop in any of these between the deployed feed and the new one is a sign
    of information loss, even when the record count holds steady — so this
    doubles as the shrink guard's input. Keep it stable: adding a field here
    makes it a publish-blocking tripwire.
    """
    countries: set[str] = set()
    categories: set[str] = set()
    total_participants = 0
    n_primary_symptom = 0
    n_intervention_arm = 0
    rob = {"low": 0, "some_concerns": 0, "high": 0}
    for r in records:
        sd = r.get("study_design") or {}
        for c in sd.get("countries") or []:
            countries.add(c)
        has_int = False
        for a in sd.get("arms") or []:
            if a.get("type") == "intervention":
                has_int = True
                if a.get("intervention_category"):
                    categories.add(a["intervention_category"])
        if has_int:
            n_intervention_arm += 1
        nrt = (r.get("sample_sizes") or {}).get("n_randomized_total")
        if isinstance(nrt, (int, float)):
            total_participants += nrt
        if any(o.get("is_primary") and o.get("symptom_domain")
               for o in (r.get("outcomes") or [])):
            n_primary_symptom += 1
        j = (r.get("risk_of_bias") or {}).get("overall_judgment")
        if j in rob:
            rob[j] += 1
    return {
        "n_records": len(records),
        "n_rct": sum(1 for r in records if record_is_rct(r)),
        "n_observational": sum(1 for r in records if not record_is_rct(r)),
        "total_participants": total_participants,
        "n_countries": len(countries),
        "n_intervention_categories": len(categories),
        "n_with_intervention_arm": n_intervention_arm,
        "n_with_primary_symptom": n_primary_symptom,
        "rob_low": rob["low"],
        "rob_some_concerns": rob["some_concerns"],
        "rob_high": rob["high"],
    }


# ── Outcome-domain buckets ───────────────────────────────────────────────────

def derive_outcome_types(records: list[dict]) -> list[dict]:
    """Group observed outcome domains into filter buckets.

    The domain->bucket map is the single most topic-specific thing in a
    dashboard. Deriving it from what the corpus actually contains beats asking
    the user to invent it up front, and it is overridable in config.

    Lives here (not in the generator) so ``summary.vocab.outcome_types`` and
    ``dashboard.manifest.json``'s copy are the same object by construction.
    """
    counts: dict[str, int] = {}
    for r in records:
        if is_failed_status(r.get("_status")):
            continue
        for o in r.get("outcomes") or []:
            d = M.outcome_domain(o)
            counts[d] = counts.get(d, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    top = [d for d, _ in ranked[:4] if d != "other"]
    out = [{"key": d, "label": d.replace("_", " ").capitalize(), "domains": [d]}
           for d in top]
    rest = [d for d, _ in ranked if d not in top]
    if rest:
        out.append({"key": "other", "label": "Other", "domains": rest})
    return out


# ── Vocabularies (fixed orders; the client must not invent its own) ──────────
# The six verdicts and their colours are duplicated across every dashboard's TS
# today. Shipping them in summary.json means a new panel cannot pick a seventh
# verdict or a different green.
VERDICTS = ("favors_treatment", "favors_control", "no_difference",
            "mixed", "inconclusive", "insufficient_data", "unknown")
VERDICT_COLORS = {
    "favors_treatment": "#16a34a", "favors_control": "#dc2626",
    "mixed": "#fdba74", "no_difference": "#94a3b8",
    "insufficient_data": "#cbd5e1", "inconclusive": "#cbd5e1", "unknown": "#e2e8f0",
}
BLINDING_ORDER = ("double_blind", "single_blind", "open_label", "unknown")
ROB_ORDER = ("low", "some_concerns", "moderate", "serious", "high",
             "critical", "no_information", "unknown")

# Rows at or below this shipped size go to the client whole and filter in the
# browser; larger corpora are served paged. ONE constant, echoed into
# summary.sizing so no consumer ever hardcodes it.
INLINE_THRESHOLD_BYTES = 3 * 1024 * 1024

# design_type strings that mean "this is not a clinical trial", dropped before
# any counting (mirrors the exclusion long-covid's page.tsx applies).
_NOT_A_TRIAL = re.compile(r"not applicable|bioinformatic|not a clinical trial", re.I)
_YEAR_IN_DOI = re.compile(r"20[12]\d")


def record_is_rct(record: dict) -> bool:
    """Randomized? The export stamps ``is_rct`` from ber_core.scientific (the
    single randomization definition); a record that never went through the
    export is classified the same way rather than defaulting to False."""
    flag = record.get("is_rct")
    if isinstance(flag, bool):
        return flag
    from ber_core.scientific import is_randomized
    return is_randomized(record)


def norm_design_type(raw) -> str:
    """Display label for the design-type chart: lowercase, underscore, and alias
    design types. Blank -> 'unknown'. This is a DISPLAY vocabulary only;
    whether a design is randomized comes from :func:`record_is_rct`.

    `pre_post` and `before_after` are the same design under two names; leaving
    them split would put one study type in two bars.
    """
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not s:
        return "unknown"
    return {"pre_post": "before_after"}.get(s, s)


def norm_blinding(raw) -> str:
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return s or "unknown"


def _year_of(record: dict) -> tuple[int | None, str]:
    """(year, source). Falls back to a DOI year, which is load-bearing.

    The bibliographic backfill in export_dashboard.py fills `year` for
    essentially every record (nattokinase: 6/30 before, 29/29 after), so this
    fallback is a safety net for runs made with --no-metadata-fetch, not the
    primary source. Do not read a sparse year chart as a property of the corpus
    without first checking that the backfill actually ran.
    """
    y = record.get("year")
    if isinstance(y, str) and y.strip().isdigit():
        y = int(y.strip())
    if isinstance(y, int) and 1900 < y < 2100:
        return y, "metadata"
    m = _YEAR_IN_DOI.search(str(record.get("paper_id") or ""))
    if m:
        return int(m.group()), "doi_regex"
    return None, "missing"


# ── Verdicts from the same comparisons used by synthesis ─────────────────────

def structured_verdict(record: dict) -> str:
    """Primary comparison results; incomplete evidence stays unresolved."""
    from ber_core.scientific import summarize_results
    return summarize_results(record)["verdict"]


def resolve_verdict(record: dict, csv_verdicts: dict) -> tuple[str, str]:
    """Historical CSV labels remain annotations, never override current evidence."""
    return structured_verdict(record), "proxy"


# ── The canonical per-trial projection ───────────────────────────────────────
# `facets` is the single definition of a trial's values for each filterable
# dimension. Charts count over it and filters match over it, so a chart
# segment's number IS the row count when clicked -- by construction. The
# website enforces this today only via a dev-only console.warn that compares
# two independently-built projections (long-covid/page.tsx:494-507).
FACET_KEYS = ("interventionNames", "interventionCategories", "symptomDomains",
              "countries", "designType", "blinding", "rob", "verdict",
              "outcomeType")

# Arm types that are not an intervention being studied, so they must not
# contribute an intervention name to the grid.
_NON_INTERVENTION_ARMS = {"placebo", "usual_care", "waitlist", "sham", "control",
                          "active_comparator", "active_placebo", "standard_care", "no_treatment",
                          "comparator", "unexposed", "healthy_control",
                          "historical_control", "pre_treatment", "by_exposure"}


def _stitch_scalars(d: dict, *, limit: int = 4) -> str:
    """Render the first few scalar key:value pairs of a dict as one line.

    The `participants` schema is PROJECT-SPECIFIC — long_covid carries
    `required_symptoms`/`long_covid_definition`, others carry different keys —
    so PICO's Population can't assume a field. This composes something honest
    from whatever the project actually extracted, ignoring provenance (`*_source`)
    and empty values.
    """
    parts: list[str] = []
    for k, v in d.items():
        if k.endswith("_source") or v in (None, "", [], {}):
            continue
        if isinstance(v, (str, int, float)):
            label = k.replace("_", " ")
            parts.append(f"{label}: {v}")
        if len(parts) >= limit:
            break
    return "; ".join(parts)


def compose_pico(rec: dict) -> dict:
    """Population / Intervention / Comparator / Outcome, composed defensively.

    Never assumes a schema: Population prefers a human summary field if the
    project extracted one, else stitches whatever participant scalars exist.
    """
    sd = rec.get("study_design") or {}
    parts = rec.get("participants") or {}
    arms = sd.get("arms") or []

    # Prefer a human population field, then the inclusion criteria (the key most
    # projects carry), then a defensive stitch of whatever scalars exist.
    inclusion = parts.get("key_inclusion_criteria")
    if isinstance(inclusion, list):
        inclusion = "; ".join(str(x) for x in inclusion if x)
    population = (
        parts.get("description")
        or parts.get("population")
        or parts.get("condition")
        or inclusion
        or _stitch_scalars(parts)
    )
    interventions = [
        a.get("intervention_name")
        for a in arms
        if a.get("type") == "intervention" and a.get("intervention_name")
    ]
    comparators = [
        a.get("intervention_name") or a.get("type")
        for a in arms
        if a.get("type") in _NON_INTERVENTION_ARMS
    ]
    outcomes = [o.get("name") for o in (rec.get("outcomes") or []) if o.get("name")]
    return {
        "population": population or None,
        "intervention": interventions,
        "comparator": comparators,
        "outcome": outcomes,
    }


def facet_values(facets: dict, key: str) -> list[str]:
    """A trial's values for one facet. The SOLE definition, used by both sides.

    Single-valued facets are stored as scalars and wrapped here, matching the
    website's FACET_VALUES contract so the shipped TS reducer can consume these
    rows unchanged.
    """
    v = facets.get(key)
    if v is None:
        return []
    if isinstance(v, list):
        return sorted({x for x in v if x})
    return [v] if v else []


def representative_studies(rows: list[dict]) -> list[dict]:
    """One verified study per descriptive chart; keep every report in the table.

    Prefer an explicitly primary report, then stable report ID. This is unrelated
    to statistical precision; comparison selection remains estimate-specific.
    """
    by_study = {}
    for row in sorted(rows, key=lambda r: (not r.get("is_primary_report", False), r["id"])):
        by_study.setdefault(row.get("study_id") or row["id"], row)
    selected = {r["id"] for r in by_study.values()}
    return [r for r in rows if r["id"] in selected]


def count_distinct_trials(rows: list[dict], key: str) -> dict[str, int]:
    """value -> number of DISTINCT trials carrying it.

    The Python twin of the website's countDistinctTrials. A trial listing one
    country twice counts once.
    """
    out: dict[str, int] = {}
    for r in representative_studies(rows):
        for v in set(facet_values(r.get("facets") or {}, key)):
            out[v] = out.get(v, 0) + 1
    return out


def exclusion_reason(record: dict, screened_out: set) -> str | None:
    """Why this record is not in the dashboard, or None to include it.

    Ordered most-specific-first so each dropped record is attributed to its real
    cause. Every exclusion is counted and reported: a silently dropped record
    makes the headline tiles unexplainable.

    NOTE ON `screened_out`: this deliberately does NOT re-apply
    `trial_screening.csv`'s `is_excluded` flag. The exported feed is ALREADY the
    export's eligibility decision, reached via `utils.load_exclusion_dois` plus
    the indication classifier. Re-screening here measured as dropping 27 of
    nattokinase's 30 papers -- mostly rows whose `is_excluded=yes` predates the
    extraction, and 14 of which carry no exclusion_reason at all. A second gate
    that silently discards 90% of a feed the first gate deliberately kept is the
    "coverage up, correctness down" failure, so the parameter is accepted (for
    callers that genuinely have a post-export screen) but the export passes an
    empty set. See test_dashboard_summary.test_the_exported_feed_is_not_rescreened.
    """
    if is_failed_status(record.get("_status")):
        return "status_failed"
    if not isinstance(record.get("study_design"), dict):
        return "no_study_design"
    dt = (record.get("study_design") or {}).get("design_type") or ""
    if _NOT_A_TRIAL.search(str(dt)):
        return "not_a_clinical_trial"
    if norm_paper_id(record.get("paper_id")) in screened_out:
        return "screened_out"
    return None


def project_row(record: dict, *, csv_verdicts: dict, domain_to_type: dict,
                meta: dict | None = None) -> dict:
    """One record -> one canonical dashboard row.

    Pure function of a single record: everything here is filter-independent,
    which is precisely why it can be precomputed once and shared.
    """
    sd = record.get("study_design") or {}
    pid = str(record.get("paper_id") or "")
    doi = pid.split("#")[0]
    meta = meta or {}

    interventions, categories = [], []
    comparators: list[str] = []
    for a in sd.get("arms") or []:
        if a.get("type") in _NON_INTERVENTION_ARMS:
            # The other side of the comparison — what the intervention was tested
            # AGAINST. Falls back to the arm type ("placebo") when the extraction
            # gave no explicit name, which is the common case for placebo arms.
            comp = (a.get("intervention_name") or a.get("label")
                    or (a.get("type") or "")).strip()
            if comp:
                comparators.append(comp)
            continue
        name = (a.get("intervention_canonical") or a.get("intervention_name") or "").strip()
        if name:
            interventions.append(name.lower())
        cat = (a.get("intervention_category") or "").strip()
        if cat:
            categories.append(cat)

    outcomes = record.get("outcomes") or []
    primaries = [o for o in outcomes if o.get("is_primary")] or outcomes[:1]
    # RULING: emit BOTH domain shapes rather than pretending they are one field.
    # The table wants one domain; the facet wants all of them, so that a trial
    # with two primary domains appears under both bars and the chart count still
    # equals the filtered row count.
    all_domains = sorted({M.outcome_domain(o) for o in primaries})
    primary_domain = M.outcome_domain(primaries[0]) if primaries else "other"

    instruments = sorted({M.normalize_instrument(M.outcome_instrument(o))
                          for o in outcomes if M.outcome_instrument(o)} - {""})

    verdict, vsource = resolve_verdict(record, csv_verdicts)
    from ber_core.scientific import summarize_results, validate_record, study_identity
    result_summary = summarize_results(record)
    year, ysource = _year_of({**record, **({"year": meta.get("year")} if meta.get("year") else {})})
    rob = ((record.get("risk_of_bias") or {}).get("overall_judgment")) or "unknown"
    design = norm_design_type(sd.get("design_type")
                              or ("rct" if record.get("is_rct") else ""))
    blinding = norm_blinding(sd.get("blinding"))
    otype = domain_to_type.get(primary_domain, "other")

    p_primary = None
    eff = None
    if primaries:
        selected = result_summary["selected"]
        if selected:
            e = selected[0]
            eff = {"measure": e.get("effect_measure"), "value": e.get("effect"),
                   "ci_low": e.get("ci_low"), "ci_high": e.get("ci_high"),
                   "p": e.get("p_value"), "p_operator": e.get("p_value_operator")}
            p_primary = e.get("p_value")

    return {
        "id": pid,
        "study_id": study_identity(record),
        "is_primary_report": record.get("is_primary_report") is True,
        "legacy_verdict": csv_verdicts.get(doi),
        "doi": doi,
        "year": year,
        "year_source": ysource,
        "title": (record.get("title") or meta.get("title") or "")[:250],
        "journal": record.get("journal") or meta.get("journal") or "",
        # Citation fields for the dashboard's hover cards and table. `phase` is
        # emitted even when absent from the extraction schema so the contract
        # carries the field and it can be populated without a second bump.
        "authors": (meta.get("authors") or record.get("authors") or "")[:300],
        "phase": sd.get("phase") or None,
        "n": (record.get("sample_sizes") or {}).get("n_randomized_total"),
        "design_type": design,
        "is_rct": record_is_rct(record),
        "blinding": blinding,
        "rob": rob,
        "verdict": verdict,
        "verdict_source": vsource,
        "result_details": result_summary,
        "scientific_validation": validate_record(record),
        "intervention_verdicts": {
            (a.get("intervention_canonical") or a.get("intervention_name") or "").lower():
            result_summary["by_arm"].get(str(a.get("arm_id")), "insufficient_data")
            for a in sd.get("arms") or [] if a.get("type") == "intervention"
        },
        "primary_domain": primary_domain,
        "primary_domains": all_domains,
        "outcome_type": otype,
        "primary_name": (primaries[0].get("name") if primaries else None),
        "primary_p_value": p_primary,
        "eff": eff,
        # The comparison arm(s) and the trial's total duration. Both come
        # straight from the pass-2 schema — no re-extraction needed.
        "comparator": comparators,
        # The full PICO breakdown (population text, raw intervention names, the
        # comparator arms, and ALL outcome names — not just the primary). Same
        # extraction data as the fields above, kept as one block so every table
        # (evidence + dashboard) reads the same composition.
        "pico": compose_pico(record),
        "follow_up_weeks": (record.get("follow_up") or {}).get(
            "total_duration_weeks"),
        # Organism is NOT in the pass-2 schema. It is joined in from the pass-1
        # screen by the caller (export_dashboard) and left None here, so a
        # paper screened before the organism migration renders "—" rather than a
        # fabricated "human".
        "organism": record.get("organism"),
        "n_instruments": len(instruments),
        "instruments": instruments,
        "countries": list(sd.get("countries") or []),
        "facets": {
            "interventionNames": sorted(set(interventions)),
            "interventionCategories": sorted(set(categories)),
            "symptomDomains": all_domains,
            "countries": sorted({c for c in (sd.get("countries") or []) if c}),
            "designType": design,
            "blinding": blinding,
            "rob": rob,
            "verdict": verdict,
            "outcomeType": otype,
        },
    }


def build_rows(records: list[dict], *, csv_verdicts: dict | None = None,
               screened_out: set | None = None,
               outcome_types: list[dict] | None = None,
               metadata: dict | None = None) -> tuple[list[dict], dict]:
    """(rows, inclusion) — the projection plus a full account of what was dropped."""
    csv_verdicts = csv_verdicts or {}
    screened_out = screened_out or set()
    metadata = metadata or {}
    outcome_types = outcome_types if outcome_types is not None else derive_outcome_types(records)
    domain_to_type = {d: t["key"] for t in outcome_types for d in t.get("domains") or []}

    rows, excluded, excluded_ids = [], {}, []
    for rec in records:
        why = exclusion_reason(rec, screened_out)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            excluded_ids.append(str(rec.get("paper_id") or ""))
            continue
        # normalize: the metadata map is keyed lowercase (export_dashboard
        # `_load_local_metadata`). A raw-case paper_id silently missed it, and
        # the row then lost journal/authors AND reported the DOI-string year
        # instead of the real one — wrong, not merely blank.
        doi = norm_paper_id(rec.get("paper_id"))
        rows.append(project_row(rec, csv_verdicts=csv_verdicts,
                                domain_to_type=domain_to_type,
                                meta=metadata.get(doi) or {}))
    return rows, {
        "n_input": len(records),
        "n_included": len(rows),
        "excluded": excluded,
        "excluded_ids": excluded_ids[:200],
        "n_excluded_ids_truncated": max(0, len(excluded_ids) - 200),
    }


# ── Baseline aggregates (the `filters = {}` snapshot) ────────────────────────

def _desc(counts: dict[str, int], key: str) -> list[dict]:
    """Count-descending, name-ascending — a stable order across runs."""
    return [{key: k, "count": v}
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def aggregate(rows: list[dict]) -> dict:
    """Totals + every chart at zero filters.

    Each chart is DEFINITIONALLY `count_distinct_trials(rows, facet)`, so a
    client reducing the same rows with no filters active must reproduce these
    numbers exactly. That equality is the test that stops studio and website
    from ever disagreeing.
    """
    n_reports = len(rows)
    rows = representative_studies(rows)
    n = len(rows)
    instruments = {i for r in rows for i in r["instruments"]}
    participants = sum(r["n"] for r in rows if isinstance(r.get("n"), (int, float)))

    by_year_counts: dict[int, int] = {}
    for r in rows:
        if r["year"] is not None:
            by_year_counts[r["year"]] = by_year_counts.get(r["year"], 0) + 1
    by_year = []
    if by_year_counts:
        # Zero-fill so a gap year is a visible trough, not a missing bar that
        # silently compresses the x-axis.
        for y in range(min(by_year_counts), max(by_year_counts) + 1):
            by_year.append({"year": y, "count": by_year_counts.get(y, 0)})

    # The intervention grid: per-name verdict breakdown + dominant category.
    grid: dict[str, dict] = {}
    for r in rows:
        for name in facet_values(r["facets"], "interventionNames"):
            g = grid.setdefault(name, {"name": name, "count": 0,
                                       "verdicts": {v: 0 for v in VERDICTS},
                                       "_cats": {}})
            g["count"] += 1
            verdict = (r.get("intervention_verdicts") or {}).get(name, r["verdict"])
            g["verdicts"][verdict] = g["verdicts"].get(verdict, 0) + 1
            for c in facet_values(r["facets"], "interventionCategories"):
                g["_cats"][c] = g["_cats"].get(c, 0) + 1
    by_intervention = []
    for g in sorted(grid.values(), key=lambda x: (-x["count"], x["name"])):
        cats = g.pop("_cats")
        g["category"] = max(cats, key=lambda k: (cats[k], k)) if cats else None
        by_intervention.append(g)

    blinding_sig = []
    for b in [x for x in BLINDING_ORDER
              if any(r["blinding"] == x for r in rows)]:
        sub = [r for r in rows if r["blinding"] == b]
        sig = sum(1 for r in sub if any(s.get("significance") == "significant" for s in (r.get("result_details") or {}).get("selected", [])))
        nsig = sum(1 for r in sub if (r.get("result_details") or {}).get("selected") and all(s.get("significance") == "not_detected" for s in r["result_details"]["selected"]))
        blinding_sig.append({"blinding": b, "significant": sig,
                             "not_significant": nsig,
                             "unknown": len(sub) - sig - nsig})

    return {
        "totals": {
            "n_trials": n,
            "n_reports": n_reports,
            "n_rct": sum(1 for r in rows if r["is_rct"]),
            "n_observational": sum(1 for r in rows if not r["is_rct"]),
            "total_participants": participants,
            "n_countries": len(count_distinct_trials(rows, "countries")),
            "n_intervention_categories": len(
                count_distinct_trials(rows, "interventionCategories")),
            "n_interventions": len(grid),
            # RULING: a GLOBAL DISTINCT set of normalized instruments, not a sum
            # of per-trial counts. The summed variant is not a count of
            # instruments at all -- it is instrument-uses -- and prints 3x
            # larger (restless-legs: 1572 vs 529). The distinct number is also
            # the one assess_poolability cites when it calls a topic fragmented,
            # so the tile and the feasibility verdict now use one vocabulary.
            "n_instruments": len(instruments),
            "n_with_year": sum(1 for r in rows if r["year"] is not None),
        },
        "charts": {
            "by_year": by_year,
            "by_country": _desc(count_distinct_trials(rows, "countries"), "country"),
            "by_design": _desc(count_distinct_trials(rows, "designType"), "design_type"),
            "by_rob": _desc(count_distinct_trials(rows, "rob"), "rob"),
            "by_verdict": _desc(count_distinct_trials(rows, "verdict"), "verdict"),
            "by_symptom_domain": _desc(
                count_distinct_trials(rows, "symptomDomains"), "domain"),
            "by_outcome_type": _desc(
                count_distinct_trials(rows, "outcomeType"), "key"),
            "by_intervention_category": _desc(
                count_distinct_trials(rows, "interventionCategories"), "category"),
            "by_intervention": by_intervention,
            "blinding_significance": blinding_sig,
        },
    }


# ── The whole document ───────────────────────────────────────────────────────

# 2: rows gained `authors` + `phase` for citation rendering. Consumers must read
# them defensively (`r.authors ?? ""`) so a v1 bundle still renders, just without
# an author line, rather than crashing.
# 3: rows gained `pico` (population / intervention / comparator / all outcome
# names). Read defensively — a v2 bundle lacks it and PICO cells render "—".
SCHEMA_VERSION = 4


def _present(rows: list[dict], key: str, order: tuple[str, ...] | None = None) -> list[str]:
    """Option list for a dropdown: only values actually present.

    Fixed-vocabulary facets keep their canonical order (so "low" always precedes
    "high"); open vocabularies sort alphabetically.
    """
    seen = set(count_distinct_trials(rows, key))
    if order:
        return [v for v in order if v in seen]
    return sorted(seen)


def build_summary(records: list[dict], *, slug: str, rows: list[dict],
                  inclusion: dict, outcome_types: list[dict],
                  feasibility: dict, rows_bytes: int,
                  feed_bytes: int = 0, provenance: dict | None = None,
                  generated_at: str = "") -> dict:
    """Assemble summary.json. `rows` must be the same list that gets written."""
    agg = aggregate(rows)
    vsrc: dict[str, int] = {}
    ysrc: dict[str, int] = {}
    for r in rows:
        vsrc[r["verdict_source"]] = vsrc.get(r["verdict_source"], 0) + 1
        ysrc[r["year_source"]] = ysrc.get(r["year_source"], 0) + 1

    contract = validate_contract(records)
    contract = {**contract,
                "clean": not any(contract[k] for k in
                                 ("missing_keys", "unknown_rob", "unknown_arm_type"))}

    from ber_core.scientific import RULES_VERSION
    compact_feasibility = {k:v for k,v in feasibility.items() if k != "withheld"}
    compact_feasibility["withheld_count"] = len(feasibility.get("withheld") or [])
    # Full field-level evidence lives in meta_analysis.json, not the eager summary.
    compact_feasibility["withheld"] = [
        {**{k: s.get(k) for k in ("paper_id", "intervention", "comparator", "outcome_name", "timepoint",
                                 "source_path", "extraction_version", "poolable", "rules_version")},
         "issues": [{k:i.get(k) for k in ("code", "reason", "path", "source_location", "extraction_version")} for i in s["issues"]]}
        for s in (feasibility.get("withheld") or [])[:20]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "scientific_rules_version": RULES_VERSION,
        "slug": slug,
        "generated_at_utc": generated_at,
        "generator": "export_dashboard.py",
        "sizing": {
            "n_rows": len(rows),
            "rows_bytes": rows_bytes,
            "feed_bytes": feed_bytes,
            "inline_threshold_bytes": INLINE_THRESHOLD_BYTES,
            # Computed and WRITTEN, never inferred by a reader -- so the two
            # loading paths can never disagree about which one they are on.
            "mode": "inline" if rows_bytes <= INLINE_THRESHOLD_BYTES else "paged",
        },
        "inclusion": inclusion,
        "vocab": {
            "verdicts": list(VERDICTS),
            "verdict_colors": dict(VERDICT_COLORS),
            "blinding_order": list(BLINDING_ORDER),
            "rob_order": list(ROB_ORDER),
            "outcome_types": outcome_types,
            "facet_keys": list(FACET_KEYS),
            "present": {
                "verdicts": _present(rows, "verdict", VERDICTS),
                "blinding": _present(rows, "blinding", BLINDING_ORDER),
                "rob": _present(rows, "rob", ROB_ORDER),
                "design_types": _present(rows, "designType"),
                "countries": _present(rows, "countries"),
                "intervention_categories": _present(rows, "interventionCategories"),
                "intervention_names": _present(rows, "interventionNames"),
                "symptom_domains": _present(rows, "symptomDomains"),
                "outcome_type_keys": _present(rows, "outcomeType"),
            },
        },
        **agg,
        "feasibility": compact_feasibility,
        "provenance": provenance or {"status": "unavailable"},
        "contract": contract,
        # What each ambiguous number MEANS, so a consumer never has to guess and
        # a reviewer can see when a definition changes.
        "definitions": {
            "n_instruments": "global_distinct_normalized",
            "study_counts": "Verified study links; descriptive charts prefer an explicitly primary report then stable report ID. All reports remain in the table.",
            "primary_domain": "grading.outcome_domain on first primary outcome",
            "verdict_source": ("csv" if vsrc.get("csv") and not vsrc.get("proxy")
                               else "structured_proxy" if not vsrc.get("csv")
                               else "mixed"),
            "verdict_source_counts": vsrc,
            "year_source_counts": ysrc,
        },
    }
