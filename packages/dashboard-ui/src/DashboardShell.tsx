"use client";

/** The whole Bird's Eye dashboard, composed once.
 *
 *  Both consumers render this: the studio preview (client-side, react-query)
 *  and a generated website page (RSC, fs read). That is the "one renderer"
 *  constraint made concrete — the alternative, generating per-review .tsx, gives
 *  two renderers guaranteed to drift.
 *
 *  CONTRACT: this component takes fully-loaded data and owns NO loading, error,
 *  or empty state. Those are studio concepts (a published dashboard is never
 *  unbuilt or stale), and letting them leak in here would add dead code to the
 *  RSC path and start the fork.
 *
 *  Every section is a PANEL gated through `panelGate` (see layout.ts): the
 *  studio's dashboard editor decides what renders via the `layout` override,
 *  a v2 manifest decides for published bundles, and legacy manifests keep the
 *  pre-editor behavior. The optional `edit` prop puts a remove button on each
 *  panel — studio edit mode only. */

import { X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import {
  barsFrom,
  breakdownFromBars,
  hoverTrialsFrom,
  interventionBars,
  labelsFor,
  simpleBreakdown,
  verdictSegments,
  yearBars,
  type BarChartKey,
} from "./adapters";
import { BlindingSignificanceChart } from "./BlindingSignificanceChart";
import { BreakdownChart } from "./BreakdownChart";
import { FilterCard } from "./FilterCard";
import { LandscapeHeatmap } from "./LandscapeHeatmap";
import { ForestPlot, type ForestGroup } from "./ForestPlot";
import { poolableGroups } from "./forestAdapter";
import {
  panelGate,
  type DashboardEdit,
  type DashboardLayout,
  type PanelKey,
} from "./layout";
import { ContractReport, FeasibilityCard } from "./panels";
import { PrismaFlow, type PrismaData } from "./PrismaFlow";
import { ResultsTable } from "./ResultsTable";
import {
  applyFilters,
  countDistinctTrials,
  representativeStudies,
  type DashboardManifest,
  type DashboardSummary,
  SCIENTIFIC_RULES_VERSION,
  type FacetKey,
  type TrialRow,
} from "./summary";
import { YearChart } from "./YearChart";

type Filters = Partial<Record<FacetKey, Set<string>>>;

/** Facets offered as filter cards, in display order. Each card is its own
 *  removable panel. */
const FILTERS: {
  facet: FacetKey;
  chart: BarChartKey;
  title: string;
  panel: PanelKey;
}[] = [
  { facet: "designType", chart: "by_design", title: "Study design", panel: "filter_design" },
  { facet: "rob", chart: "by_rob", title: "Risk of bias", panel: "filter_rob" },
  { facet: "countries", chart: "by_country", title: "Country", panel: "filter_country" },
  { facet: "outcomeType", chart: "by_outcome_type", title: "Outcome type", panel: "filter_outcome_type" },
];

export function DashboardShell({
  summary,
  manifest,
  rows,
  header,
  notice,
  audit = true,
  mapSlot,
  metaGroups,
  metaMinTrials,
  withheldComparisons,
  layout,
  edit,
  prisma,
}: {
  summary: DashboardSummary;
  manifest: DashboardManifest | null;
  rows: TrialRow[];
  /** Title block; the host owns it (studio shows build time, site shows a nav). */
  header?: ReactNode;
  /** Host-owned banner slot (e.g. the studio's "bundle is stale" warning). */
  notice?: ReactNode;
  /** Studio-only panels: skipped-panel reasons and the schema-drift report. A
   *  published dashboard has no business showing its own extraction warnings. */
  audit?: boolean;
  mapSlot?: ReactNode;
  /** Pooled meta-analysis groups (from `meta_analysis.json`). When present and
   *  poolable, a forest-plot section renders. Absent for most corpora. */
  metaGroups?: ForestGroup[];
  /** The `min_trials` the POOLING actually used (`meta_analysis.json.min_trials`).
   *
   *  Must be threaded through, not defaulted: the pipeline pools at 2 by default
   *  while `poolableGroups` filtered at 3, so a corpus pooled into 2-trial groups
   *  produced a file full of real estimates and rendered nothing at all. Two
   *  thresholds for one decision is the bug; this makes the pipeline's the only
   *  one. Omitted → `poolableGroups`' own default. */
  metaMinTrials?: number;
  withheldComparisons?: import("./summary").ScientificComparison[];
  /** Live layout override (the studio editor's instant preview). Absent →
   *  manifest-driven gating; see layout.ts for the precedence rules. */
  layout?: DashboardLayout;
  /** Edit-mode callbacks: when set, every panel gets a remove button and the
   *  table's column picker persists through `onToggleColumn`. */
  edit?: DashboardEdit;
  /** The screening funnel (from the bundle's `prisma.json`, or the studio's
   *  live endpoint). Renders only when the `prisma` panel is on AND data
   *  exists — the shell never fetches. */
  prisma?: PrismaData | null;
}) {
  const [filters, setFilters] = useState<Filters>({});

  const has = panelGate(manifest, layout);

  // Facets whose UI is visible. A hidden filter card must not keep filtering
  // the dashboard — a removed Country card with an active country selection
  // would leave the whole page silently narrowed with no visible cause.
  const allowedFacets = useMemo(() => {
    const s = new Set<FacetKey>(
      FILTERS.filter((f) => has(f.panel)).map((f) => f.facet),
    );
    if (has("intervention_grid")) s.add("interventionNames");
    return s;
  }, [has]);

  const effectiveFilters = useMemo(() => {
    const out: Filters = {};
    for (const [k, v] of Object.entries(filters) as [FacetKey, Set<string>][]) {
      if (allowedFacets.has(k) && v && v.size > 0) out[k] = v;
    }
    return out;
  }, [filters, allowedFacets]);

  const active = Object.values(effectiveFilters).some((s) => s && s.size > 0);
  const filtered = useMemo(
    () => (active ? applyFilters(rows, effectiveFilters) : rows),
    [rows, effectiveFilters, active],
  );
  /** null signals "use the precomputed baseline" to every adapter. */
  const subset = active ? filtered : null;

  const segments = verdictSegments(summary);
  const noun = manifest?.intervention_noun ?? "Intervention";

  // Forest plots: one per pooled outcome group. Empty for corpora with no
  // meta-analysis, so the section simply doesn't render.
  const forests = useMemo(
    () =>
      metaMinTrials != null
        ? poolableGroups(metaGroups ?? [], metaMinTrials)
        : poolableGroups(metaGroups ?? []),
    [metaGroups, metaMinTrials],
  );

  // The breakdown and its hover trials MUST come from the same row array, or
  // the tooltip lists studies the filter removed.
  const interventions = interventionBars(summary, subset);
  const interventionHovers = useMemo(
    () => hoverTrialsFrom(filtered, "interventionNames"),
    [filtered],
  );

  function setFacet(facet: FacetKey, next: Set<string>) {
    setFilters((prev) => ({ ...prev, [facet]: next }));
  }

  /** Cross-filtered denominator: rows passing every filter EXCEPT this one, so
   *  "Germany (4)" means "ticking Germany leaves 4", not "Germany appears 4
   *  times somewhere in the corpus". */
  function except(facet: FacetKey): TrialRow[] {
    const others: Filters = { ...effectiveFilters };
    delete others[facet];
    return applyFilters(rows, others);
  }

  return (
    <div>
      {header}
      {notice}
      {summary.scientific_rules_version !== SCIENTIFIC_RULES_VERSION && <p role="status" className="mb-4 rounded border border-amber-500 p-3 text-sm">Historical output: scientific validation and a rebuild are required before using pooled conclusions.</p>}
      {summary.scientific_rules_version && <details className="mb-4 rounded border border-border p-3 text-sm">
        <summary>Scientific validation · {summary.feasibility.withheld_count ?? summary.feasibility.withheld?.length ?? 0} comparisons withheld from pooling</summary>
        <p className="my-2">Rules {summary.scientific_rules_version}. Studies and raw data are retained. Numerical checks do not verify table-column attribution. A missing estimate is not a negative finding.</p>
        {summary.totals.n_reports != null && summary.totals.n_reports > summary.totals.n_trials && <p>{summary.totals.n_reports} reports represent {summary.totals.n_trials} independent studies. Charts count studies; the table retains all reports.</p>}
        {rows.filter(r => r.result_details).map(r => <details className="my-2" key={r.id}><summary>{r.title || r.doi}: comparison results</summary>
          {r.result_details?.selected.map((s, i) => <div className="ml-3 my-2" key={i}>
            <p>{s.intervention} versus {s.comparator} · {s.outcome_name} · {s.timepoint?.label ?? "Assessment unresolved"}</p>
            {s.effect != null && <p>{s.effect_measure}: {s.effect.toPrecision(4)} {s.unit ?? ""}{s.ci_low != null && s.ci_high != null ? ` (95% CI ${s.ci_low.toPrecision(4)} to ${s.ci_high.toPrecision(4)})` : " (uncertainty unresolved)"} · {s.basis ?? "Analysis basis unresolved"}</p>}
            <p>{s.significance === "not_detected" ? "No statistically significant difference" : s.significance === "significant" ? "Statistically significant comparison; clinical interpretation depends on the outcome" : "Insufficient extracted data"}{s.p_value != null ? `; reported p ${s.p_value_operator ?? "(operator unknown)"} ${s.p_value}` : ""}</p>
            {s.issues.length > 0 && <ul className="list-disc pl-4 text-foreground/65">{s.issues.map((issue, i) => <li key={i}>{issue.reason}</li>)}</ul>}
          </div>)}
        </details>)}
        <details><summary>Withheld estimates and source evidence</summary>
          {!withheldComparisons?.length && <p>Showing a compact preview. The complete comparison audit and source evidence are in the review’s meta_analysis.json artifact.</p>}
          {(withheldComparisons ?? summary.feasibility.withheld ?? []).map((s, i) => <div className="my-3 border-t border-border pt-2" key={i}>
            <p>{s.paper_id}: {s.intervention} versus {s.comparator} · {s.outcome_name} · {s.timepoint?.label}</p>
            <ul className="ml-4 list-disc">{s.issues.map((issue, j) => <li key={j}>{issue.reason}{issue.source_location ? ` — ${JSON.stringify(issue.source_location)}` : ""}</li>)}</ul>
            {s.source_location != null && <p>Source: {JSON.stringify(s.source_location)}</p>}
            {s.source_quote != null && <blockquote className="text-foreground/60">{typeof s.source_quote === "string" ? s.source_quote : JSON.stringify(s.source_quote)}</blockquote>}
          </div>)}
        </details>
      </details>}

      {/* No headline stat tiles. Removed deliberately: the dashboard leads with
          the evidence itself, not a row of aggregate counts. A `stat_tiles` key
          left in an older saved layout is inert — nothing renders it. */}
      {has("feasibility") && (
        <Removable k="feasibility" label="Meta-analysis feasibility" edit={edit}>
          <FeasibilityCard summary={summary} />
        </Removable>
      )}

      <div className="mb-2 grid gap-x-4 md:grid-cols-2">
        {FILTERS.filter((f) => has(f.panel)).map(({ facet, chart, title, panel }) => {
          const base = except(facet);
          const counts = countDistinctTrials(base, facet);
          const options = barsFrom(summary, chart, null)
            .map((o) => ({ ...o, count: counts[o.key] ?? 0 }))
            .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
          return (
            <Removable key={facet} k={panel} label={`${title} filter`} edit={edit}>
              <FilterCard
                title={title}
                options={options}
                selected={filters[facet] ?? new Set()}
                onChange={(next) => setFacet(facet, next)}
                filteredCount={representativeStudies(filtered).length}
                totalCount={base.length}
                mapSlot={facet === "countries" ? mapSlot : undefined}
                mapToggleLabel="show map"
              />
            </Removable>
          );
        })}
      </div>

      {active && (
        <p className="mb-4 text-sm">
          Showing <strong className="tabular-nums">{representativeStudies(filtered).length}</strong> of{" "}
          <strong className="tabular-nums">{representativeStudies(rows).length}</strong> studies.{" "}
          <button
            type="button"
            className="text-blue-600 underline hover:text-blue-700"
            onClick={() => setFilters({})}
          >
            Clear all filters
          </button>
        </p>
      )}

      {has("intervention_grid") && interventions.length > 0 && (
        <Removable k="intervention_grid" label={`Trials by ${noun.toLowerCase()}`} edit={edit}>
          <BreakdownChart
            title={`Trials by ${noun.toLowerCase()}`}
            studyCount={representativeStudies(subset ?? rows).length}
            breakdown={breakdownFromBars(interventions)}
            segments={segments}
            hoverTrials={interventionHovers}
            onBarClick={(key) => {
              const cur = filters.interventionNames ?? new Set<string>();
              const next = new Set(cur);
              next.has(key) ? next.delete(key) : next.add(key);
              setFacet("interventionNames", next);
            }}
            selectedKey={[...(filters.interventionNames ?? [])][0]}
            clickHint="Each rectangle is one trial, coloured by result — hover for the study, click a row to filter the dashboard (click again to clear)."
            collapseSingletons
            collapseMaxTrials={1}
          />
        </Removable>
      )}

      {has("year_chart") && (
        <Removable k="year_chart" label="Trials by year" edit={edit}>
          <YearChart
            data={yearBars(summary, subset)}
            nMissing={
              active
                ? filtered.filter((r) => r.year == null).length
                : summary.totals.n_trials - summary.totals.n_with_year
            }
          />
        </Removable>
      )}

      <div className="grid gap-x-4 md:grid-cols-2">
        {has("design_breakdown") && (
          <Removable k="design_breakdown" label="Study designs" edit={edit}>
            <SimpleBar summary={summary} chart="by_design" title="Study designs" subset={subset} />
          </Removable>
        )}
        {has("rob_breakdown") && (
          <Removable k="rob_breakdown" label="Risk of bias" edit={edit}>
            <SimpleBar summary={summary} chart="by_rob" title="Risk of bias" subset={subset} />
          </Removable>
        )}
        {has("country_map") && (
          <Removable k="country_map" label="Trials by country" edit={edit}>
            <SimpleBar summary={summary} chart="by_country" title="Trials by country" subset={subset} />
          </Removable>
        )}
        {has("outcome_domains") && (
          <Removable k="outcome_domains" label="Outcome domains" edit={edit}>
            <SimpleBar
              summary={summary}
              chart="by_symptom_domain"
              title="Outcome domains"
              subset={subset}
            />
          </Removable>
        )}
      </div>

      {has("heatmap") && (
        <Removable k="heatmap" label={`${noun} × outcome landscape`} edit={edit}>
          <LandscapeHeatmap rows={filtered} noun={noun} />
        </Removable>
      )}

      {has("blinding_significance") && (
        <Removable k="blinding_significance" label="Blinding vs significance" edit={edit}>
          <BlindingSignificanceChart summary={summary} subset={subset} />
        </Removable>
      )}

      {has("results_table") && (
        <Removable k="results_table" label="Trials table" edit={edit}>
          <ResultsTable
            rows={filtered}
            summary={summary}
            columns={layout?.columns ?? manifest?.results_columns ?? undefined}
            edit={edit}
          />
        </Removable>
      )}

      {has("forest_plots") && forests.length > 0 && (
        <Removable k="forest_plots" label="Meta-analysis" edit={edit}>
          <section className="mt-8">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-lg font-semibold">
                Meta-analysis
                <span className="ml-2 text-sm font-normal text-foreground/50">
                  {forests.length} pooled outcome{forests.length === 1 ? "" : "s"}
                </span>
              </h2>
              <ProvenanceBadge summary={summary} />
            </div>
            <div className="grid gap-6 lg:grid-cols-2">
              {forests.map((g) => (
                <div
                  key={`${g.ingredient}::${g.outcome_domain}`}
                  className="overflow-x-auto rounded-lg border border-border bg-card p-3"
                >
                  <ForestPlot group={g} />
                </div>
              ))}
            </div>
          </section>
        </Removable>
      )}

      {has("prisma") && prisma && (prisma.main_flow?.length ?? 0) > 0 && (
        <Removable k="prisma" label="Screening flow" edit={edit}>
          <section className="mt-8">
            <h2 className="mb-3 text-lg font-semibold">Screening flow</h2>
            <div className="overflow-x-auto rounded-lg border border-border bg-card p-3">
              <PrismaFlow data={prisma} />
            </div>
          </section>
        </Removable>
      )}

      {/* No PanelSkipList here anymore: the dashboard editor's "Add chart"
          picker is its writable replacement — it lists the same available
          panels WITH their skip reasons, where the user can act on them. */}
      {audit && <ContractReport summary={summary} />}
    </div>
  );
}

/** Edit-mode wrapper: a positioned remove button in the panel's top-right
 *  corner. Renders children bare when the dashboard is read-only. */
function Removable({
  k,
  label,
  edit,
  children,
}: {
  k: PanelKey;
  label: string;
  edit?: DashboardEdit;
  children: ReactNode;
}) {
  if (!edit) return <>{children}</>;
  return (
    <div className="group relative">
      <button
        type="button"
        aria-label={`Remove ${label}`}
        title={`Remove ${label}`}
        onClick={() => edit.onRemovePanel(k)}
        className="absolute right-2 top-2 z-10 rounded-full border border-border bg-card/90 p-1 text-foreground/50 shadow-sm hover:border-red-500/50 hover:text-red-600"
      >
        <X size={13} />
      </button>
      {children}
    </div>
  );
}

/** How traceable the pooled numbers are to their source quotes. Renders only
 *  when the bundle carries a rate — corpora extracted before provenance
 *  tracking (e.g. restless_legs) report `unavailable`, and then it stays hidden
 *  rather than showing a misleading 0%. */
function ProvenanceBadge({ summary }: { summary: DashboardSummary }) {
  const rate = summary.provenance?.traceable_rate;
  if (rate == null) return null;
  const pct = Math.round(rate * 100);
  const tone =
    pct >= 90
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
      : pct >= 70
        ? "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
        : "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-400";
  return (
    <span
      className={`rounded border px-2 py-0.5 text-[11px] font-medium ${tone}`}
      title="Share of extracted values whose supporting quote was found verbatim in the source"
    >
      {pct}% of values traceable to source
    </span>
  );
}

/** A non-stacked BreakdownChart. `BreakdownChart` accepts both shapes, so these
 *  need no per-facet verdict breakdown in `summary.json`. */
function SimpleBar({
  summary,
  chart,
  title,
  subset,
}: {
  summary: DashboardSummary;
  chart: BarChartKey;
  title: string;
  subset: TrialRow[] | null;
}) {
  const bars = barsFrom(summary, chart, subset).filter((b) => b.count > 0);
  if (!bars.length) return null;
  return (
    <BreakdownChart
      title={title}
      studyCount={subset === null ? summary.totals.n_trials : representativeStudies(subset).length}
      breakdown={simpleBreakdown(bars)}
      labels={labelsFor(bars)}
    />
  );
}
