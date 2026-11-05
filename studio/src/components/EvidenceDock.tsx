"use client";

/** The evidence table docked beneath the wizard, on every review page.
 *
 *  Lives in the review layout so it persists across all stage routes — the
 *  papertrace shape of "wizard on top, evidence table below, one screen". It
 *  polls while any job runs so rows and columns fill in live, and stays hidden
 *  until the search has produced at least one paper.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { FileWarning, Hourglass, Loader2, Square } from "lucide-react";
import { useParams, usePathname } from "next/navigation";
import { toast } from "@/lib/toast";

import { EvidenceTable } from "@/components/EvidenceTable";
import {
  cancelJob,
  excludePapers,
  getEvidence,
  getJob,
  getStages,
  includePapers,
  listJobs,
} from "@/lib/api";
import type { EvidenceOut, EvidenceRow } from "@/lib/api";
import { fmtDur } from "@/lib/format";
import { useAuth } from "@/lib/auth";

export function EvidenceDock() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const pathname = usePathname();
  const { isAuthenticated, ready } = useAuth();
  // Pages that already show their own study table (or are a focused report) don't
  // want the full evidence table docked beneath them: the missing-PDFs report is
  // its own worklist, and the dashboard — whether on its own route or inline on
  // the dashboard stage — has its own ResultsTable inside DashboardShell. The
  // dock stays off on all three (the dashboard stage also shows its own build
  // progress). The stage slug is "view-dashboard" (services/stages.py).
  const onMissingPdfs = !!pathname?.endsWith("/missing-pdfs");
  const onDashboard = !!pathname?.endsWith("/dashboard");
  const onExportStage = !!pathname?.endsWith("/stages/view-dashboard");
  const enabled =
    ready &&
    isAuthenticated &&
    !!id &&
    !onMissingPdfs &&
    !onDashboard &&
    !onExportStage;
  const qc = useQueryClient();

  // The poll gate: is any stage running? Reuse the same signal the stage pages
  // use, so the table refreshes exactly while work is producing rows.
  const stages = useQuery({
    queryKey: ["stages", id],
    queryFn: () => getStages(id!),
    enabled,
    refetchInterval: 3000,
  });
  // Watch jobs directly, so the PARALLEL metadata job (which isn't a stage
  // activity) also keeps the table polling as it fills the DB.
  const jobs = useQuery({
    queryKey: ["review-jobs", id],
    queryFn: () => listJobs(id!),
    enabled,
    refetchInterval: 3000,
  });
  const anyRunning =
    !!stages.data?.stages.some((s) => s.activity?.running_job_id) ||
    !!jobs.data?.some((j) => j.state === "running" || j.state === "queued");

  const evidence = useQuery({
    queryKey: ["evidence", id],
    queryFn: () => getEvidence(id!),
    enabled,
    // Live while jobs run; idle otherwise. useJob also invalidates ["evidence"]
    // on job finish for a final refresh.
    refetchInterval: anyRunning ? 2500 : false,
  });

  // Both toggles update the evidence cache OPTIMISTICALLY so the row recolors
  // the instant the user clicks — no wait for the round-trip. On error we roll
  // back to the snapshot; on settle we refetch evidence AND the PRISMA funnel
  // (which reads the same user_exclusions.json server-side) so the diagram's
  // counts move in lockstep with the table.
  const patchRow = (doi: string, patch: Partial<EvidenceRow>) => {
    qc.setQueryData<EvidenceOut>(["evidence", id], (prev) =>
      prev
        ? { ...prev, rows: prev.rows.map((r) => (r.doi === doi ? { ...r, ...patch } : r)) }
        : prev,
    );
  };
  const settleToggle = () => {
    qc.invalidateQueries({ queryKey: ["evidence", id] });
    qc.invalidateQueries({ queryKey: ["prisma", id] });
  };
  const exclude = useMutation({
    mutationFn: (doi: string) => excludePapers(id!, [doi]),
    onMutate: async (doi: string) => {
      await qc.cancelQueries({ queryKey: ["evidence", id] });
      const prev = qc.getQueryData<EvidenceOut>(["evidence", id]);
      // Excluded ⇒ red now; drop the green flag + any force-include at once.
      patchRow(doi, {
        is_excluded: true,
        included: false,
        user_included: false,
        screen_state: "excluded",
      });
      return { prev };
    },
    onError: (e, _doi, ctx) => {
      if (ctx?.prev) qc.setQueryData(["evidence", id], ctx.prev);
      toast.error((e as Error)?.message ?? "Could not exclude");
    },
    onSettled: settleToggle,
  });
  const include = useMutation({
    mutationFn: (doi: string) => includePapers(id!, [doi]),
    onMutate: async (doi: string) => {
      await qc.cancelQueries({ queryKey: ["evidence", id] });
      const prev = qc.getQueryData<EvidenceOut>(["evidence", id]);
      // Manual include is now a FORCE-include (mirrors the server): the paper
      // goes green immediately and to extraction, overriding the full-text
      // verdict — no waiting on Pending. Clears any stale exclusion.
      patchRow(doi, {
        is_excluded: false,
        exclusion_reason: null,
        included: true,
        user_included: true,
        screen_state: "included",
      });
      return { prev };
    },
    onError: (e, _doi, ctx) => {
      if (ctx?.prev) qc.setQueryData(["evidence", id], ctx.prev);
      toast.error((e as Error)?.message ?? "Could not include");
    },
    onSettled: settleToggle,
  });

  const rows = evidence.data?.rows ?? [];

  const screenedIn = rows.filter((r) => r.relevance === "yes" && !r.is_excluded);
  const withFulltext = screenedIn.filter((r) => r.has_fulltext).length;

  // Scheduling belongs to the server; polling only displays progress.
  const missingWanted = rows.filter(
    (r) => (r.screen_state === "included" || r.screen_state === "pending") && !r.has_fulltext,
  ).length;
  const pdfJob = jobs.data?.find((j) => j.kind === "pdf");
  const pdfRunning = pdfJob?.state === "running" || pdfJob?.state === "queued";
  const pendingScreen = rows.filter((r) => r.screen_state === "pending" && r.has_fulltext).length;
  const screenJob = jobs.data?.find((j) => j.kind === "fulltext_screen");
  const screenRunning = screenJob?.state === "running" || screenJob?.state === "queued";

  // Live per-item progress (downloaded/converted i of N, ETA) only comes from
  // GET /jobs/{id}, which overlays the worker's sidecar — listJobs returns the
  // between-steps DB row. Poll it while the fetch runs.
  const pdfDetail = useQuery({
    queryKey: ["job", pdfJob?.id],
    queryFn: () => getJob(pdfJob!.id),
    enabled: !!pdfJob && pdfRunning,
    refetchInterval: 2000,
  });
  const pdfLive = pdfDetail.data?.progress?.live ?? null;

  // Foreground pipeline run (extraction, export, …). Unlike the
  // background jobs, this is what "Run stage" started, and its progress is
  // otherwise only visible on the stage page it was launched from — so surface it
  // above the table wherever the user is. Poll GET /jobs/{id} for the live
  // done/total/ETA (the worker's sidecar; listJobs only has the between-steps row).
  const pipelineJob = jobs.data?.find(
    (j) =>
      (j.kind === "pipeline" || j.kind === "step") &&
      (j.state === "running" || j.state === "queued"),
  );
  const pipelineDetail = useQuery({
    queryKey: ["job", pipelineJob?.id],
    queryFn: () => getJob(pipelineJob!.id),
    enabled: !!pipelineJob,
    refetchInterval: 2000,
  });
  const pipelineProg = pipelineDetail.data?.progress;
  const pipelineLive = pipelineProg?.live ?? null;

  // Stop the foreground run. The pipeline is resumable (every step skips work
  // already on disk), so this is safe: run the stage again to pick up where it
  // left off. The cancel endpoint marks the job row cancelled; the worker kills
  // the subprocess when it notices.
  const stopRun = useMutation({
    mutationFn: (jobId: string) => cancelJob(jobId),
    onSuccess: () => {
      toast.success("Run stopped — progress so far is saved; run again to resume.");
      qc.invalidateQueries({ queryKey: ["review-jobs", id] });
      qc.invalidateQueries({ queryKey: ["stages", id] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not stop the run"),
  });

  if (!enabled || rows.length === 0) return null;

  return (
    // Full screen width, just a small margin on the ends — the evidence table
    // is wide (many columns) and benefits from all the horizontal room.
    <div className="px-6 pb-10">
      {/* We want these papers but couldn't fetch full text — offer the manual
          worklist. Only shown once screening has produced a wanted set. */}
      {missingWanted > 0 && (
        <Link
          href={`/reviews/${id}/missing-pdfs`}
          className="mb-3 flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 hover:bg-amber-500/15 dark:text-amber-400"
        >
          <FileWarning size={15} className="shrink-0" />
          <span>
            We&apos;re missing full text for{" "}
            <span className="font-semibold">{missingWanted.toLocaleString()}</span>{" "}
            paper{missingWanted === 1 ? "" : "s"} you want to include — get them
            manually →
          </span>
        </Link>
      )}
      {/* Foreground pipeline run (e.g. extraction) — a real status block above the
          table with a text progress bar, since the browser has no tqdm. Visible
          from any page while the run is going. */}
      {pipelineJob && (
        <div className="mb-3 rounded-lg border border-primary/40 bg-primary/5 p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
              <Loader2 size={14} className="shrink-0 animate-spin text-primary" />
              <span className="font-medium">
                {pipelineLive?.label ?? pipelineProg?.step_label ?? "Running"}
              </span>
              {(pipelineProg?.total ?? 0) > 1 && (
                <span className="text-xs text-muted-foreground">
                  step {pipelineProg?.current ?? 1} of {pipelineProg?.total}
                </span>
              )}
              {pipelineLive?.total ? (
                <span className="tabular-nums text-muted-foreground">
                  {(pipelineLive.done ?? 0).toLocaleString()} /{" "}
                  {pipelineLive.total.toLocaleString()} (
                  {Math.round((100 * (pipelineLive.done ?? 0)) / pipelineLive.total)}
                  %)
                </span>
              ) : (
                <span className="text-muted-foreground">
                  {pipelineJob.state === "queued" ? "queued…" : "starting…"}
                </span>
              )}
            </div>
            <button
              type="button"
              disabled={stopRun.isPending}
              onClick={() => stopRun.mutate(pipelineJob.id)}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-red-600/40 bg-red-600/10 px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-600/20 disabled:opacity-50 dark:text-red-400"
            >
              {stopRun.isPending ? (
                <Loader2 size={11} className="shrink-0 animate-spin" />
              ) : (
                <Square size={11} className="shrink-0 fill-current" />
              )}
              {stopRun.isPending ? "Stopping…" : "Stop"}
            </button>
          </div>
          {/* tqdm-style bar: determinate once the step reports item counts, an
              indeterminate sweep while it queues/boots (so it never looks dead). */}
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-primary/15">
            {pipelineLive?.total ? (
              <div
                className="h-full rounded-full bg-primary transition-all duration-500"
                style={{
                  width: `${Math.max(
                    1.5,
                    (100 * (pipelineLive.done ?? 0)) / pipelineLive.total,
                  )}%`,
                }}
              />
            ) : (
              <div className="h-full w-1/3 animate-dock-sweep rounded-full bg-primary/50" />
            )}
          </div>
          {pipelineLive?.total ? (
            <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] tabular-nums text-muted-foreground">
              {!!pipelineLive.rate && (
                <span>
                  {pipelineLive.rate >= 1
                    ? `${pipelineLive.rate.toFixed(1)}/s`
                    : `${(pipelineLive.rate * 60).toFixed(1)}/min`}
                </span>
              )}
              {pipelineLive.elapsed_s != null && (
                <span>{fmtDur(pipelineLive.elapsed_s)} elapsed</span>
              )}
              {/* Truthy, not != null: a 0 ETA means "unknown/not yet computed"
                  and must not render as "~0s left". */}
              {!!pipelineLive.eta_s && <span>~{fmtDur(pipelineLive.eta_s)} left</span>}
            </div>
          ) : null}
          <p className="mt-1.5 text-xs text-muted-foreground">
            This runs in the background — you can leave this page and it will keep
            going. Stopping is safe: completed papers are saved, and running again
            resumes where it left off.
          </p>
        </div>
      )}
      {/* Background-job chips sit side by side (wrapping only when the row is too
          narrow), separated by a divider, rather than stacking vertically. */}
      {(pdfRunning || screenRunning || pendingScreen > 0) && (
        <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {pdfRunning && (
            <span className="flex items-center gap-2">
              <Loader2 size={13} className="shrink-0 animate-spin" />
              <span>
                {stepVerb(pdfDetail.data?.progress?.step_id) ?? "Fetching"} full texts
                in the background
              </span>
              {/* N / M = full-text-having / screened-in, counted from the rows
                  (has_fulltext flips live as papers convert). The download is now
                  restricted server-side (--only-dois) to this SAME screened-in
                  set, so this count matches what the job actually fetches. The
                  job's live ETA rides along when the scripts expose one. */}
              {screenedIn.length > 0 && (
                <span className="tabular-nums">
                  {withFulltext.toLocaleString()} / {screenedIn.length.toLocaleString()}
                  {!!pdfLive?.eta_s && ` · ETA ~${fmtDur(pdfLive.eta_s)}`}
                </span>
              )}
            </span>
          )}
          {pdfRunning && (screenRunning || pendingScreen > 0) && (
            <span aria-hidden className="text-border">|</span>
          )}
          {/* The spinner claims work is happening, so it shows only while a
              fulltext_screen job actually runs. Papers merely awaiting the
              screen (none running — queued server-side, or waiting on a busy
              LLM queue) get a neutral count instead of a spinner that never
              stops. */}
          {screenRunning ? (
            <span className="flex items-center gap-2">
              <Loader2 size={13} className="shrink-0 animate-spin" />
              <span>
                Checking full-text eligibility in the background
                {pendingScreen > 0 &&
                  ` — ${pendingScreen.toLocaleString()} paper${pendingScreen === 1 ? "" : "s"} pending`}
              </span>
            </span>
          ) : (
            pendingScreen > 0 && (
              <span className="flex items-center gap-2">
                <Hourglass size={13} className="shrink-0" aria-hidden />
                <span>
                  {pendingScreen.toLocaleString()} paper
                  {pendingScreen === 1 ? "" : "s"} awaiting the full-text screen
                </span>
              </span>
            )
          )}
        </div>
      )}
      <EvidenceTable
        reviewId={id!}
        rows={rows}
        stageReached={evidence.data?.stage_reached ?? "search"}
        onExclude={(doi) => exclude.mutate(doi)}
        onInclude={(doi) => include.mutate(doi)}
        isMutating={exclude.isPending || include.isPending}
      />
    </div>
  );
}

/** The active phase of the fetch, from the running step id, for the chip verb. */
function stepVerb(stepId: string | null | undefined): string | null {
  if (stepId === "5_download_pdfs") return "Downloading";
  if (stepId === "6_convert_pdfs") return "Converting";
  return null;
}
