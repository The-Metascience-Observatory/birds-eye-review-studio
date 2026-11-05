/** The dashboard's editable layout: which panels render, which table columns show.
 *
 *  Three sources, in precedence order:
 *  1. A live `layout` override — the studio's dashboard editor passes the
 *     review's config directly so toggles apply instantly, without waiting for
 *     the manifest step to re-run.
 *  2. A v2 manifest (`layout_version >= 2`): strict gating on `panels_rendered`,
 *     which also carries the honest skip-reasons for panels whose data is
 *     missing.
 *  3. A legacy manifest (or none): the pre-editor behavior — only the three
 *     historically-gated charts (and prisma, which never rendered) consult the
 *     manifest; everything else is always-on. An old bundle therefore renders
 *     exactly as it always did.
 */

import type { DashboardManifest } from "./summary";

/** Every panel DashboardShell can render. Keys and labels mirror the Python
 *  registry in generate_dashboard.py (PANELS). */
export type PanelKey =
  // No "stat_tiles": the headline aggregate-count tiles were removed. Older
  // saved layouts may still carry the string; it matches nothing and is inert.
  | "feasibility"
  | "filter_design"
  | "filter_rob"
  | "filter_country"
  | "filter_outcome_type"
  | "intervention_grid"
  | "year_chart"
  | "design_breakdown"
  | "rob_breakdown"
  | "country_map"
  | "outcome_domains"
  | "results_table"
  | "forest_plots"
  | "prisma"
  | "heatmap"
  | "blinding_significance";

export const SHELL_PANELS: { key: PanelKey; label: string; why: string }[] = [
  { key: "feasibility", label: "Meta-analysis feasibility",
    why: "Whether pooling is honest for this topic, and what blocks it." },
  { key: "filter_design", label: "Study design filter",
    why: "Narrow every chart and the table to one study design." },
  { key: "filter_rob", label: "Risk of bias filter",
    why: "Narrow to low/high risk-of-bias trials." },
  { key: "filter_country", label: "Country filter",
    why: "Narrow to trials run in one country." },
  { key: "filter_outcome_type", label: "Outcome type filter",
    why: "Narrow to one outcome type." },
  { key: "intervention_grid", label: "Trials by intervention",
    why: "Every treatment being studied, coloured by result." },
  { key: "year_chart", label: "Trials by year",
    why: "Whether the field is growing, flat, or abandoned." },
  { key: "design_breakdown", label: "Study designs",
    why: "RCT vs observational vs preclinical — exposes gaps like 'no RCTs'." },
  { key: "rob_breakdown", label: "Risk of bias breakdown",
    why: "How much of the evidence is trustworthy at a glance." },
  { key: "country_map", label: "Trials by country",
    why: "Where the research is happening — a headline Bird's Eye question." },
  { key: "outcome_domains", label: "Outcome domains",
    why: "Which outcomes the field actually measures." },
  { key: "results_table", label: "Trials table",
    why: "Every included trial, sortable and searchable." },
  { key: "forest_plots", label: "Meta-analysis",
    why: "Pooled effects — only when the topic supports pooling." },
  { key: "prisma", label: "Screening flow",
    why: "How the corpus was narrowed, with exclusions and reasons." },
  { key: "heatmap", label: "Intervention x outcome landscape",
    why: "Which treatment/outcome pairs are studied, and which are empty." },
  { key: "blinding_significance", label: "Blinding vs significance",
    why: "Whether open-label trials report more positive results." },
];

export interface DashboardLayout {
  /** Panel keys to render, from config. Plain strings: config may carry keys
   *  this build doesn't know — unknown keys are ignored, never fatal. */
  panels: string[];
  /** Results-table column keys (see RESULT_COLUMNS in ResultsTable.tsx). */
  columns: string[];
}

/** Callbacks the studio's edit mode wires in. When absent, the dashboard is
 *  read-only (the website / full-width preview). */
export interface DashboardEdit {
  onRemovePanel: (key: PanelKey) => void;
  onToggleColumn: (key: string) => void;
}

/** The panels the pre-editor shell consulted the manifest for; everything else
 *  rendered unconditionally. Used only for legacy (pre-`layout_version`)
 *  manifests, so old bundles keep rendering identically. */
const LEGACY_GATED = new Set<string>([
  "intervention_grid", "year_chart", "country_map", "prisma",
  "heatmap", "blinding_significance",
]);

export function panelGate(
  manifest: DashboardManifest | null,
  layout?: DashboardLayout,
): (key: PanelKey) => boolean {
  if (layout) {
    const s = new Set(layout.panels);
    return (k) => s.has(k);
  }
  const rendered = new Set((manifest?.panels_rendered ?? []).map((p) => p.key));
  if ((manifest?.layout_version ?? 1) >= 2) return (k) => rendered.has(k);
  return (k) => (LEGACY_GATED.has(k) ? rendered.has(k) : true);
}
