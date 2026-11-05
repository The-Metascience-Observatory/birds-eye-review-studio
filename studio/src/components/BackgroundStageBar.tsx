"use client";

/** A slim progress bar for a stage running in the BACKGROUND on another page.
 *
 *  Some stages are pure fetches with nothing to configure and no decision to
 *  make — fetching abstracts is the archetype. Giving them their own page means
 *  the user sits and watches a bar, which is time they could spend configuring
 *  the next stage. So the wizard skips past them and shows their progress here
 *  instead, letting the download run while the user works.
 *
 *  A bar, not a terminal: there is no decision to make from the log, and a
 *  scrolling console over someone's settings form is noise. The link to the
 *  stage page stays, for when something goes wrong and the log DOES matter.
 */

import { AlertTriangle, Loader2 } from "lucide-react";
import Link from "next/link";

import { fmtDur } from "@/lib/format";
import type { JobLive, StageOut } from "@/lib/types";

export function BackgroundStageBar({
  reviewId,
  stage,
  live,
  failed,
}: {
  reviewId: string;
  stage: StageOut;
  /** Live counters for this stage's step, when it is the running one. */
  live: JobLive | null;
  failed?: boolean;
}) {
  const running = !!stage.activity?.running_job_id;

  // Only the live and failed states are worth surfacing here. Once a background
  // fetch finishes cleanly there is nothing to act on, so the bar disappears
  // rather than leaving a stale "finished" notice on the page.
  if (!running && !failed) return null;

  const pct =
    live && live.total
      ? Math.min(100, Math.round(((live.done ?? 0) / live.total) * 100))
      : null;

  if (failed) {
    return (
      <Shell tone="red" reviewId={reviewId} stage={stage}>
        <AlertTriangle size={14} className="shrink-0" />
        <span className="min-w-0 flex-1">
          {stage.title} failed — open the stage to see what happened.
        </span>
      </Shell>
    );
  }

  return (
    <Shell tone="muted" reviewId={reviewId} stage={stage}>
      <Loader2 size={14} className="shrink-0 animate-spin" />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-baseline gap-x-2">
          <span>{stage.title} — running in the background</span>
          {live?.total != null && (
            <span className="text-xs tabular-nums text-muted-foreground">
              {(live.done ?? 0).toLocaleString()} / {live.total.toLocaleString()}
              {live.eta_s != null && ` · ~${fmtDur(live.eta_s)} left`}
            </span>
          )}
        </span>
        <span className="mt-1.5 block h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <span
            className={`block h-full rounded-full bg-primary transition-all ${
              pct == null ? "w-1/3 animate-pulse" : ""
            }`}
            style={pct == null ? undefined : { width: `${pct}%` }}
          />
        </span>
      </span>
    </Shell>
  );
}

function Shell({
  tone,
  reviewId,
  stage,
  children,
}: {
  tone: "red" | "green" | "muted";
  reviewId: string;
  stage: StageOut;
  children: React.ReactNode;
}) {
  const cls = {
    red: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400",
    green: "border-green-600/40 bg-green-600/10 text-green-700 dark:text-green-500",
    muted: "border-border bg-card text-foreground",
  }[tone];
  return (
    <div className={`mb-4 flex items-start gap-2 rounded border px-3 py-2 text-sm ${cls}`}>
      {children}
      <Link
        href={`/reviews/${reviewId}/stages/${stage.slug}`}
        className="shrink-0 text-xs text-muted-foreground underline hover:text-foreground"
      >
        details
      </Link>
    </div>
  );
}
