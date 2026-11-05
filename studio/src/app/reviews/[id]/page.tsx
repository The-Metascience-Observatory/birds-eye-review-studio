"use client";

/** The review's bare URL: a redirect, not a page.
 *
 *  There is no overview page in the walkthrough anymore — the timeline
 *  (PhaseStepper) on every stage page is the navigation. Landing here sends
 *  the user to the stage they last left off on (remembered per review in
 *  localStorage), falling back to the wizard's current stage — which for a
 *  fresh review is the first Define stage. The route itself must survive:
 *  old links, bookmarks, and the reviews list all point at it.
 */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";

import { getStages } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { lastStage } from "@/lib/prefs";
import type { StagesOut } from "@/lib/types";

export default function ReviewEntryRedirect() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();
  const enabled = ready && isAuthenticated && !!id;

  const stages = useQuery<StagesOut>({
    queryKey: ["stages", id],
    queryFn: () => getStages(id),
    enabled,
  });

  useEffect(() => {
    if (!stages.data) return;
    const real = stages.data.stages.filter(
      (s) => s.slug !== "_other" && !s.hidden && !s.skipped,
    );
    if (real.length === 0) return;
    // Last-visited wins, but only if that stage still exists for this review
    // (stages can be skipped by a config change after the bookmark was set).
    const remembered = lastStage(id);
    const target =
      (remembered && real.some((s) => s.slug === remembered) && remembered) ||
      stages.data.current_stage ||
      real[0].slug;
    router.replace(`/reviews/${id}/stages/${target}`);
  }, [stages.data, id, router]);

  return (
    <main className="mx-auto w-4/5 max-w-none animate-pulse px-4 py-10">
      <div className="mb-4 h-8 w-96 rounded bg-muted" />
      <div className="h-96 rounded-lg border border-border bg-muted" />
    </main>
  );
}
