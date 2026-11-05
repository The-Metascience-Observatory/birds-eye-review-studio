"use client";

/** The dashboard stage (slug "view-dashboard", phase label "View dashboard").
 *
 *  The dashboard lives HERE, inline: it auto-builds the first time this page is
 *  opened and auto-REBUILDS when the bundle is stale (see useAutoBuildDashboard),
 *  rendering below the stepper — no Run click, no hop to a separate page.
 *
 *  It is also EDITABLE here: "Edit dashboard" puts a remove button on every
 *  panel, an "Add chart" picker offers the rest, and the table grows an
 *  "Edit columns" checkbox list. Selections persist to review config
 *  (`dashboard.panels` / `dashboard.columns`), apply instantly via the `layout`
 *  override, and the manifest catches up through a seconds-long background
 *  step run (layout-only staleness — no amber banner, the visible dashboard is
 *  already correct).
 *
 *  Two distinct actions with different permissions remain:
 *  - Building the dashboard LOCALLY (this stage's pipeline steps) is open to
 *    every owner — it writes into the review's own workspace, touches nothing
 *    public, and is what the user reviews.
 *  - Publishing to the PUBLIC site is two-party (owner proposes, admin approves)
 *    and OPT-IN: the publish panel stays hidden behind a toggle until the user
 *    asks for it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  ExternalLink,
  Loader2,
  Pencil,
  RotateCw,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  DEFAULT_COLUMN_KEYS,
  SHELL_PANELS,
  adaptForestGroups,
  poolableGroups,
  type DashboardEdit,
  type DashboardLayout,
  type DashboardManifest,
  type PanelKey,
} from "@bird/dashboard-ui";

import { AddChartPicker, type AddChartOption } from "@/components/AddChartPicker";
import { DashboardSkeleton, DashboardView } from "@/components/DashboardView";
import { PublishPanel } from "@/components/PublishPanel";
import { useAutoBuildDashboard } from "@/hooks/useAutoBuildDashboard";
import { useDashboardState, useReview, useReviewConfig } from "@/hooks/useReview";
import {
  getDashboardSummary,
  getJob,
  getMetaAnalysis,
  getPrisma,
  listJobs,
  patchReviewConfig,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtDur } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { ReviewConfigOut, StageOut } from "@/lib/types";

/** The panels a legacy (pre-`layout_version`) manifest implies: everything the
 *  old shell rendered unconditionally, plus whichever gated charts its
 *  panels_rendered carries, plus forest when poolable data exists. Used only
 *  until the first v2 manifest lands (the layout-stale self-heal writes one). */
function legacySeed(
  manifest: DashboardManifest | null,
  hasForests: boolean,
): string[] {
  const rendered = new Set((manifest?.panels_rendered ?? []).map((p) => p.key));
  const panels = [
    "feasibility",
    "filter_design", "filter_rob", "filter_country", "filter_outcome_type",
    "design_breakdown", "rob_breakdown", "outcome_domains",
    "results_table",
  ];
  for (const k of ["intervention_grid", "year_chart", "country_map", "prisma"]) {
    if (rendered.has(k)) panels.push(k);
  }
  if (hasForests) panels.push("forest_plots");
  return panels;
}

export function ExportStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();
  const { isAuthenticated, ready } = useAuth();
  const dash = useDashboardState(reviewId);
  const review = useReview(reviewId);
  const config = useReviewConfig(reviewId);
  const [showPublish, setShowPublish] = useState(false);
  const [editMode, setEditMode] = useState(false);

  const { building, rebuild, anyRunning, layoutOnly } = useAutoBuildDashboard(
    reviewId,
    stage,
    dash,
  );

  const exists = dash.data?.exists === true;
  const enabled = ready && isAuthenticated && !!reviewId;

  // Shared cache keys with DashboardView — no extra fetches, just subscriptions,
  // so the editor can read the manifest and know whether pooling has data.
  const summaryQ = useQuery({
    queryKey: ["dashboard-summary", reviewId],
    queryFn: () => getDashboardSummary(reviewId),
    enabled: enabled && exists,
    retry: false,
  });
  const metaQ = useQuery({
    queryKey: ["meta-analysis", reviewId],
    queryFn: () => getMetaAnalysis(reviewId),
    enabled: enabled && summaryQ.isSuccess,
    retry: false,
  });
  // Shares DashboardView's key. Older bundles lack prisma.json (so the manifest
  // marks the panel skipped), but the studio renders the funnel from this LIVE
  // endpoint — the picker must not refuse a panel that would render fine.
  const prismaQ = useQuery({
    queryKey: ["prisma", reviewId],
    queryFn: () => getPrisma(reviewId),
    enabled: enabled && summaryQ.isSuccess,
    retry: false,
  });
  const hasPrismaData = (prismaQ.data?.main_flow?.length ?? 0) > 0;
  const manifest = summaryQ.data?.manifest ?? null;
  const hasForests = useMemo(
    () => poolableGroups(adaptForestGroups(metaQ.data?.groups ?? [])).length > 0,
    [metaQ.data],
  );

  // The effective layout: live config wins (instant toggles), then the v2
  // manifest's pre-resolve request list, then the legacy reconstruction.
  const dashCfg = (config.data?.config_json?.dashboard ?? {}) as {
    panels?: string[];
    columns?: string[];
  };
  const layout: DashboardLayout = useMemo(
    () => ({
      panels:
        dashCfg.panels ??
        ((manifest?.layout_version ?? 1) >= 2 && manifest?.panels_requested
          ? manifest.panels_requested
          : legacySeed(manifest, hasForests)),
      columns:
        dashCfg.columns ?? manifest?.results_columns ?? DEFAULT_COLUMN_KEYS,
    }),
    [dashCfg.panels, dashCfg.columns, manifest, hasForests],
  );

  // Persist layout edits. Optimistic on the review-config cache so a toggle
  // repaints the dashboard the same frame; the PATCH lands behind it, and the
  // layout-stale detection then fires the cheap manifest reconcile.
  const saveLayout = useMutation({
    mutationFn: (partial: { panels?: string[]; columns?: string[] }) =>
      patchReviewConfig(reviewId, { dashboard: partial }),
    onMutate: async (partial) => {
      await qc.cancelQueries({ queryKey: ["review-config", reviewId] });
      const prev = qc.getQueryData<ReviewConfigOut>(["review-config", reviewId]);
      qc.setQueryData<ReviewConfigOut>(["review-config", reviewId], (old) =>
        old
          ? {
              ...old,
              config_json: {
                ...old.config_json,
                dashboard: {
                  ...((old.config_json.dashboard as object | undefined) ?? {}),
                  ...partial,
                },
              },
            }
          : old,
      );
      return { prev };
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["review-config", reviewId], ctx.prev);
      toast.error((e as Error)?.message ?? "Could not save the layout");
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      // Pick up the layout-staleness promptly so the manifest reconcile fires.
      qc.invalidateQueries({ queryKey: ["dashboard-state", reviewId] });
    },
  });

  const edit: DashboardEdit | undefined = editMode
    ? {
        onRemovePanel: (k: PanelKey) =>
          saveLayout.mutate({ panels: layout.panels.filter((p) => p !== k) }),
        onToggleColumn: (k: string) =>
          saveLayout.mutate({
            columns: layout.columns.includes(k)
              ? layout.columns.filter((c) => c !== k)
              : [...layout.columns, k],
          }),
      }
    : undefined;

  // "Add chart" options: every shell panel not currently on, disabled with the
  // honest reason when it can't render. The manifest's skip reasons cover
  // missing data files; forest is additionally gated on measured poolability.
  const addOptions: AddChartOption[] = useMemo(() => {
    const skipped = new Map(
      (manifest?.panels_skipped ?? []).map((s) => [s.key, s.reason]),
    );
    return SHELL_PANELS.filter((p) => !layout.panels.includes(p.key)).map(
      (p) => {
        let disabledReason: string | undefined;
        if (p.key === "forest_plots" && !hasForests) {
          disabledReason =
            summaryQ.data?.summary.feasibility?.recommendation ??
            "No poolable meta-analysis for this topic.";
        } else if (p.key === "prisma" && hasPrismaData) {
          // The bundle may predate prisma.json, but the live funnel renders.
        } else if (skipped.has(p.key)) {
          disabledReason = skipped.get(p.key);
        }
        return { key: p.key, label: p.label, why: p.why, disabledReason };
      },
    );
  }, [manifest, layout.panels, hasForests, summaryQ.data]);

  // When the build finishes (building flips true→false), refresh everything the
  // dashboard reads. useJob's finish-invalidation can miss this job (it stays
  // attached to the first job it saw), so this transition is the reliable link
  // between "job done" and "dashboard appears".
  const wasBuilding = useRef(false);
  useEffect(() => {
    if (wasBuilding.current && !building) {
      for (const key of [
        "dashboard-state",
        "dashboard-summary",
        "dashboard-rows",
        "meta-analysis",
        "prisma",
      ]) {
        qc.invalidateQueries({ queryKey: [key, reviewId] });
      }
    }
    wasBuilding.current = building;
  }, [building, reviewId, qc]);

  const failed =
    !exists && !building && stage.activity?.last_job_state === "failed";
  const blocked = stage.gate?.state === "blocked";
  // A layout-only reconcile is invisible by design (the visible dashboard is
  // already correct via the layout override) — no amber banner for it.
  const dataStale = dash.data?.stale && dash.data?.stale_scope !== "layout";
  const reconciling =
    (dash.data?.stale && dash.data?.stale_scope === "layout") ||
    (layoutOnly && building);

  return (
    <div className="space-y-4">
      {blocked && !exists ? (
        <p className="text-sm text-muted-foreground">
          The dashboard builds automatically once the earlier stages are
          approved.
        </p>
      ) : dash.isLoading ? (
        <DashboardSkeleton />
      ) : building && !exists ? (
        // First build: nothing to show yet. A stale REBUILD falls through to the
        // dashboard branch below, which keeps the old dashboard visible with a
        // "rebuilding" notice instead of blanking to a skeleton.
        <BuildingPanel reviewId={reviewId} />
      ) : failed ? (
        <p className="flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span>
            The dashboard build failed. Use <strong>Run stage</strong> below to
            try again (turn on debug mode to see the job log).
          </span>
        </p>
      ) : !exists ? (
        // Auto-fire is about to kick in (or was declined this mount) — transient.
        <p className="text-sm text-muted-foreground">
          Starting the dashboard build…
        </p>
      ) : (
        <>
          {dataStale &&
            (stage.activity?.last_job_state === "failed" && !building ? (
              // Auto-rebuild is suppressed after a failed build (it would loop);
              // the deliberate retry stays a visible button.
              <button
                type="button"
                onClick={rebuild}
                disabled={anyRunning}
                className="inline-flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-sm font-medium text-amber-700 hover:bg-amber-500/15 disabled:opacity-50 dark:text-amber-400"
              >
                <RotateCw size={14} />
                Rebuild dashboard
              </button>
            ) : (
              <p className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-sm text-amber-700 dark:text-amber-400">
                {building || anyRunning ? (
                  <Loader2 size={14} className="shrink-0 animate-spin" />
                ) : (
                  <RotateCw size={14} className="shrink-0" />
                )}
                <span>
                  This dashboard is out of date — rebuilding it automatically…
                </span>
              </p>
            ))}

          {/* The editor toolbar. Toggling edit mode never blocks the dashboard —
              edits apply instantly through the layout override. */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setEditMode((v) => !v)}
              className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm font-medium ${
                editMode
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border hover:bg-accent"
              }`}
            >
              {editMode ? <Check size={14} /> : <Pencil size={14} />}
              {editMode ? "Done editing" : "Edit dashboard"}
            </button>
            {editMode && <AddChartPicker options={addOptions} onAdd={(k) => saveLayout.mutate({ panels: [...layout.panels, k] })} />}
            {saveLayout.isPending ? (
              <span className="text-xs text-muted-foreground">Saving…</span>
            ) : reconciling ? (
              <span className="text-xs text-muted-foreground">
                Saved — updating the build manifest in the background…
              </span>
            ) : null}
            {editMode && (
              <span className="text-xs text-muted-foreground">
                Remove a panel with its ✕; column checkboxes live under the
                table&apos;s “Edit columns”.
              </span>
            )}
          </div>

          <DashboardView
            hideStaleBanner
            reviewId={reviewId}
            layout={layout}
            edit={edit}
            header={({ summary, manifest, state }) => (
              <header className="mb-6 flex flex-wrap items-baseline justify-between gap-3">
                <h2 className="text-xl font-bold">
                  {manifest?.title ?? summary.slug}
                </h2>
                <p className="flex items-center gap-3 text-sm text-muted-foreground">
                  <span>
                    Built{" "}
                    {state.built_at
                      ? new Date(state.built_at).toLocaleString()
                      : "—"}{" "}
                    · not published
                  </span>
                  <Link
                    href={`/reviews/${reviewId}/dashboard`}
                    className="inline-flex items-center gap-1 hover:text-foreground"
                  >
                    <ExternalLink size={13} />
                    Open full width
                  </Link>
                </p>
              </header>
            )}
          />
        </>
      )}

      {/* Publishing to the public site is opt-in — hidden behind a toggle so the
          stage stays about viewing the dashboard until the user asks to publish. */}
      <label className="flex cursor-pointer items-center gap-2 text-sm">
        <input
          type="checkbox"
          className="h-4 w-4 accent-primary"
          checked={showPublish}
          onChange={(e) => setShowPublish(e.target.checked)}
        />
        <span className="font-medium">Publish to the public site</span>
        <span className="text-muted-foreground">
          — optional; an administrator approves before anything goes live.
        </span>
      </label>

      {showPublish && (
        <PublishPanel reviewId={reviewId} slug={review.data?.slug} built={exists} />
      )}
    </div>
  );
}

/** Live progress while the export job runs. The EvidenceDock (which used to
 *  surface pipeline progress) is suppressed on this page, so the stage shows
 *  its own bar. listJobs only has the between-steps DB row; GET /jobs/{id}
 *  overlays the worker's live sidecar (done/total/ETA). */
function BuildingPanel({ reviewId }: { reviewId: string }) {
  const jobs = useQuery({
    queryKey: ["review-jobs", reviewId],
    queryFn: () => listJobs(reviewId),
    // Stop polling once no job is active: if the build succeeds while the
    // parent's `exists` check lags, this panel would otherwise show
    // "starting…" and poll forever.
    refetchInterval: (q) =>
      q.state.data?.some(
        (j) => j.state === "running" || j.state === "queued",
      ) === false
        ? false
        : 3000,
  });
  const job = jobs.data?.find(
    (j) =>
      (j.kind === "pipeline" || j.kind === "step") &&
      (j.state === "running" || j.state === "queued"),
  );
  const detail = useQuery({
    queryKey: ["job", job?.id],
    queryFn: () => getJob(job!.id),
    enabled: !!job,
    refetchInterval: 2000,
  });
  const prog = detail.data?.progress;
  const live = prog?.live ?? null;

  return (
    <div>
      <div className="rounded-lg border border-primary/40 bg-primary/5 p-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
          <Loader2 size={14} className="shrink-0 animate-spin text-primary" />
          <span className="font-medium">
            {prog?.step_label ?? "Building your dashboard"}
          </span>
          {live?.total ? (
            <span className="tabular-nums text-muted-foreground">
              {(live.done ?? 0).toLocaleString()} /{" "}
              {live.total.toLocaleString()}
              {live.eta_s != null && ` · ~${fmtDur(live.eta_s)} left`}
            </span>
          ) : (
            <span className="text-muted-foreground">starting…</span>
          )}
        </div>
        {live?.total ? (
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-primary/15">
            <div
              className="h-full rounded-full bg-primary transition-all"
              style={{
                width: `${Math.round((100 * (live.done ?? 0)) / live.total)}%`,
              }}
            />
          </div>
        ) : null}
        <p className="mt-1.5 text-xs text-muted-foreground">
          The dashboard will appear here when the build finishes.
        </p>
      </div>
      {/* Reserve the dashboard's shape below the bar so the page doesn't jump
          when the build completes. */}
      <div className="mt-4">
        <DashboardSkeleton />
      </div>
    </div>
  );
}
