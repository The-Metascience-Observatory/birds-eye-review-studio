/** Bridges the pipeline's precomputed data to the chart components' prop shapes.
 *
 *  THIS FILE IS WHERE THE NO-RE-DERIVATION RULE IS ENFORCED.
 *
 *  Every function that can return a baseline takes `summary` first and
 *  `filtered: TrialRow[] | null` last. When `filtered === null` (no filter
 *  active) the result is a pure re-key of `summary.charts` with **zero
 *  arithmetic** — the numbers on screen are the numbers Python computed, which
 *  is what makes a studio preview and a published dashboard incapable of
 *  disagreeing. Only the `filtered !== null` branches count anything, and a
 *  filtered count is by definition not precomputable.
 *
 *  `server/tests/test_dashboard_summary.py` asserts the Python side of this
 *  equality; `adapters.test.ts` asserts the TypeScript side reproduces it. */

import type { HoverTrial, Segment } from "./hover/trialHoverPanel";
import {
  countDistinctTrials,
  representativeStudies,
  facetValues,
  type DashboardSummary,
  type FacetKey,
  type InterventionBar,
  type TrialRow,
  type Verdict,
} from "./summary";
import { VERDICT_LABEL } from "./labels";
import { formatLabel } from "./utils";

/** Which facet each precomputed chart is the reduce of.
 *
 *  These SEVEN PAIRS are the same ones asserted in
 *  `test_dashboard_summary.test_precomputed_charts_equal_a_fresh_reduce_over_the_rows`.
 *  Keeping them literally side by side is what makes a drift visible in review:
 *  if a chart is ever re-keyed to a different facet in Python, this map is the
 *  place that must change too. */
const FACET_FOR_CHART = {
  by_country: "countries",
  by_design: "designType",
  by_rob: "rob",
  by_verdict: "verdict",
  by_symptom_domain: "symptomDomains",
  by_outcome_type: "outcomeType",
  by_intervention_category: "interventionCategories",
} as const;

export type BarChartKey = keyof typeof FACET_FOR_CHART;

/** The key each precomputed chart's entries are labelled with. */
const LABEL_KEY: Record<BarChartKey, string> = {
  by_country: "country",
  by_design: "design_type",
  by_rob: "rob",
  by_verdict: "verdict",
  by_symptom_domain: "domain",
  by_outcome_type: "key",
  by_intervention_category: "category",
};

/** Stacked-bar segments, straight from the shipped vocabulary.
 *
 *  Order comes from `vocab.verdicts`, i.e. from Python. That matters beyond
 *  aesthetics: `TiledSegment` indexes hover trials by position within a
 *  segment, so a reordering here would silently re-point tiles at the wrong
 *  studies. */
export function verdictSegments(summary: DashboardSummary): Segment[] {
  return summary.vocab.verdicts.map((v) => ({
    key: v,
    label: VERDICT_LABEL[v] ?? formatLabel(v),
    color: summary.vocab.verdict_colors[v] ?? "#e2e8f0",
  }));
}

/** BASELINE. A pure re-key of `summary.charts.by_intervention` — no arithmetic.
 *
 *  `BreakdownChart` re-sums each row into its own `total`, which therefore
 *  equals the precomputed `count` by construction (a Python test already
 *  asserts the segments sum to the count). */
export function breakdownFromBars(
  bars: InterventionBar[],
): Record<string, Record<string, number>> {
  return Object.fromEntries(bars.map((b) => [b.name, { ...b.verdicts }]));
}

/** FILTERED ONLY. Rebuild the segmented breakdown for a row subset.
 *
 *  Invariant: for every key, the segment values sum to
 *  `countDistinctTrials(rows, facet)[key]`, because the inner increment happens
 *  once per (distinct facet value, row) pair — the same iteration
 *  `countDistinctTrials` performs. */
function breakdownFromRows(
  rows: TrialRow[],
  facet: FacetKey,
  verdicts: Verdict[],
): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {};
  const known = new Set<string>(verdicts);
  for (const r of representativeStudies(rows)) {
    // A verdict outside `vocab.verdicts` (vocab drift) would land in the bucket
    // but not in any rendered segment — silently vanishing from the bar and its
    // total while still showing in the hover panel. Fold it into "unknown" so
    // the trial stays counted.
    const verdict =
      known.has(r.verdict) || !known.has("unknown") ? r.verdict : "unknown";
    for (const v of new Set(facetValues(r.facets, facet))) {
      const specific = facet === "interventionNames" ? r.intervention_verdicts?.[v] ?? verdict : verdict;
      const bucket = (out[v] ??= Object.fromEntries(
        verdicts.map((k) => [k, 0]),
      ) as Record<string, number>);
      bucket[specific] = (bucket[specific] ?? 0) + 1;
    }
  }
  return out;
}

/** Simple `{key,label,count}[]` bars. Baseline = re-key; filtered = reduce. */
export function barsFrom(
  summary: DashboardSummary,
  chart: BarChartKey,
  filtered: TrialRow[] | null,
): { key: string; label: string; count: number }[] {
  if (filtered === null) {
    const labelKey = LABEL_KEY[chart];
    const entries = (summary.charts[chart] ?? []) as Record<string, unknown>[];
    return entries.map((e) => {
      const key = String(e[labelKey] ?? "");
      return { key, label: formatLabel(key), count: Number(e.count) };
    });
  }
  const counts = countDistinctTrials(filtered, FACET_FOR_CHART[chart]);
  return Object.entries(counts)
    .map(([key, count]) => ({ key, label: formatLabel(key), count }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

/** `{label: count}` for BreakdownChart's non-stacked mode. */
export function simpleBreakdown(
  bars: { key: string; count: number }[],
): Record<string, number> {
  return Object.fromEntries(bars.map((b) => [b.key, b.count]));
}

/** `{label: displayLabel}` so charts show "Double blind", not "double_blind". */
export function labelsFor(
  bars: { key: string; label: string }[],
): Record<string, string> {
  return Object.fromEntries(bars.map((b) => [b.key, b.label]));
}

/** Lead-author citation label. Mirrors `grading.first_author_surname`.
 *
 *  Presentation, so it stays in TS rather than being precomputed — but the
 *  underlying `authors` string comes from the projection. */
export function leadAuthor(authors: string | undefined | null): string {
  const raw = (authors ?? "").trim();
  if (!raw) return "";
  const parts = raw.split(/;| and |,(?=\s*[A-Z][a-z])/).filter((s) => s.trim());
  const first = (parts[0] ?? raw).trim();
  // "Smith, John" -> "Smith"; "John Smith" -> "Smith"
  const surname = first.includes(",")
    ? (first.split(",")[0] ?? "").trim()
    : (first.split(/\s+/).slice(-1)[0] ?? first);
  if (!surname) return "";
  return parts.length > 1 ? `${surname} et al.` : surname;
}

/** Per-chart-key study lists for the hover panel.
 *
 *  NOT a baseline derivation even when called with all rows: it produces a list
 *  of STUDIES, never a count. `TiledSegment` takes its tile count from the
 *  breakdown, so the numbers still come from `summary.charts` on the baseline
 *  path while only the tooltip's contents come from rows.
 *
 *  MUST be called with the same row array that produced the breakdown, or the
 *  tooltip lists studies the filter removed. Row ORDER also matters: tile k of a
 *  segment maps to the k-th study pushed here, so never sort rows between
 *  filtering and this call. */
export function hoverTrialsFrom(
  rows: TrialRow[],
  facet: FacetKey,
): Record<string, HoverTrial[]> {
  const out: Record<string, HoverTrial[]> = {};
  for (const r of representativeStudies(rows)) {
    for (const v of new Set(facetValues(r.facets, facet))) {
      (out[v] ??= []).push({
        doi: r.doi,
        label: leadAuthor(r.authors),
        title: r.title,
        year: r.year,
        journal: r.journal,
        n: r.n,
        design: formatLabel(r.design_type),
        phase: r.phase ?? null,
        verdict: facet === "interventionNames" ? r.intervention_verdicts?.[v] ?? r.verdict : r.verdict,
        note:
          r.verdict_source === "proxy"
            ? r.result_details?.reason ?? "Result inferred from extracted comparisons."
            : undefined,
      });
    }
  }
  return out;
}

/** Baseline intervention bars, or a filtered rebuild that preserves the
 *  baseline's ordering and category assignment so filtering never reshuffles
 *  the chart under the user. */
export function interventionBars(
  summary: DashboardSummary,
  filtered: TrialRow[] | null,
): InterventionBar[] {
  const baseline = summary.charts.by_intervention;
  if (filtered === null) return baseline;

  const counts = breakdownFromRows(
    filtered,
    "interventionNames",
    summary.vocab.verdicts,
  );
  const out: InterventionBar[] = [];
  for (const b of baseline) {
    const verdicts = counts[b.name];
    if (!verdicts) continue; // filtered out entirely
    out.push({
      name: b.name,
      category: b.category,
      verdicts: verdicts as Record<Verdict, number>,
      count: Object.values(verdicts).reduce((a, c) => a + c, 0),
    });
  }
  return out.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

/** Zero-filled year bins. Baseline is `summary.charts.by_year` verbatim. */
export function yearBars(
  summary: DashboardSummary,
  filtered: TrialRow[] | null,
): { year: number; count: number }[] {
  if (filtered === null) return summary.charts.by_year;
  const counts = new Map<number, number>();
  for (const r of representativeStudies(filtered)) {
    if (r.year != null) counts.set(r.year, (counts.get(r.year) ?? 0) + 1);
  }
  if (!counts.size) return [];
  const ks = [...counts.keys()];
  const out: { year: number; count: number }[] = [];
  for (let y = Math.min(...ks); y <= Math.max(...ks); y++) {
    out.push({ year: y, count: counts.get(y) ?? 0 });
  }
  return out;
}
