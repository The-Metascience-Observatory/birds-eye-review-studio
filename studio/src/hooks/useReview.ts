"use client";

/** Shared React-Query hooks for a review's read-only detail data.
 *
 *  These query-key + fetcher pairs were duplicated across pages/components (the
 *  review query in reviews/[id] and ExportStage; the dashboard-state query in
 *  both plus PublishPanel's invalidate). Centralizing them keeps the keys — the
 *  thing invalidations target — in one place, and applies the same
 *  `retry: false` these read-only detail views all want (a missing bundle is a
 *  normal 404, not something to retry). */

import { useQuery } from "@tanstack/react-query";

import { getDashboardState, getReview, getReviewConfig } from "@/lib/api";
import type { ReviewConfigOut } from "@/lib/types";

export function useReview(id: string, enabled = true) {
  return useQuery({
    queryKey: ["review", id],
    queryFn: () => getReview(id),
    enabled: enabled && !!id,
  });
}

export function useDashboardState(id: string, enabled = true) {
  return useQuery({
    queryKey: ["dashboard-state", id],
    queryFn: () => getDashboardState(id),
    enabled: enabled && !!id,
    retry: false,
  });
}

/** A review's config. The `["review-config", id]` key is what config saves
 *  invalidate, so every stage body that edits config reads through this. Each
 *  stage keeps its own save mutation (they build different patches), but the
 *  query — the duplicated part — lives here. */
export function useReviewConfig(id: string) {
  return useQuery<ReviewConfigOut>({
    queryKey: ["review-config", id],
    queryFn: () => getReviewConfig(id),
  });
}
