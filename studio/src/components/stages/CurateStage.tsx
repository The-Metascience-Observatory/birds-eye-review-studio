"use client";

/** The curation checkpoints (triage, papers).
 *
 *  These stages have no pipeline steps and no config — they exist so a human
 *  pauses to look before the pipeline spends money on the next batch. The
 *  actual include/exclude happens in the evidence table docked below every
 *  stage; this body gives that pause something to look AT: the screening funnel
 *  so far, so the reviewer can see how many papers are about to move on.
 */

import { PrismaCard } from "@/components/PrismaCard";
import { Banner } from "@/components/ui/Banner";
import type { StageOut } from "@/lib/types";

export function CurateStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const running = !!stage.activity?.running_job_id;

  return (
    <div className="space-y-4">
      <Banner tone="info">
        Review what has been screened before the pipeline continues. Toggle
        Include / Exclude on any paper in the evidence table below — the row
        recolors and the funnel below updates at once; extraction skips the
        excluded ones on the next run. When it looks right, approve to continue.
      </Banner>

      <PrismaCard reviewId={reviewId} running={running} />
    </div>
  );
}
