"use client";

/** One wizard stage.
 *
 *  The page owns fetching and dispatch; `StageShell` owns the chrome and the
 *  run/approve actions; the body owns that stage's settings. Bodies are chosen
 *  by slug with a generic fallback, so every stage is reachable from day one and
 *  bespoke bodies replace the fallback one at a time.
 */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect } from "react";
import type { ComponentType } from "react";

import { AgentRail } from "@/components/AgentRail";
import { BlockersPanel } from "@/components/BlockersPanel";
import { JobConsole } from "@/components/JobConsole";
import { StageShell } from "@/components/StageShell";
import { ClassifyStage } from "@/components/stages/ClassifyStage";
import { CurateStage } from "@/components/stages/CurateStage";
import { ExportStage } from "@/components/stages/ExportStage";
import { ExtractStage } from "@/components/stages/ExtractStage";
import { GenericStage } from "@/components/stages/GenericStage";
import { MetaStage } from "@/components/stages/MetaStage";
import { ReportStage } from "@/components/stages/ReportStage";
import { SearchStage } from "@/components/stages/SearchStage";
import { SetupStage } from "@/components/stages/SetupStage";
import { useJob } from "@/hooks/useJob";
import { rememberStage, usePref } from "@/lib/prefs";
import { getStages } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { StageOut, StagesOut } from "@/lib/types";

export interface StageBodyProps {
  reviewId: string;
  stage: StageOut;
}

/** Bespoke bodies, added incrementally. Anything absent gets GenericStage. */
const BODIES: Record<string, ComponentType<StageBodyProps>> = {
  setup: SetupStage,
  search: SearchStage,
  classify: ClassifyStage,
  triage: CurateStage,
  extract: ExtractStage,
  meta: MetaStage,
  // Keys are STAGE SLUGS (see services/stages.py). A slug with no entry here
  // silently falls back to GenericStage — which renders "This stage has no
  // settings", not an error — so a slug rename that misses this map looks like
  // a blank stage rather than a bug. Old slugs cannot be aliased here: the page
  // only renders slugs present in the server's stage list ("No such stage").
  "view-dashboard": ExportStage,
  report: ReportStage,
};

export default function StagePage() {
  const params = useParams<{ id: string; slug: string }>();
  const { id, slug } = params;
  const { isAuthenticated, ready } = useAuth();
  const enabled = ready && isAuthenticated && !!id;

  const stages = useQuery<StagesOut>({
    queryKey: ["stages", id],
    queryFn: () => getStages(id),
    enabled,
    // Follow background work so its progress bar advances while the user
    // configures this stage.
    refetchInterval: (q) =>
      q.state.data?.stages.some((s) => s.activity?.running_job_id) ? 3000 : false,
  });

  const [debug] = usePref("debugMode");
  // No job console on the dashboard page (slug "view-dashboard") even in debug
  // mode — it's a presentation surface, and the stage shows its own build
  // progress panel. The log is only streamed while the console is shown.
  const showConsole = debug && slug !== "view-dashboard";
  const { job, jobActive, logLines, stuckQueued } = useJob(id, enabled, {
    streamLog: showConsole,
  });

  // Bookmark this stage as where the user left off — the review's bare URL
  // redirects here next time. Only once the slug is confirmed real, so a typo'd
  // URL doesn't become the bookmark.
  const isRealStage = !!stages.data?.stages.some((s) => s.slug === slug);
  useEffect(() => {
    if (isRealStage) rememberStage(id, slug);
  }, [id, slug, isRealStage]);

  if (!ready || stages.isLoading) {
    return (
      <div className="mx-auto max-w-7xl animate-pulse px-4 py-8">
        <div className="mb-6 h-8 w-72 rounded bg-muted" />
        <div className="h-48 rounded-lg border border-border bg-muted" />
      </div>
    );
  }

  const all = stages.data?.stages ?? [];
  const stage = all.find((s) => s.slug === slug);

  if (!stage) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10">
        <h1 className="text-lg font-semibold">No such stage</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          This review has no stage called <code>{slug}</code>.
        </p>
        <Link
          href={`/reviews/${id}`}
          className="mt-3 inline-block text-sm text-blue-600 underline"
        >
          ← Back to the review
        </Link>
      </div>
    );
  }

  const Body = BODIES[slug] ?? GenericStage;

  return (
    <StageShell
      reviewId={id}
      stage={stage}
      stages={all}
      phases={stages.data?.phases ?? []}
      currentStage={stages.data?.current_stage ?? null}
      aside={<AgentRail reviewId={id} stage={stage} job={job} />}
      warnings={<BlockersPanel reviewId={id} stage={stage} />}
      backgroundLive={jobActive ? (job?.progress?.live ?? null) : null}
    >
      <Body reviewId={id} stage={stage} />
      {showConsole && job && (
        <div className="mt-6">
          <JobConsole
            job={job}
            jobActive={jobActive}
            logLines={logLines}
            stuckQueued={stuckQueued}
            stepLabel={job.progress?.step_label}
          />
        </div>
      )}
    </StageShell>
  );
}
