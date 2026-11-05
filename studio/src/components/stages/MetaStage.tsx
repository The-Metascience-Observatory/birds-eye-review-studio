"use client";

/** The meta-analysis stage body.
 *
 *  Meta-analysis is ALWAYS OPTIONAL. This stage's job is to explain, in plain
 *  language, whether pooling is feasible for this corpus — not to gate anything.
 *  It fetches the live feasibility verdict (`assess_poolability` over the pass-2
 *  records) and renders it through the SAME `FeasibilityCard` the published
 *  dashboard uses, so the wizard's assessment and the final dashboard can never
 *  disagree.
 *
 *  When the verdict says pooling IS feasible, the card used to be a dead end: it
 *  recommended a meta-analysis the UI had no way to run. Pooling is gated on the
 *  `generate_meta_analysis` config flag, which was settable only at review
 *  creation — backwards, since you only learn pooling is feasible after
 *  extraction. `PoolAction` below closes that loop: it sets the flag and re-runs
 *  the bundle build, which emits `meta_analysis.json` and makes DashboardShell
 *  render the forest plot.
 *
 *  Below that, the existing config editor (via GenericStage) exposes the
 *  `meta_analysis` settings for users who want to tune the pooling itself.
 */

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart3, Loader2 } from "lucide-react";
import { FeasibilityCard } from "@bird/dashboard-ui";
import type { Feasibility } from "@bird/dashboard-ui";

import { PrismaCard } from "@/components/PrismaCard";
import { GenericStage } from "@/components/stages/GenericStage";
import { Banner } from "@/components/ui/Banner";
import { SkeletonCard } from "@/components/ui/Skeleton";
import { toast } from "@/lib/toast";
import { getFeasibility, patchReviewConfig, runStage } from "@/lib/api";
import { useReviewConfig } from "@/hooks/useReview";
import type { StageOut } from "@/lib/types";

export function MetaStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const running = !!stage.activity?.running_job_id;

  const feasibility = useQuery({
    queryKey: ["feasibility", reviewId],
    queryFn: () => getFeasibility(reviewId),
    // Follow a running job so the verdict updates as extraction finishes.
    refetchInterval: running ? 3000 : false,
    retry: false,
  });

  const f = feasibility.data;

  return (
    <div className="space-y-4">
      <Banner tone="info">
        Meta-analysis is optional. This card explains whether the trials can be
        pooled — it never blocks the review. If pooling is not feasible, that is
        itself an honest finding worth publishing.
      </Banner>

      {feasibility.isLoading ? (
        <SkeletonCard />
      ) : feasibility.isError ? (
        <Banner tone="error">
          Feasibility could not be assessed yet. Run the deep-extraction stage
          first, then come back here.
        </Banner>
      ) : f ? (
        <>
          <FeasibilityCard feasibility={f} />
          <PoolAction reviewId={reviewId} feasibility={f} />
        </>
      ) : null}

      <PrismaCard reviewId={reviewId} running={running} title="Evidence funnel" />

      <div>
        <h3 className="mb-2 text-sm font-semibold">Pooling settings</h3>
        <GenericStage reviewId={reviewId} stage={stage} />
      </div>
    </div>
  );
}

/** Turn the feasibility verdict into something the user can ACT on.
 *
 *  Pooling is deterministic (`export_dashboard.py --meta-analysis` runs
 *  `grading`'s audited timepoint/basis rules — no LLM, no cost), so this is a
 *  plain "do it" button rather than a spend confirmation.
 *
 *  Only domains whose verdict is "poolable" actually produce a forest plot, so
 *  the button names THOSE — never the corpus-wide "N of M outcomes are poolable"
 *  figure, which would promise far more rows than appear.
 */
function PoolAction({
  reviewId,
  feasibility,
}: {
  reviewId: string;
  feasibility: Feasibility;
}) {
  const qc = useQueryClient();
  const config = useReviewConfig(reviewId);
  const cfg = config.data?.config_json as Record<string, unknown> | undefined;
  const enabled = Boolean(cfg?.generate_meta_analysis);
  const dashboardCfg = cfg?.dashboard as Record<string, unknown> | undefined;
  const panelList = dashboardCfg?.panels as string[] | undefined;
  // Pooling on, but the panel that draws it is missing from an explicit panel
  // list: the data exists and the dashboard silently won't show it.
  const panelMissing = Boolean(
    enabled && panelList && !panelList.includes("forest_plots"),
  );

  const poolable = (feasibility.domains ?? []).filter(
    (d) => d.verdict === "poolable",
  );

  const pool = useMutation({
    mutationFn: async () => {
      // TWO separate switches, and enabling only the first is why pooling ran
      // but nothing appeared:
      //   1. `generate_meta_analysis` makes the build PRODUCE meta_analysis.json
      //   2. the `forest_plots` panel makes the dashboard DISPLAY it
      // A review whose dashboard.panels list predates pooling has no
      // forest_plots entry, so DashboardShell's `has("forest_plots")` gate stays
      // false and the section never renders however good the data is.
      //
      // Config first either way: `canonical_steps_for` reads the flag when it
      // BUILDS the step list, so it must be persisted before the run is queued
      // or the export step is constructed without --meta-analysis.
      const panels = (dashboardCfg?.panels as string[] | undefined) ?? null;
      await patchReviewConfig(reviewId, {
        generate_meta_analysis: true,
        // Only touch the panel list when the review has an explicit one that
        // is missing forest_plots. A review with no list gets DEFAULT_PANELS,
        // and rewriting it here would freeze today's defaults into its config.
        ...(panels && !panels.includes("forest_plots")
          ? { dashboard: { panels: [...panels, "forest_plots"] } }
          : {}),
      });
      return runStage(reviewId, "view-dashboard");
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
      toast.success("Pooling — the forest plot appears on the dashboard when the build finishes");
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not start pooling"),
  });

  // Nothing clears the trial threshold — the honest case. The card above already
  // explains why; adding a disabled button would only imply the tool is stuck.
  if (poolable.length === 0) return null;

  const names = poolable
    .map((d) => {
      const label = String(d.domain).replace(/_/g, " ");
      const n = typeof d.n_trials === "number" ? d.n_trials : null;
      return n ? `${label} (${n} trial${n === 1 ? "" : "s"})` : label;
    })
    .join(", ");

  if (enabled) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <p className="text-sm">
          Pooling is on for <span className="font-medium">{names}</span>.
        </p>
        {panelMissing ? (
          // Pooled data exists but this review's saved panel list has no
          // forest_plots entry, so the dashboard renders everything EXCEPT the
          // plot. Say so and offer the one-click fix rather than linking to a
          // dashboard where the user will hunt for something that cannot appear.
          <>
            <p className="mt-1 max-w-prose text-sm text-muted-foreground">
              The plot is built but hidden: this dashboard&apos;s panel list
              doesn&apos;t include the meta-analysis section yet.
            </p>
            <button
              type="button"
              disabled={pool.isPending}
              onClick={() => pool.mutate()}
              className="mt-3 inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {pool.isPending ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <BarChart3 size={14} />
              )}
              {pool.isPending ? "Adding…" : "Show it on the dashboard"}
            </button>
          </>
        ) : (
          <>
            <Link
              href={`/reviews/${reviewId}/dashboard`}
              className="mt-1 inline-block text-sm text-blue-600 underline underline-offset-2"
            >
              View the forest plot on the dashboard →
            </Link>
            <p className="mt-2 text-xs text-muted-foreground">
              Re-run the dashboard build after any extraction change to refresh
              it.
            </p>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-semibold">Pool the comparable trials</h3>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">
        Builds forest plots for <span className="font-medium">{names}</span> —
        the only {poolable.length === 1 ? "domain" : "domains"} with enough
        trials measuring the same thing. Runs the audited deterministic pooling
        (random effects); no model calls, no cost.
      </p>
      {/* The domain trial-count above is what `assess_poolability` sees. The
          export then splits each domain further by intervention arm AND effect
          measure, so a 4-trial domain can become two 2-trial plots. Say so,
          rather than promising a row count the plot won't have. */}
      <p className="mt-1 max-w-prose text-xs text-muted-foreground">
        Trials are grouped by intervention and effect measure, so a domain may
        produce more than one plot with fewer trials in each.
      </p>
      <button
        type="button"
        disabled={pool.isPending}
        onClick={() => pool.mutate()}
        className="mt-3 inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
      >
        {pool.isPending ? (
          <Loader2 size={14} className="animate-spin" />
        ) : (
          <BarChart3 size={14} />
        )}
        {pool.isPending ? "Starting…" : "Pool these trials"}
      </button>
    </div>
  );
}
