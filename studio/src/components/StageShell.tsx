"use client";

/** One wizard stage: header, body slot, and the Back / Run / Approve footer.
 *
 *  Back, Next, and Approve are deliberately three distinct affordances.
 *  Conflating them is what makes a wizard feel like a trap:
 *
 *  - Back is pure navigation and NEVER un-approves. Un-approving on Back would
 *    silently cascade-block every downstream stage the moment a user clicked to
 *    re-read something. Withdrawal is a separate, confirmed action.
 *  - Run does the work. Approve records that a human accepted it.
 *  - "Skip for now" navigates without approving, so a hard gate never traps
 *    someone who is only browsing.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  Loader2,
  Lock,
  Play,
  RotateCcw,
  RotateCw,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "@/lib/toast";

import { BackgroundStageBar } from "@/components/BackgroundStageBar";
import { PhaseStepper } from "@/components/PhaseStepper";
import { Banner } from "@/components/ui/Banner";
import { approveStage, runStage, unapproveStage } from "@/lib/api";
import type { JobLive, PhaseOut, StageOut, StagesOut } from "@/lib/types";

/** Stages whose edits persist the instant they are made, so the approve /
 *  withdraw / re-run footer is noise: there is never unsaved work to sign off.
 *  Every OTHER stage keeps its gate — those run expensive jobs where an explicit
 *  "I looked at this" is the whole point. */
const AUTOSAVE_STAGES = new Set(["view-dashboard", "report"]);

/** Backoff for retrying a REFUSED autorun start (5s, 10s, 20s … capped). */
const AUTORUN_BASE_BACKOFF_MS = 5_000;
const AUTORUN_MAX_BACKOFF_MS = 60_000;

/** The last autorun start attempted for a slug. */
interface AutoRunAttempt {
  slug: string;
  inFlight: boolean;
  /** The server accepted the start — never auto-fire this slug again. */
  started: boolean;
  failures: number;
  /** Epoch ms before which a refused start is not retried. */
  retryAt: number;
}

export function StageShell({
  reviewId,
  stage,
  stages,
  phases = [],
  currentStage = null,
  children,
  aside,
  warnings,
  backgroundLive,
}: {
  reviewId: string;
  stage: StageOut;
  stages: StageOut[];
  /** The six phases, for the stepper. Empty renders nothing. */
  phases?: PhaseOut[];
  /** Slug of the wizard's current stage — the stepper highlights its phase. */
  currentStage?: string | null;
  children: ReactNode;
  /** The agent rail. Passed in so this component stays presentational. */
  aside?: ReactNode;
  /** Run-check warnings. Its own column right of the agent rail on wide screens;
   *  falls back to the top of the main column below `xl`. Renders nothing when
   *  there are no warnings (the component returns null), so the column collapses. */
  warnings?: ReactNode;
  /** Live counters for whichever step is currently running, for the background
   *  progress bars. Owned by the page (which owns useJob). */
  backgroundLive?: JobLive | null;
}) {
  const router = useRouter();
  const qc = useQueryClient();
  const [confirmWithdraw, setConfirmWithdraw] = useState(false);

  // Stages that RUN (incl. hidden background ones) vs. stages the user SEES in
  // the walkthrough. Hidden stages (e.g. fetch-abstracts) still auto-run and show
  // a background bar, but are not numbered stages the user steps through.
  const runnable = stages.filter((s) => s.slug !== "_other" && !s.skipped);
  const real = runnable.filter((s) => !s.hidden);
  const idx = real.findIndex((s) => s.slug === stage.slug);
  const prev = idx > 0 ? real[idx - 1] : null;
  const next = idx >= 0 && idx < real.length - 1 ? real[idx + 1] : null;

  const gate = stage.gate;
  const running = !!stage.activity?.running_job_id;
  const anyRunning = stages.some((s) => s.activity?.running_job_id);
  // Artifacts already on disk for this stage, so the run button can say
  // "Re-run" rather than implying the last run never happened.
  const hasRun = stage.status === "done" || stage.status === "partial";

  /** Seed the cache from the mutation response so the client's idea of "next"
   *  cannot disagree with the server's — the endpoint already returns the whole
   *  list with a recomputed current_stage. */
  function applyStages(data: StagesOut) {
    qc.setQueryData(["stages", reviewId], data);
    return data;
  }

  const run = useMutation({
    mutationFn: () => runStage(reviewId, stage.slug),
    onSuccess: () => {
      // No success toast: the button switches to "Running…" and the progress
      // bar reflects the run, so a dropdown notification is just noise.
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
      // useJob follows the newest job from this list; refreshing it now
      // attaches the console/progress to the new run without waiting a poll.
      qc.invalidateQueries({ queryKey: ["review-jobs", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not start"),
  });

  const approve = useMutation({
    mutationFn: () => approveStage(reviewId, stage.slug),
    onSuccess: (data) => {
      applyStages(data);
      const target = data.current_stage ?? next?.slug;
      if (target && target !== stage.slug) {
        router.push(`/reviews/${reviewId}/stages/${target}`);
      }
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not approve"),
  });

  const withdraw = useMutation({
    mutationFn: () => unapproveStage(reviewId, stage.slug),
    onSuccess: (data) => {
      applyStages(data);
      setConfirmWithdraw(false);
      toast.success("Approval withdrawn");
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not withdraw"),
  });

  // Background stages (pure fetches with nothing to configure) are started FROM
  // WHEREVER THE USER IS, not by visiting their page — the point is that the
  // download runs while they configure the next stage. Only fires when the
  // stage is unblocked and has not run, so it never repeats work, never
  // bypasses a gate, and never touches a stage that costs money.
  //
  // A successful start is never repeated for the same slug (if that job then
  // fails, re-firing it on every poll would loop). A REJECTED start — a 409
  // because another job holds the review, or a transient network error — is
  // retried, with an exponential backoff so a persistent refusal can't turn
  // into a request per poll.
  const autoFired = useRef<AutoRunAttempt | null>(null);
  // Bumped when a backoff window expires, so the retry happens even if no
  // stages poll arrives to re-run the effect.
  const [retryTick, setRetryTick] = useState(0);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // This component survives /reviews/[id] navigation (no remount), so a slug
  // fired for review A must not suppress the same slug's auto-run in review B.
  useEffect(() => {
    autoFired.current = null;
    return () => {
      if (retryTimer.current) clearTimeout(retryTimer.current);
      retryTimer.current = null;
    };
  }, [reviewId]);
  // Scan RUNNABLE (not just displayed) stages so a hidden autorun stage still
  // auto-starts.
  const pending = runnable.find(
    (s) =>
      s.autorun &&
      s.step_ids.length > 0 &&
      s.status === "pending" &&
      !s.activity?.running_job_id &&
      s.gate?.state !== "blocked",
  );
  const pendingSlug = pending?.slug ?? null;
  const autoRun = useMutation({
    mutationFn: (slug: string) => runStage(reviewId, slug),
  });
  useEffect(() => {
    if (!pendingSlug) return;
    if (anyRunning) return; // one job per review; wait for the current one
    const prior = autoFired.current;
    if (prior && prior.slug === pendingSlug) {
      if (prior.inFlight || prior.started) return;
      if (Date.now() < prior.retryAt) return; // still backing off
    }
    const attempt: AutoRunAttempt = {
      slug: pendingSlug,
      inFlight: true, // ref guard: strict-mode double-mount
      started: false,
      failures: prior?.slug === pendingSlug ? prior.failures : 0,
      retryAt: 0,
    };
    autoFired.current = attempt;
    autoRun.mutate(pendingSlug, {
      onSuccess: () => {
        attempt.inFlight = false;
        attempt.started = true;
        qc.invalidateQueries({ queryKey: ["stages", reviewId] });
        qc.invalidateQueries({ queryKey: ["review-jobs", reviewId] });
      },
      onError: () => {
        // A 409 (busy) is normal — retry after a backoff, not on every poll.
        attempt.inFlight = false;
        attempt.failures += 1;
        const delay = Math.min(
          AUTORUN_MAX_BACKOFF_MS,
          AUTORUN_BASE_BACKOFF_MS * 2 ** (attempt.failures - 1),
        );
        attempt.retryAt = Date.now() + delay;
        if (retryTimer.current) clearTimeout(retryTimer.current);
        retryTimer.current = setTimeout(() => setRetryTick((n) => n + 1), delay);
      },
    });
    // `autoRun` is stable enough (mutate's identity doesn't matter here) and
    // must not be a dependency: its state changes would re-run this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingSlug, anyRunning, retryTick, reviewId, qc]);

  const busy = run.isPending || approve.isPending || running;
  // How many stages a withdrawal would block, so the confirm can be specific.
  const downstream = idx >= 0 ? real.length - idx - 1 : 0;

  return (
    <div className="mx-auto flex max-w-screen-2xl gap-6 px-4 py-8">
      <main className="min-w-0 flex-1">
        {phases.length > 0 && (
          <div className="mb-4">
            <PhaseStepper
              reviewId={reviewId}
              phases={phases}
              stages={stages}
              currentStage={currentStage}
            />
          </div>
        )}
        <header className="mb-6">
          {/* No back-link here: the timeline (PhaseStepper) above IS the
              navigation — there is no separate overview page anymore. */}
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h1 className="text-2xl font-bold">{stage.title}</h1>
          </div>
          {stage.description && (
            <p className="mt-1 max-w-prose text-sm text-muted-foreground">
              {stage.description}
            </p>
          )}
          {stage.details.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-sm text-muted-foreground">
              {stage.details.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          )}
        </header>

        {gate?.state === "blocked" && (
          <Banner tone="error" icon={Lock} className="mb-4">
            Blocked by{" "}
            <Link
              href={`/reviews/${reviewId}/stages/${gate.blocked_by}`}
              className="font-medium underline"
            >
              {stages.find((s) => s.slug === gate.blocked_by)?.title ??
                gate.blocked_by}
            </Link>
            {" — approve it first."}
          </Banner>
        )}

        {gate?.state === "stale" && (
          <Banner tone="warning" className="mb-4">
            The configuration changed since you approved this stage
            {gate.stale_keys.length > 0 && (
              <> ({gate.stale_keys.join(", ")})</>
            )}
            . Re-run it or approve again.
          </Banner>
        )}

        {/* Stages running in the background (incl. hidden ones like fetch-
            abstracts), so their progress is visible from whatever page the user
            is actually working on. */}
        {runnable
          .filter((s) => s.autorun && s.slug !== stage.slug)
          .map((s) => (
            <BackgroundStageBar
              key={s.slug}
              reviewId={reviewId}
              stage={s}
              live={backgroundLive?.step_id && s.step_ids.includes(backgroundLive.step_id) ? backgroundLive : null}
            />
          ))}

        <div className="mb-6">{children}</div>

        <footer className="sticky bottom-0 -mx-4 flex flex-wrap items-center gap-2 border-t border-border bg-background/95 px-4 py-3 backdrop-blur">
          {prev ? (
            <Link
              href={`/reviews/${reviewId}/stages/${prev.slug}`}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
            >
              <ArrowLeft size={14} />
              {prev.title}
            </Link>
          ) : (
            <span />
          )}

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {AUTOSAVE_STAGES.has(stage.slug) ? (
              // The dashboard stage has no approval ritual: every edit is
              // PATCHed the moment it is made, so there is nothing pending to
              // sign off, nothing to withdraw, and no "Done" to press. Showing
              // Approve/Withdraw/Re-run here implied unsaved work that never
              // existed. Leave the back link and say plainly that it's saved.
              <span className="text-xs text-muted-foreground">
                Changes save automatically.
              </span>
            ) : (
              <>
            {gate?.state === "approved" && (
              <button
                type="button"
                onClick={() =>
                  confirmWithdraw ? withdraw.mutate() : setConfirmWithdraw(true)
                }
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground"
              >
                <RotateCcw size={13} />
                {confirmWithdraw
                  ? downstream > 0
                    ? `Withdraw — this blocks ${downstream} later stage${downstream === 1 ? "" : "s"}`
                    : "Withdraw — click again"
                  : "Withdraw approval"}
              </button>
            )}

            {next && (
              <Link
                href={`/reviews/${reviewId}/stages/${next.slug}`}
                className="text-sm text-muted-foreground hover:text-foreground"
              >
                Skip for now
              </Link>
            )}

            {stage.step_ids.length > 0 && (
              <button
                type="button"
                onClick={() => run.mutate()}
                disabled={busy || gate?.state === "blocked"}
                title={
                  gate?.state === "blocked"
                    ? `Approve ${gate.blocked_by} first`
                    : hasRun
                      ? "Run this stage again. Steps are resumable, so finished work is not repeated unless its inputs changed."
                      : "Run this stage's pipeline steps"
                }
                className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-accent disabled:opacity-50"
              >
                {run.isPending || running ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : hasRun ? (
                  <RotateCw className="h-4 w-4" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
                {/* "Run stage" on a stage that already produced output reads as
                    though the last run did nothing. The label follows the
                    artifacts, and the button stays available after approval so
                    a stage can be refreshed without withdrawing the sign-off. */}
                {running
                  ? "Running…"
                  : hasRun
                    ? "Re-run stage"
                    : "Run stage"}
              </button>
            )}

            <PrimaryAction
              stage={stage}
              next={next}
              reviewId={reviewId}
              busy={busy}
              running={running}
              hasRun={hasRun}
              pending={approve.isPending}
              runPending={run.isPending}
              onApprove={() => approve.mutate()}
              onRun={() => run.mutate()}
            />
              </>
            )}
          </div>
        </footer>
      </main>

      {aside && (
        <aside className="hidden w-80 shrink-0 lg:block">
          <div className="sticky top-6">{aside}</div>
        </aside>
      )}

      {/* Warnings own their own <aside> wrapper and return null when there's
          nothing to show, so this column takes ZERO width (and no flex gap) when
          empty — no reserved white space beside the agent rail. */}
      {warnings}
    </div>
  );
}

/** The one button whose label depends on gate state. Never an ambiguous "Next":
 *  the user should always know whether clicking records a decision. */
function PrimaryAction({
  stage,
  next,
  reviewId,
  busy,
  running,
  hasRun,
  pending,
  runPending,
  onApprove,
  onRun,
}: {
  stage: StageOut;
  next: StageOut | null;
  reviewId: string;
  busy: boolean;
  running: boolean;
  hasRun: boolean;
  pending: boolean;
  runPending: boolean;
  onApprove: () => void;
  onRun: () => void;
}) {
  const gate = stage.gate;
  const cls =
    "inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50";

  // While THIS stage's job is running, don't let the user advance — the next
  // stage reads this one's output, and leaving mid-run hides the progress. Show a
  // disabled "…running" affordance in place of Continue / Approve & continue.
  if (running) {
    return (
      <span
        className={`${cls} cursor-not-allowed opacity-50`}
        title="Wait for this stage to finish before continuing."
      >
        <Loader2 className="h-4 w-4 animate-spin" />
        Running…
      </span>
    );
  }

  // Artifact-producing stages must have run before they can be approved —
  // approving a stage with no output is a rubber stamp.
  const needsRun =
    stage.step_ids.length > 0 &&
    (stage.status === "pending" || stage.status === "n/a");

  // A required stage that hasn't run yet: the PRIMARY action is to run it, not to
  // approve nothing. One clear blue "Run and continue" beats a greyed-out
  // "Approve & continue" the user can't press. (Skipped when the gate is blocked
  // upstream, or when the stage takes no run — those fall through below.)
  if (
    needsRun &&
    !hasRun &&
    gate &&
    gate.state !== "approved" &&
    gate.state !== "blocked"
  ) {
    return (
      <button
        type="button"
        onClick={onRun}
        disabled={busy || runPending}
        title="Run this stage's pipeline steps, then continue when it finishes."
        className={cls}
      >
        {runPending ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <Play className="h-4 w-4" />
        )}
        Run and continue
      </button>
    );
  }

  // Already approved, or needs no approval: plain navigation.
  if (!gate || gate.state === "approved") {
    if (!next) {
      // Last stage: the bare review URL just redirects back to the current
      // stage, so "Done" goes to the reviews list instead.
      return (
        <Link href="/dashboard" className={cls}>
          Done <Check className="h-4 w-4" />
        </Link>
      );
    }
    return (
      <Link href={`/reviews/${reviewId}/stages/${next.slug}`} className={cls}>
        Continue <ArrowRight className="h-4 w-4" />
      </Link>
    );
  }

  return (
    <button
      type="button"
      onClick={onApprove}
      disabled={busy || needsRun || gate.state === "blocked"}
      title={
        gate.state === "blocked"
          ? `Approve ${gate.blocked_by} first`
          : needsRun
            ? "Run this stage before approving it"
            : "Record that you have reviewed this stage, and continue"
      }
      className={cls}
    >
      {pending ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : (
        <Check className="h-4 w-4" />
      )}
      {gate.state === "stale" ? "Re-approve & continue" : "Approve & continue"}
    </button>
  );
}

