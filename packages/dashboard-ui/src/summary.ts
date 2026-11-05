/** Types for the pipeline-computed `summary.json` + `dashboard_rows.jsonl`.
 *
 *  These mirror `dashboard_summary.py` exactly. Every number a dashboard shows
 *  comes from there, computed ONCE in Python, so the studio preview and the
 *  published website page cannot disagree. A component that re-derives a count
 *  from the rows re-introduces precisely the drift this file exists to stop —
 *  the one exception is filtered counts, which are by definition not
 *  precomputable and go through `countDistinctTrials` below (the TS twin of
 *  `dashboard_summary.count_distinct_trials`). */

export const SCIENTIFIC_RULES_VERSION = "2026-10-03.1";

export type Verdict =
  | "favors_treatment"
  | "favors_control"
  | "no_difference"
  | "mixed"
  | "insufficient_data"
  | "inconclusive"
  | "unknown";

/** The sole definition of a trial's values per filterable dimension. Charts
 *  count over it and filters match over it, so a chart segment's number IS the
 *  row count when clicked — by construction rather than by convention. */
export interface RowFacets {
  interventionNames: string[];
  interventionCategories: string[];
  symptomDomains: string[];
  countries: string[];
  designType: string;
  blinding: string;
  rob: string;
  verdict: string;
  outcomeType: string;
}

export type FacetKey = keyof RowFacets;

export interface ScientificIssue { code: string; reason: string; path?: string; source_location?: unknown; source_quote?: unknown; extraction_version?: string; }
export interface ScientificComparison {
  paper_id: string; intervention: string; comparator: string; outcome_name: string;
  ci_low?: number | null; ci_high?: number | null; basis?: string; unit?: string;
  timepoint?: { label?: string }; effect: number | null; effect_measure?: string;
  significance: string; p_value: number | null; p_value_operator: string | null;
  source_location?: unknown; source_quote?: unknown; source_path?: unknown; source?: string;
  issues: ScientificIssue[]; poolable: boolean; rules_version?: string;
}

export interface TrialRow {
  id: string;
  study_id?: string;
  is_primary_report?: boolean;
  legacy_verdict?: string;
  doi: string;
  year: number | null;
  year_source: "metadata" | "doi_regex" | "missing";
  title: string;
  journal: string;
  /** Citation fields (schema v2+). Read defensively: a v1 bundle lacks them. */
  authors?: string;
  phase?: string | null;
  n: number | null;
  design_type: string;
  is_rct: boolean;
  blinding: string;
  rob: string;
  verdict: Verdict;
  intervention_verdicts?: Record<string, Verdict>;
  result_details?: { reason: string; selected: ScientificComparison[] };
  scientific_validation?: { rules_version: string; status: string; issues: ScientificIssue[] };
  verdict_source: "csv" | "proxy" | "none";
  primary_domain: string;
  primary_domains: string[];
  outcome_type: string;
  primary_name: string | null;
  primary_p_value: number | null;
  /** Comparison arm(s) — what the intervention was tested against. Optional:
   *  bundles built before these columns existed lack them entirely. */
  comparator?: string[] | null;
  /** The full PICO breakdown (schema v3+): population text, raw intervention
   *  names, comparator arms, and ALL outcome names (not just the primary).
   *  Read defensively — a v2 bundle lacks it and PICO cells render "—". */
  pico?: {
    population: string | null;
    intervention: string[];
    comparator: string[];
    outcome: string[];
  };
  /** Total trial duration in weeks (`follow_up.total_duration_weeks`). */
  follow_up_weeks?: number | null;
  /** Pass-1 organism verdict, joined in at export. Null for reviews screened
   *  before the organism migration — render "—", never assume "human". */
  organism?: string | null;
  eff: {
    measure: string | null;
    value: number | null;
    ci_low: number | null;
    ci_high: number | null;
    p: number | null;
    p_operator?: string | null;
  } | null;
  n_instruments: number;
  instruments: string[];
  countries: string[];
  facets: RowFacets;
}

export interface InterventionBar {
  name: string;
  count: number;
  category: string | null;
  verdicts: Record<Verdict, number>;
}

/** The meta-analysis feasibility verdict. Produced by `assess_poolability` and
 *  rendered verbatim by `FeasibilityCard` — the same block the published
 *  dashboard carries under `summary.feasibility`, and what the wizard's meta
 *  stage fetches live from `/reviews/{id}/feasibility`. */
export interface Feasibility {
  rules_version?: string;
  withheld?: ScientificComparison[];
  withheld_count?: number;
  overall: "not_possible" | "thin" | "fragmented" | "supported";
  n_papers: number;
  /** Papers with at least one POOLABLE outcome. */
  n_with_contrast: number;
  /** Papers that report a between-group comparison, poolable or not. Always
   *  >= n_with_contrast: effects stated without an SD/CI/SE count here but
   *  cannot be pooled. Optional — older published dashboards predate it. */
  n_with_comparison?: number;
  n_outcomes: number;
  n_pooled: number;
  n_comparisons?: number;
  recommendation: string;
  domains: { domain: string; verdict: string; [k: string]: unknown }[];
  blockers: { code: string; n: number; [k: string]: unknown }[];
}

export interface DashboardSummary {
  schema_version: number;
  scientific_rules_version?: string;
  slug: string;
  generated_at_utc: string;
  sizing: {
    n_rows: number;
    rows_bytes: number;
    feed_bytes: number;
    inline_threshold_bytes: number;
    mode: "inline" | "paged";
  };
  inclusion: {
    n_input: number;
    n_included: number;
    excluded: Record<string, number>;
  };
  vocab: {
    verdicts: Verdict[];
    verdict_colors: Record<string, string>;
    blinding_order: string[];
    rob_order: string[];
    outcome_types: { key: string; label: string; domains: string[] }[];
    facet_keys: FacetKey[];
    present: Record<string, string[]>;
  };
  totals: {
    n_trials: number;
    n_reports?: number;
    n_rct: number;
    n_observational: number;
    total_participants: number;
    n_countries: number;
    n_intervention_categories: number;
    n_interventions: number;
    n_instruments: number;
    n_with_year: number;
  };
  charts: {
    by_year: { year: number; count: number }[];
    by_country: { country: string; count: number }[];
    by_design: { design_type: string; count: number }[];
    by_rob: { rob: string; count: number }[];
    by_verdict: { verdict: string; count: number }[];
    by_symptom_domain: { domain: string; count: number }[];
    by_outcome_type: { key: string; count: number }[];
    by_intervention_category: { category: string; count: number }[];
    by_intervention: InterventionBar[];
    blinding_significance: {
      blinding: string;
      significant: number;
      not_significant: number;
      unknown: number;
    }[];
  };
  feasibility: Feasibility;
  provenance: { status: string; traceable_rate?: number | null };
  contract: {
    clean: boolean;
    missing_keys: Record<string, number>;
    unknown_rob: Record<string, number>;
    unknown_arm_type: Record<string, number>;
  };
  definitions: Record<string, unknown>;
}

export interface DashboardManifest {
  title: string;
  route: string;
  intervention_noun: string;
  panels_rendered: { key: string; label: string }[];
  panels_skipped: { key: string; label?: string; reason: string }[];
  available_panels: { key: string; label: string; why: string }[];
  /** Layout contract (manifests written by the dashboard-editor era). Absent on
   *  older bundles — the shell then falls back to its legacy gating. */
  layout_version?: number;
  /** Digest of the config's `dashboard` subtree at generation time; the server
   *  compares it to detect a layout edited after the last build. */
  layout_digest?: string;
  /** The PRE-resolve panel list (includes data-skipped panels). The editor
   *  seeds config from this, never from panels_rendered. */
  panels_requested?: string[];
  /** Results-table column keys (see RESULT_COLUMNS). */
  results_columns?: string[];
}

/** A trial's values for one facet. Single-valued facets are stored as scalars
 *  and wrapped here — the SOLE definition, used by both charts and filters. */
export function facetValues(facets: RowFacets, key: FacetKey): string[] {
  const v = facets[key];
  if (v == null) return [];
  return Array.isArray(v) ? v.filter(Boolean) : v ? [v] : [];
}

/** value -> number of DISTINCT trials carrying it. The TS twin of
 *  `dashboard_summary.count_distinct_trials`; a Python test asserts the
 *  precomputed charts equal this reduce over the same rows. */
export function representativeStudies(rows: TrialRow[]): TrialRow[] {
  const byStudy = new Map<string, TrialRow>();
  for (const r of [...rows].sort((a, b) => Number(!!b.is_primary_report) - Number(!!a.is_primary_report) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))) {
    const id = r.study_id || r.id;
    if (!byStudy.has(id)) byStudy.set(id, r);
  }
  const ids = new Set([...byStudy.values()].map(r => r.id));
  return rows.filter(r => ids.has(r.id));
}

export function countDistinctTrials(
  rows: TrialRow[],
  key: FacetKey,
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of representativeStudies(rows)) {
    for (const v of new Set(facetValues(r.facets, key))) {
      out[v] = (out[v] ?? 0) + 1;
    }
  }
  return out;
}

/** Rows passing every active filter. `filters` maps a facet key to selected
 *  values; an empty or absent set means "no constraint on this dimension". */
export function applyFilters(
  rows: TrialRow[],
  filters: Partial<Record<FacetKey, Set<string>>>,
): TrialRow[] {
  const active = Object.entries(filters).filter(
    ([, sel]) => sel && sel.size > 0,
  ) as [FacetKey, Set<string>][];
  if (active.length === 0) return rows;
  return rows.filter((r) =>
    active.every(([key, sel]) =>
      facetValues(r.facets, key).some((v) => sel.has(v)),
    ),
  );
}
