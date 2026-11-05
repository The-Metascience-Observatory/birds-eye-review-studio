"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getJob, listJobs, streamJobLog } from "@/lib/api";
import type { JobOut } from "@/lib/types";

export const JOB_TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/** The log console keeps at most this many lines (a ring buffer): a long
 *  extraction run emits tens of thousands, and the console only ever shows the
 *  tail. */
const LOG_MAX_LINES = 2000;
/** Batch streamed lines into one render per this many ms, not one per line. */
const LOG_FLUSH_MS = 100;

/** Query keys invalidated once when a job reaches a terminal state. */
const REFRESH_ON_FINISH = [
  "review-config",
  "review-jobs",
  "classify-summary",
  "preflight",
  "stages",
  "evidence",
  "feasibility",
  "prisma",
  // The export stage renders the dashboard inline — refresh it when the build
  // job lands (invalidate also refetches the previously-404'd summary query).
  "dashboard-state",
  "dashboard-summary",
  "dashboard-rows",
  "meta-analysis",
  // The report stage renders the synthesized document — refresh it when the
  // synthesis job lands.
  "review-document",
] as const;

export interface UseJob {
  job: JobOut | undefined;
  jobActive: boolean;
  activeJobId: string | null;
  setActiveJobId: (id: string | null) => void;
  /** The last LOG_MAX_LINES log lines streamed over SSE. Always empty unless
   *  the caller asked for the stream (`streamLog`). */
  logLines: string[];
  /** Queued for >6s — the worker is probably not running. */
  stuckQueued: boolean;
}

/**
 * Own the "current job" for a review: reattach to the newest one on load, poll
 * its state until terminal, stream its log over SSE while `streamLog` is set,
 * and refresh derived queries once it finishes.
 *
 * `streamLog` should be true only while the log is actually on screen (the
 * debug console). The stream is a long-lived connection the job's whole
 * output flows through; opening it for a log nobody can see was pure cost.
 */
export function useJob(
  reviewId: string,
  enabled: boolean,
  { streamLog = false }: { streamLog?: boolean } = {},
): UseJob {
  const queryClient = useQueryClient();
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);

  // The stage page component is REUSED across /reviews/[id]/* navigations, so
  // nothing remounts this hook when the review changes — without this reset the
  // latched job id (and its log) from review A leaks into review B, and
  // consumers like AgentRail would pair B's review id with A's job id.
  useEffect(() => {
    setActiveJobId(null);
    setLogLines([]);
  }, [reviewId]);

  // Attach to the most recent job (so a running job keeps streaming across
  // reloads, and a finished one still shows its outcome) — and FOLLOW a new
  // run when one starts: no caller sets activeJobId directly, so if the latch
  // never updated, a rerun would poll the old job forever and the new run
  // would look like it did nothing. A background job (metadata/pdf/…) never
  // steals the console from a live foreground run.
  const jobs = useQuery<JobOut[]>({
    queryKey: ["review-jobs", reviewId],
    queryFn: () => listJobs(reviewId),
    enabled,
  });
  useEffect(() => {
    const newest = jobs.data?.[0]; // list is newest-first
    if (!newest) return;
    if (!activeJobId) {
      setActiveJobId(newest.id);
      return;
    }
    if (newest.id === activeJobId || JOB_TERMINAL.has(newest.state)) return;
    const foreground = newest.kind === "pipeline" || newest.kind === "step";
    if (foreground) setActiveJobId(newest.id);
  }, [jobs.data, activeJobId]);

  const job = useQuery<JobOut>({
    queryKey: ["job", activeJobId],
    queryFn: () => getJob(activeJobId as string),
    enabled: enabled && !!activeJobId,
    refetchInterval: (q) =>
      q.state.data && JOB_TERMINAL.has(q.state.data.state) ? false : 1500,
  });

  const jobActive = !!job.data && !JOB_TERMINAL.has(job.data.state);

  // Stream the log over SSE. One stream per job id; the server flushes the
  // final bytes and emits `end`. Lines land in a bounded buffer (appending to
  // a fresh array per line was O(n²) over a long run) and reach React state in
  // batches. The job poll above still notices the terminal state on its own,
  // so not streaming loses nothing but the log text.
  useEffect(() => {
    setLogLines([]);
    if (!enabled || !activeJobId || !streamLog) return;
    const buf: string[] = [];
    let timer: ReturnType<typeof setTimeout> | null = null;
    const flush = () => {
      timer = null;
      setLogLines(buf.slice());
    };
    const stop = streamJobLog(
      activeJobId,
      (line) => {
        buf.push(line);
        if (buf.length > LOG_MAX_LINES) buf.splice(0, buf.length - LOG_MAX_LINES);
        if (timer == null) timer = setTimeout(flush, LOG_FLUSH_MS);
      },
      () => queryClient.invalidateQueries({ queryKey: ["job", activeJobId] }),
    );
    return () => {
      stop();
      if (timer != null) clearTimeout(timer);
    };
  }, [activeJobId, enabled, streamLog, queryClient]);

  // When a job finishes, refresh the derived pipeline state + config once.
  const prevState = useRef<string | null>(null);
  // A different job's states must not suppress the new job's finish refresh.
  useEffect(() => {
    prevState.current = null;
  }, [activeJobId]);
  useEffect(() => {
    const s = job.data?.state ?? null;
    if (s && JOB_TERMINAL.has(s) && prevState.current !== s) {
      for (const key of REFRESH_ON_FINISH) {
        queryClient.invalidateQueries({ queryKey: [key, reviewId] });
      }
    }
    prevState.current = s;
  }, [job.data?.state, reviewId, queryClient]);

  // Warn if a job sits queued (worker probably not running).
  const [queuedSince, setQueuedSince] = useState<number | null>(null);
  useEffect(() => {
    if (job.data?.state === "queued") setQueuedSince((t) => t ?? Date.now());
    else setQueuedSince(null);
  }, [job.data?.state]);
  const stuckQueued =
    job.data?.state === "queued" &&
    queuedSince != null &&
    Date.now() - queuedSince > 6000;

  return {
    job: job.data,
    jobActive,
    activeJobId,
    setActiveJobId,
    logLines,
    stuckQueued,
  };
}
