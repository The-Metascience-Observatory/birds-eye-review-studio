/** @bird/dashboard-ui — shared, prop-driven Bird's Eye Review dashboard
 *  components. Consumed by the studio and by newly-generated dashboards.
 *
 *  Historical public-website dashboards keep their own bespoke component copies
 *  and do NOT import this package — so their per-dashboard tweaks stay free. */

// Pure helpers
export { fmt, formatLabel } from "./utils";
export {
  prettyMeasure,
  prettyDomain,
  type DomainLabels,
} from "./labels";
export { trialHref } from "./links";

// Components
export {
  ForestPlot,
  type ForestGroup,
  type ForestTrial,
  type ForestPooled,
  type ForestSubgroup,
  type ForestExcluded,
} from "./ForestPlot";
export { YearChart } from "./YearChart";

// Precomputed-summary contract (mirrors dashboard_summary.py)
export {
  facetValues,
  countDistinctTrials,
  applyFilters,
  type Verdict,
  type RowFacets,
  type FacetKey,
  type TrialRow,
  type InterventionBar,
  type DashboardSummary,
  type DashboardManifest,
  type Feasibility,
} from "./summary";

// Adapters: the ONLY place that chooses between a precomputed baseline and a
// filtered reduce. See adapters.ts for the invariant.
export {
  barsFrom,
  breakdownFromBars,
  hoverTrialsFrom,
  interventionBars,
  labelsFor,
  leadAuthor,
  simpleBreakdown,
  verdictSegments,
  yearBars,
  type BarChartKey,
} from "./adapters";

// Chart + layout components, shared by the studio preview and generated
// website dashboards.
export { BreakdownChart } from "./BreakdownChart";
export { BlindingSignificanceChart } from "./BlindingSignificanceChart";
export { LandscapeHeatmap } from "./LandscapeHeatmap";
export { TrialRectList, type TrialRectItem } from "./TrialRectList";
export { ChartWatermark } from "./ChartWatermark";
export { useIsMobile } from "./useIsMobile";
export {
  HoverPanel,
  useHoverPanel,
  rectAnchor,
  trialLink,
  type HoverTrial,
  type Segment,
  type Anchor,
} from "./hover/trialHoverPanel";
export { adaptForestGroups, poolableGroups } from "./forestAdapter";
export {
  PrismaFlow,
  type PrismaData,
  type PrismaStage,
  type PrismaExclusion,
  type PrismaSource,
} from "./PrismaFlow";
export {
  ResultsTable,
  RESULT_COLUMNS,
  DEFAULT_COLUMN_KEYS,
  type ColumnDef,
} from "./ResultsTable";
export { DashboardShell } from "./DashboardShell";
export { FilterCard } from "./FilterCard";

// The editable-layout contract (studio dashboard editor).
export {
  panelGate,
  SHELL_PANELS,
  type PanelKey,
  type DashboardLayout,
  type DashboardEdit,
} from "./layout";

// The honesty panels (studio-facing; no website counterpart).
export {
  FeasibilityCard,
  PanelSkipList,
  ContractReport,
} from "./panels";

export { VERDICT_LABEL } from "./labels";
