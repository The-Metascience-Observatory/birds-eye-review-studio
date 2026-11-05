/** Adapt `meta_analysis.json`'s `groups` into the ForestPlot's `ForestGroup[]`.
 *
 *  The pipeline's group shape already matches `ForestGroup` field-for-field (the
 *  public website casts it straight through) — the ONLY gaps are per-trial: the
 *  pipeline omits the display `label` and the outbound `doi`, which are derived
 *  from `first_author` + `year` + `paper_id`. This is the studio's equivalent of
 *  the website's `loadForestGroups`, kept in the package so both consumers agree.
 */

import { SCIENTIFIC_RULES_VERSION } from "./summary";
import type { ForestGroup, ForestTrial } from "./ForestPlot";

/** "Author 2021", falling back to the bare DOI when the author is unknown. */
function trialLabel(t: {
  paper_id?: string;
  first_author?: string;
  year?: number | string | null;
}): string {
  const author = (t.first_author ?? "").trim();
  const year = t.year != null && t.year !== "" ? ` ${t.year}` : "";
  if (author) return `${author}${year}`;
  const base = (t.paper_id ?? "").split("#")[0];
  return base || "unknown";
}

/** One row per group, trials enriched with `label` + base `doi`. */
export function adaptForestGroups(rawGroups: unknown[]): ForestGroup[] {
  if (!Array.isArray(rawGroups)) return [];
  return rawGroups.map((raw) => {
    const g = raw as ForestGroup & { trials?: unknown[] };
    const trials = Array.isArray(g.trials) ? g.trials : [];
    return {
      ...g,
      trials: trials.map((rt) => {
        const t = rt as ForestTrial;
        const base = (t.paper_id ?? "").split("#")[0];
        return { ...t, doi: t.doi ?? base, label: t.label ?? trialLabel(t) };
      }),
    };
  });
}

/** The groups worth drawing a forest plot for: an actual pooled estimate over
 *  at least `minTrials` studies, and not the catch-all "other"/"mixed" bucket
 *  (a pooled bar over unlike drugs is meaningless). Sorted by evidence weight. */
export function poolableGroups(
  groups: ForestGroup[],
  minTrials = 2,
): ForestGroup[] {
  return groups
    .filter(
      (g) =>
        g.rules_version === SCIENTIFIC_RULES_VERSION &&
        !!g.pooled &&
        g.n_trials >= minTrials &&
        !/^(other|mixed|misc)/i.test(g.ingredient ?? ""),
    )
    .sort((a, b) => b.n_trials - a.n_trials);
}
