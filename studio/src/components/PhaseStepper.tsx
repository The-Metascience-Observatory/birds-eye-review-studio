"use client";

/** The phase progress rail (Define · Search · Screen · Review · Extract ·
 *  Meta-analysis · View dashboard), papertrace-style. Labels come from the
 *  server (`StagesOut.phases`) — never hardcode them here.
 *
 *  A PHASE is a display grouping over stages — it has no gate and no status of
 *  its own on the server. This component DERIVES a phase's status from the
 *  stages it contains, so the rail and the stage list can never disagree: both
 *  read the same resolved `StageOut[]`. The phase list itself comes from the
 *  server (`StagesOut.phases`), so adding or reordering phases needs no client
 *  change.
 */

import Link from "next/link";
import { Check, Loader2 } from "lucide-react";

import type { PhaseOut, StageOut } from "@/lib/types";

type PhaseStatus = "done" | "running" | "current" | "todo";

/** Roll a phase's stages up to one status.
 *
 *  - running — a job is ACTUALLY executing in one of this phase's stages. The
 *    only state that may animate: a spinner reads as "work is happening", and
 *    showing one for a merely-current phase had users hunting for a job that
 *    didn't exist.
 *  - current — the wizard's current stage lives here (nothing running). This
 *    is where the user is; a static marker.
 *  - done    — every gate-bearing stage is approved (or skipped/needs-no-approval
 *    and done). The phase is behind the user.
 *  - todo    — ahead of the user, nothing done yet.
 *
 *  Skipped stages don't count either way — a phase of only skipped stages is
 *  `done` (there was no work), which is what a user scanning the rail expects.
 */
function phaseStatus(
  phaseSlug: string,
  stages: StageOut[],
  currentStage: string | null,
): PhaseStatus {
  const inPhase = stages.filter(
    (s) => s.phase === phaseSlug && s.slug !== "_other" && !s.skipped && !s.hidden,
  );
  if (inPhase.length === 0) return "done"; // all skipped / hidden / empty

  if (inPhase.some((s) => s.activity?.running_job_id)) return "running";
  if (currentStage && inPhase.some((s) => s.slug === currentStage))
    return "current";

  const settled = inPhase.every((s) => {
    if (!s.requires_approval) return s.status === "done" || s.status === "n/a";
    return s.gate?.state === "approved";
  });
  return settled ? "done" : "todo";
}

/** The first (in registry order) stage of a phase, so the label can link into
 *  the wizard at the right place. */
function phaseEntry(phaseSlug: string, stages: StageOut[]): StageOut | null {
  return (
    stages.find(
      (s) =>
        s.phase === phaseSlug && s.slug !== "_other" && !s.skipped && !s.hidden,
    ) ?? null
  );
}

export function PhaseStepper({
  reviewId,
  phases,
  stages,
  currentStage,
}: {
  reviewId: string;
  phases: PhaseOut[];
  stages: StageOut[];
  currentStage: string | null;
}) {
  if (phases.length === 0) return null;

  return (
    <ol className="flex w-full items-center gap-1 pb-1">
      {/* No leading "Overview" node: the timeline IS the navigation. The bare
          review URL redirects to the last-visited stage, so there is nowhere
          separate to go "home" to. */}
      {phases.map((p, i) => {
        const status = phaseStatus(p.slug, stages, currentStage);
        const entry = phaseEntry(p.slug, stages);
        const last = i === phases.length - 1;

        const dot =
          status === "done" ? (
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-green-600 text-white">
              <Check size={12} strokeWidth={3} />
            </span>
          ) : status === "running" ? (
            // The ONLY animated dot: a spinner means a job is executing NOW.
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
              <Loader2 size={12} className="animate-spin" />
            </span>
          ) : status === "current" ? (
            // "You are here": a static numbered dot; the boxed LABEL below is
            // the marker (per user preference — no fill, definitely no spin).
            // The stronger border keeps narrow screens (label hidden) legible.
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 border-primary text-[10px] font-semibold text-foreground">
              {i + 1}
            </span>
          ) : (
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-border text-[10px] font-semibold text-muted-foreground">
              {i + 1}
            </span>
          );

        const label = (
          // Hidden on the narrowest screens so a dots-only rail always fits;
          // truncates rather than forcing overflow once shown. The CURRENT
          // phase's name gets a box — that is the "you are here" marker.
          <span
            className={`hidden truncate text-xs font-medium min-[420px]:inline ${
              status === "current"
                ? "rounded border border-primary px-1.5 py-0.5 text-foreground"
                : status === "running"
                  ? "text-foreground"
                  : status === "done"
                    ? "text-muted-foreground"
                    : "text-muted-foreground/60"
            }`}
          >
            {p.label}
          </span>
        );

        return (
          <li
            key={p.slug}
            className={`flex min-w-0 items-center gap-1 ${last ? "" : "flex-1"}`}
          >
            {entry ? (
              <Link
                href={`/reviews/${reviewId}/stages/${entry.slug}`}
                className="flex min-w-0 items-center gap-1.5 rounded px-1 py-1 hover:bg-accent"
                title={`Go to ${p.label}`}
              >
                {dot}
                {label}
              </Link>
            ) : (
              <span className="flex min-w-0 items-center gap-1.5 px-1 py-1">
                {dot}
                {label}
              </span>
            )}
            {!last && (
              // Flexible connector: absorbs slack on wide screens and shrinks
              // toward zero on narrow ones, so the rail never overflows.
              <span
                className={`h-px min-w-1 flex-1 ${
                  status === "done" ? "bg-green-600/50" : "bg-border"
                }`}
                aria-hidden
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}
