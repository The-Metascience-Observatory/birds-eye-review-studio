/* ── Dashboard preview ─────────────────────────────────────────────────────
 * The pipeline builds a bundle into the review's workspace; these read it.
 * Aggregates arrive precomputed — the studio never re-derives a count, or it
 * could disagree with the published dashboard. */

import { request } from "./client";
import type { MetaAnalysisOut } from "../types";
import type {
  DashboardManifest,
  DashboardSummary,
  Feasibility,
  PrismaData,
  TrialRow,
} from "@bird/dashboard-ui";

export interface DashboardState {
  exists: boolean;
  stale: boolean;
  reason: string | null;
  /** Why it's stale: "data" needs the full export re-run; "layout" (the
   *  dashboard editor changed config after the last build) only needs the
   *  seconds-long manifest step. */
  stale_scope?: "data" | "layout" | null;
  built_at: string | null;
  n_trials: number | null;
  mode?: "inline" | "paged";
}

export function getDashboardState(reviewId: string): Promise<DashboardState> {
  return request<DashboardState>(`/reviews/${reviewId}/dashboard/state`);
}

export function getDashboardSummary(reviewId: string): Promise<{
  state: DashboardState;
  summary: DashboardSummary;
  manifest: DashboardManifest | null;
}> {
  return request(`/reviews/${reviewId}/dashboard/summary`);
}

/** Rows per `/dashboard/rows` request. Matches the server's default page. */
const ROWS_PAGE = 5000;
/** Backstop against a server that never returns a short page. 100 pages is
 *  500k trials — far past any corpus, and the loop normally ends long before. */
const MAX_ROW_PAGES = 100;

/**
 * Every canonical per-trial row of the built dashboard.
 *
 * The endpoint is paged (offset/limit; default limit 5000). A single call used
 * to silently truncate a large corpus at that limit, so every filter and count
 * on the client would quietly disagree with the published dashboard. This
 * walks the pages until a short one says the file is exhausted.
 */
export async function getDashboardRows(
  reviewId: string,
): Promise<{ rows: TrialRow[]; offset: number; count: number }> {
  const rows: TrialRow[] = [];
  let offset = 0;
  for (let page = 0; page < MAX_ROW_PAGES; page++) {
    const res = await request<{ rows: TrialRow[]; offset: number; count: number }>(
      `/reviews/${reviewId}/dashboard/rows?offset=${offset}&limit=${ROWS_PAGE}`,
    );
    rows.push(...res.rows);
    if (res.count < ROWS_PAGE) break;
    offset += res.count;
  }
  return { rows, offset: 0, count: rows.length };
}

/** The PRISMA screening funnel, from live artifacts — the top of the funnel
 *  appears as soon as search runs, filling in through screening. */
export function getPrisma(reviewId: string): Promise<PrismaData> {
  return request<PrismaData>(`/reviews/${reviewId}/prisma`);
}

/** The pooled meta-analysis (forest-plot groups) from the built bundle. Empty
 *  groups for corpora with no pooling. */
export function getMetaAnalysis(reviewId: string): Promise<MetaAnalysisOut> {
  return request<MetaAnalysisOut>(`/reviews/${reviewId}/meta-analysis`);
}

/** Meta-analysis feasibility, computed live from the pass-2 records. Returns
 *  `assess_poolability`'s dict, which is exactly the FeasibilityCard's shape. */
export function getFeasibility(reviewId: string): Promise<Feasibility> {
  return request<Feasibility>(`/reviews/${reviewId}/feasibility`);
}
