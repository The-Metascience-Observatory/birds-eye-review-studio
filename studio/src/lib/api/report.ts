/* ── The AI narrative review (report stage) ───────────────────────────────── */

import { request } from "./client";
import type { JobOut, ReviewDocumentOut, SynthesizeIn } from "../types";

/** Start the AI narrative-review synthesis as a background LLM job (kind
 *  "synthesis"). NOT exempt from the one-job guard — 409s while a pipeline run
 *  is active (and vice versa). Body options override the config's `synthesis`
 *  block for this run. Spends LLM money: only call on an explicit click. */
export function synthesizeReview(
  reviewId: string,
  body: SynthesizeIn,
): Promise<JobOut> {
  return request<JobOut>(`/reviews/${reviewId}/synthesize-review`, {
    method: "POST",
    body,
  });
}

/** The synthesized narrative document + metadata; `exists: false` when never
 *  generated (a 200, so the report stage renders its empty state plainly). */
export function getReviewDocument(reviewId: string): Promise<ReviewDocumentOut> {
  return request<ReviewDocumentOut>(`/reviews/${reviewId}/review-document`);
}
