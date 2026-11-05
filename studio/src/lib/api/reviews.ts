/** Reviews, their config, and the wizard: stages, run checks, prompts, AI
 *  drafting, and stage run / approve. */

import { request } from "./client";
import type {
  ClassifySummaryOut,
  ConfigPatch,
  JobOut,
  PreflightOut,
  PregatePreviewOut,
  PromptOut,
  ReviewConfigOut,
  ReviewCreate,
  ReviewOut,
  SearchDraftOut,
  StagesOut,
  TaxonomyDraftOut,
} from "../types";

export function listReviews(): Promise<ReviewOut[]> {
  return request<ReviewOut[]>("/reviews");
}

export function createReview(input: ReviewCreate): Promise<ReviewOut> {
  return request<ReviewOut>("/reviews", { method: "POST", body: input });
}

export function getReview(id: string): Promise<ReviewOut> {
  return request<ReviewOut>(`/reviews/${id}`);
}

export function getReviewConfig(id: string): Promise<ReviewConfigOut> {
  return request<ReviewConfigOut>(`/reviews/${id}/config`);
}

/** Merge a partial config into the stored one.
 *
 * The merge happens server-side against the current row, so an editor that
 * knows about one key cannot drop another (and cannot revert a concurrent
 * change it never fetched). A null value deletes a key (so the default applies
 * again); nested dicts merge one level deep.
 */
export function patchReviewConfig(
  id: string,
  partial: ConfigPatch,
): Promise<ReviewConfigOut> {
  return request<ReviewConfigOut>(`/reviews/${id}/config`, {
    method: "PATCH",
    body: { config_json: partial },
  });
}

/** The wizard stage list, resolved for this review (conditions + steps applied). */
export function getStages(id: string): Promise<StagesOut> {
  return request<StagesOut>(`/reviews/${id}/stages`);
}

/** Categorical counts from the classify stage's CSVs (empty when not run yet). */
export function getClassifySummary(id: string): Promise<ClassifySummaryOut> {
  return request<ClassifySummaryOut>(`/reviews/${id}/classify-summary`);
}

/** Config problems to fix before running (errors should block it).
 *
 *  Pass `stage` to see only the warnings that stage's scripts can act on —
 *  otherwise every page shows every warning, which buries the urgent ones. */
export function getPreflight(
  id: string,
  stage?: string,
): Promise<PreflightOut> {
  const q = stage ? `?stage=${encodeURIComponent(stage)}` : "";
  return request<PreflightOut>(`/reviews/${id}/preflight${q}`);
}

/** How many papers the keyword pre-gate would skip — no LLM calls, no cost. */
export function getPregatePreview(id: string): Promise<PregatePreviewOut> {
  return request<PregatePreviewOut>(`/reviews/${id}/pregate-preview`);
}

/** Editable classification prompts (this review's overrides, else the defaults). */
export function getPrompts(id: string): Promise<PromptOut[]> {
  return request<PromptOut[]>(`/reviews/${id}/prompts`);
}

/** Save a prompt override; empty text reverts to the shipped default. */
export function savePrompt(
  id: string,
  name: string,
  text: string | null,
): Promise<PromptOut> {
  return request<PromptOut>(`/reviews/${id}/prompts/${name}`, {
    method: "PUT",
    body: { text },
  });
}

/** Draft per-source search queries with the LLM (may take ~10-30s). */
export function draftSearchQueries(id: string): Promise<SearchDraftOut> {
  return request<SearchDraftOut>(`/reviews/${id}/search/draft`, {
    method: "POST",
    body: {},
  });
}

/** AI-draft the extraction vocabularies (intervention_categories +
 *  outcome_domains) from the topic and save them into config. ~10-30s. */
export function draftTaxonomies(id: string): Promise<TaxonomyDraftOut> {
  return request<TaxonomyDraftOut>(`/reviews/${id}/taxonomies/draft`, {
    method: "POST",
    body: {},
  });
}

/* ── Wizard: stage run + approval ─────────────────────────────────────────── */

/** Run one stage's canonical steps. Selects by step ID, never by number. */
export function runStage(reviewId: string, slug: string): Promise<JobOut> {
  return request<JobOut>(`/reviews/${reviewId}/stages/${slug}/run`, {
    method: "POST",
  });
}

/** Run a single canonical pipeline step. Used for the cheap layout-only
 *  dashboard rebuild ("16_generate_dashboard" rewrites just the manifest). */
export function runStep(reviewId: string, stepId: string): Promise<JobOut> {
  return request<JobOut>(`/reviews/${reviewId}/steps/${stepId}/run`, {
    method: "POST",
  });
}

/** Record a sign-off. Returns the whole stage list with a recomputed
 *  `current_stage`, so the caller can seed the cache and navigate without a
 *  second round-trip — and without its idea of "next" disagreeing with the
 *  server's. */
export function approveStage(
  reviewId: string,
  slug: string,
  notes?: string,
): Promise<StagesOut> {
  return request<StagesOut>(`/reviews/${reviewId}/stages/${slug}/approve`, {
    method: "POST",
    body: { notes: notes ?? null },
  });
}

export function unapproveStage(
  reviewId: string,
  slug: string,
): Promise<StagesOut> {
  return request<StagesOut>(`/reviews/${reviewId}/stages/${slug}/unapprove`, {
    method: "POST",
  });
}
