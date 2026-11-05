"use client";

/** The review's dashboard, as a host-agnostic component.
 *
 *  Renders the bundle the pipeline built into this review's workspace using
 *  @bird/dashboard-ui — the same components a generated website dashboard uses,
 *  from the same precomputed summary.json. Two hosts share it so they cannot
 *  drift: the export stage page (inline, beside the agent rail) and the
 *  full-width /reviews/[id]/dashboard route.
 *
 *  This component owns what is studio-specific — auth, fetching, and the
 *  loading/error/stale states — and renders CONTENT ONLY (no <main> wrapper);
 *  each host supplies its own page chrome via the `header` render-prop.
 */

import { useQuery } from "@tanstack/react-query";
import { DashboardShell, adaptForestGroups } from "@bird/dashboard-ui";
import type { DashboardEdit, DashboardLayout } from "@bird/dashboard-ui";
import { AlertTriangle } from "lucide-react";
import type { ReactNode } from "react";

import {
  getDashboardRows,
  getDashboardSummary,
  getMetaAnalysis,
  getPrisma,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** What the summary endpoint returns — the header render-prop receives it so
 *  hosts can show the title / built-at line without their own fetch. */
export type DashboardSummaryPayload = Awaited<
  ReturnType<typeof getDashboardSummary>
>;

export function DashboardView({
  reviewId,
  header,
  hideStaleBanner = false,
  layout,
  edit,
}: {
  reviewId: string;
  /** Page chrome (title, back-link, built-at line), built from the loaded
   *  summary. Rendered inside DashboardShell's header slot. */
  header?: (ctx: DashboardSummaryPayload) => ReactNode;
  /** The export stage shows its own "rebuilding automatically" notice (the
   *  bundle rebuilds itself there), so it suppresses this banner. The
   *  full-width /dashboard route keeps it — nothing auto-rebuilds there. */
  hideStaleBanner?: boolean;
  /** Live layout override (the export stage's editor) — panel/column toggles
   *  apply instantly, without waiting for the manifest rebuild. */
  layout?: DashboardLayout;
  /** Edit-mode callbacks; when set every panel gets a remove button. */
  edit?: DashboardEdit;
}) {
  const { isAuthenticated, ready } = useAuth();
  const enabled = ready && isAuthenticated && !!reviewId;

  const summaryQ = useQuery({
    queryKey: ["dashboard-summary", reviewId],
    queryFn: () => getDashboardSummary(reviewId),
    enabled,
    retry: false,
  });
  const rowsQ = useQuery({
    queryKey: ["dashboard-rows", reviewId],
    queryFn: () => getDashboardRows(reviewId),
    enabled: enabled && summaryQ.isSuccess,
    retry: false,
  });
  // Forest plots — only corpora with a pooled meta-analysis return groups.
  const metaQ = useQuery({
    queryKey: ["meta-analysis", reviewId],
    queryFn: () => getMetaAnalysis(reviewId),
    enabled: enabled && summaryQ.isSuccess,
    retry: false,
  });
  // The screening funnel, for the "Screening flow" panel. Same computation the
  // bundled prisma.json carries, served live so the preview never lags.
  const prismaQ = useQuery({
    queryKey: ["prisma", reviewId],
    queryFn: () => getPrisma(reviewId),
    enabled: enabled && summaryQ.isSuccess,
    retry: false,
  });

  // A DISABLED query (auth not resolved yet) is neither loading nor errored — it
  // sits idle with `data` undefined. Handle that BEFORE the error branch so an
  // unauthenticated visitor gets a sign-in prompt rather than "No dashboard yet".
  if (ready && !isAuthenticated) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="text-lg font-semibold">Sign in to view this dashboard</h2>
        <p className="mt-1 max-w-prose text-sm text-muted-foreground">
          This review&apos;s dashboard is private to its owner.
        </p>
      </div>
    );
  }

  if (!ready || summaryQ.isLoading) return <DashboardSkeleton />;

  if (summaryQ.isError) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="text-lg font-semibold">No dashboard yet</h2>
        <p className="mt-1 max-w-prose text-sm text-muted-foreground">
          {(summaryQ.error as Error)?.message ??
            "This review has no built dashboard."}
        </p>
      </div>
    );
  }

  // No `!` here: every branch above returns, but a query can still settle to
  // undefined (disabled mid-render, or a cache eviction), and the non-null
  // assertion turned that into a hard crash instead of one more Skeleton frame.
  if (!summaryQ.data) return <DashboardSkeleton />;

  const { summary, manifest, state } = summaryQ.data;
  const rows = rowsQ.data?.rows ?? [];

  return (
    <DashboardShell
      summary={summary}
      manifest={manifest}
      rows={rows}
      metaGroups={adaptForestGroups(metaQ.data?.groups ?? [])}
      withheldComparisons={metaQ.data?.withheld as unknown as import("@bird/dashboard-ui").Feasibility["withheld"]}
      // Filter at the threshold the POOLING used, not the component default:
      // the pipeline pools at 2 and the default filtered at 3, so real pooled
      // groups were silently dropped and the forest section never rendered.
      metaMinTrials={metaQ.data?.min_trials ?? undefined}
      layout={layout}
      edit={edit}
      prisma={prismaQ.data ?? null}
      header={header?.(summaryQ.data)}
      notice={
        <>
          {state.stale && !hideStaleBanner && (
            <p className="mb-4 flex items-start gap-2 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" />
              <span>
                This dashboard is out of date: {state.reason}. Re-run the build
                step to refresh it.
              </span>
            </p>
          )}
          {summary.schema_version < 2 && (
            <p className="mb-4 rounded border border-border bg-muted px-3 py-2 text-sm text-muted-foreground">
              This bundle predates author citations. Re-run the build step to
              add them to the table and chart tooltips.
            </p>
          )}
          {rowsQ.isLoading && (
            <p className="mb-4 text-sm text-muted-foreground">
              Loading trials…
            </p>
          )}
        </>
      }
    />
  );
}

/** Matches the final layout so the page does not reflow on load. */
export function DashboardSkeleton() {
  return (
    <div className="animate-pulse">
      <div className="mb-6 h-8 w-96 max-w-full rounded bg-muted" />
      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-16 rounded-lg border border-border bg-muted" />
        ))}
      </div>
      <div className="mb-6 h-32 rounded-lg border border-border bg-muted" />
      <div className="h-64 rounded-lg border border-border bg-muted" />
    </div>
  );
}
