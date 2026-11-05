/* ── Agent: the review collaborator ───────────────────────────────────────── */

import { request } from "./client";
import type { AgentMessageOut } from "../types";

export function getAgentMessages(
  reviewId: string,
  stage?: string,
): Promise<AgentMessageOut[]> {
  const q = stage ? `?stage=${encodeURIComponent(stage)}` : "";
  return request<AgentMessageOut[]>(`/reviews/${reviewId}/agent/messages${q}`);
}

/** One chat turn. Slow (a real LLM call, up to ~2 min) — callers must render a
 *  thinking state and treat a 409 as "still answering the previous message". */
export function agentAsk(
  reviewId: string,
  stage: string,
  message: string,
): Promise<AgentMessageOut> {
  return request<AgentMessageOut>(`/reviews/${reviewId}/agent/ask`, {
    method: "POST",
    body: { stage, message },
  });
}

/** The post-run sanity check. Idempotent per job_id server-side; the client
 *  still guards against firing it twice (see AgentRail), since each first
 *  call for a job is a paid LLM call. */
export function agentReview(
  reviewId: string,
  stage: string,
  jobId: string,
): Promise<AgentMessageOut> {
  return request<AgentMessageOut>(`/reviews/${reviewId}/agent/review`, {
    method: "POST",
    body: { stage, job_id: jobId },
  });
}

export function agentDismiss(
  reviewId: string,
  stage: string,
  code: string,
  reason = "",
): Promise<AgentMessageOut> {
  return request<AgentMessageOut>(`/reviews/${reviewId}/agent/dismiss`, {
    method: "POST",
    body: { stage, code, reason },
  });
}
