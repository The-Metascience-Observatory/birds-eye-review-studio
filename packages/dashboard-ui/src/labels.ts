/** Display-label helpers for effect measures and outcome domains.
 *
 *  On the public website these lived inside each topic's `ForestPlot.tsx`, with a
 *  hard-coded per-topic `DOMAIN_LABELS`. Here the effect-measure map is shared
 *  (it is disease-agnostic) and the domain labels are passed in per dashboard, so
 *  one component serves every topic without forking. */

/** Effect-measure key -> display label. Disease-agnostic. Internal to
 *  `prettyMeasure`; not part of the public API. */
const MEASURE_LABEL: Record<string, string> = {
  risk_ratio: "Risk ratio",
  odds_ratio: "Odds ratio",
  hazard_ratio: "Hazard ratio",
  rate_ratio: "Rate ratio",
  incidence_rate_ratio: "Incidence rate ratio",
  adjusted_odds_ratio: "Adjusted odds ratio",
  adjusted_risk_ratio: "Adjusted risk ratio",
  adjusted_hazard_ratio: "Adjusted hazard ratio",
  adjusted_rate_ratio: "Adjusted rate ratio",
  mean_difference: "Mean difference",
  adjusted_mean_difference: "Adjusted mean difference",
  smd: "Std. mean difference",
  standardized_mean_difference: "Std. mean difference",
  risk_difference: "Risk difference",
  rate_difference: "Rate difference",
};

/** Result-verdict key -> display label. Shared by the results table and the
 *  chart adapters, so the wording can't drift between the two. */
export const VERDICT_LABEL: Record<string, string> = {
  favors_treatment: "Favors treatment",
  favors_control: "Favors control",
  no_difference: "No statistically significant difference",
  mixed: "Mixed",
  insufficient_data: "Insufficient extracted data",
  inconclusive: "Inconclusive",
  unknown: "Unknown",
};

/** Per-dashboard outcome-domain key -> display label. Each review supplies its
 *  own map (e.g. `{ fatigue: "Fatigue severity", ... }`); keys not present fall
 *  back to a humanized version of the key. */
export type DomainLabels = Record<string, string>;

export function prettyMeasure(m: string): string {
  return MEASURE_LABEL[m] ?? m.replace(/_/g, " ");
}

/** Humanize an outcome-domain key using the dashboard's `domainLabels` map,
 *  falling back to Title Case of the key. */
export function prettyDomain(d: string, domainLabels: DomainLabels = {}): string {
  return (
    domainLabels[d] ??
    d.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}
