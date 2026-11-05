"use client";

/** Auto-build the dashboard whenever the export stage page needs one.
 *
 *  The export steps (13_build_bundle, 16_generate_dashboard) are CPU/IO only,
 *  spend no LLM tokens, and write only into the review's own workspace — so
 *  they are safe to start without a Run click. Unlike the server-side `autorun`
 *  flag (which StageShell fires from EVERY page the moment the gate opens),
 *  this fires only while the user is actually looking at the export page:
 *  "the dashboard builds itself when you arrive", not "the moment you approve
 *  extraction".
 *
 *  Fire conditions — ALL must hold:
 *  - dashboard state is KNOWN, and the bundle either does not exist or is
 *    STALE (source data newer than the built bundle). Stale bundles rebuild
 *    automatically too — the build is free and the alternative was a manual
 *    "Rebuild dashboard" button that every user had to press anyway;
 *  - the stage gate isn't blocked by an unapproved upstream stage;
 *  - nothing else is running (the export steps run as a normal pipeline job,
 *    subject to the one-job-per-review guard);
 *  - the LAST build didn't fail — after a failure `exists`/`stale` never
 *    change, so without this guard every page visit would re-fire a doomed
 *    build. The footer's "Run stage" button is the deliberate retry;
 *  - not already fired for this build/staleness episode. The ref is set before
 *    mutate (strict-mode safe) and cleared once the bundle is fresh again, so
 *    one visit can absorb several staleness events without ever looping on a
 *    build that fails to clear one.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { getStages, runStage, runStep } from "@/lib/api";
import type { DashboardState } from "@/lib/api";
import type { StageOut } from "@/lib/types";
import { useAuth } from "@/lib/auth";

export function useAutoBuildDashboard(
  reviewId: string,
  stage: StageOut,
  dash: UseQueryResult<DashboardState>,
): {
  /** A build is starting or running — show the "Building…" panel. */
  building: boolean;
  /** Manual re-run for the stale-bundle button. */
  rebuild: () => void;
  /** Any job running on the review (disables the rebuild button). */
  anyRunning: boolean;
  /** The pending/last-fired rebuild is a manifest-only reconcile (a layout
   *  edit), so the host should show a muted hint, not the amber banner. */
  layoutOnly: boolean;
} {
  const qc = useQueryClient();
  const { isAuthenticated, ready } = useAuth();

  // Piggyback the stage page's 3s poll of the same key — no extra interval here.
  const stages = useQuery({
    queryKey: ["stages", reviewId],
    queryFn: () => getStages(reviewId),
    enabled: ready && isAuthenticated && !!reviewId,
  });
  const anyRunning = !!stages.data?.stages.some(
    (s) => s.activity?.running_job_id,
  );

  // Layout-only staleness on an EXISTING bundle takes the cheap path: just the
  // manifest step (seconds), not the full export. A missing bundle or newer
  // data always takes the full stage.
  const layoutOnly =
    dash.data?.exists === true && dash.data?.stale_scope === "layout";

  const build = useMutation({
    mutationFn: () =>
      layoutOnly
        ? runStep(reviewId, "16_generate_dashboard")
        : runStage(reviewId, "view-dashboard"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
      qc.invalidateQueries({ queryKey: ["review-jobs", reviewId] });
    },
    // A 409 (another job won the race) is normal — the 3s poll shows the truth.
    onError: () => undefined,
  });
  const { mutate } = build;

  const fired = useRef(false);
  const needsBuild =
    dash.isSuccess && (dash.data.exists === false || dash.data.stale === true);
  const shouldFire =
    needsBuild &&
    stage.gate?.state !== "blocked" &&
    !stage.activity?.running_job_id &&
    !anyRunning &&
    stage.activity?.last_job_state !== "failed";

  useEffect(() => {
    // Bundle fresh again → this episode is over; arm for the next one.
    if (dash.isSuccess && !needsBuild) fired.current = false;
    if (!shouldFire || fired.current) return;
    fired.current = true; // before mutate: survives strict-mode double-invoke
    mutate();
  }, [dash.isSuccess, needsBuild, shouldFire, mutate]);

  // "Building" bridges three windows: the POST in flight, the gap between the
  // POST succeeding and the 3s stages poll first showing running_job_id, and
  // the run itself. The middle one is `isSuccess && still no bundle && the last
  // job didn't fail` — without it the UI flashes "no dashboard" for a poll tick.
  const building =
    build.isPending ||
    !!stage.activity?.running_job_id ||
    (build.isSuccess &&
      dash.data?.exists === false &&
      stage.activity?.last_job_state !== "failed");

  return {
    building,
    rebuild: () => mutate(),
    anyRunning,
    layoutOnly,
  };
}
