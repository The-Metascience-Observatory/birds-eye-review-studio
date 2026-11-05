/* ── Publishing: owner request → admin approve/reject ─────────────────────── */

import { request } from "./client";
import type {
  PublishedDashboardOut,
  PublishPreflightOut,
  PublishRequestOut,
} from "../types";

/** The evidence packet for a publish decision (writes nothing). */
export function getPublishPreflight(
  reviewId: string,
  route: string,
): Promise<PublishPreflightOut> {
  return request<PublishPreflightOut>(
    `/reviews/${reviewId}/publish/preflight?route=${encodeURIComponent(route)}`,
  );
}

/** An owner proposes publishing this review to a public route. */
export function requestPublish(
  reviewId: string,
  route: string,
): Promise<PublishRequestOut> {
  return request<PublishRequestOut>(`/reviews/${reviewId}/publish-request`, {
    method: "POST",
    body: { route },
  });
}

/** Admin: the moderation queue. `state` defaults to "pending" server-side. */
export function getPublishRequests(
  state = "pending",
): Promise<PublishRequestOut[]> {
  return request<PublishRequestOut[]>(
    `/admin/publish-requests?state=${encodeURIComponent(state)}`,
  );
}

/** Admin: everything currently live (incl. grandfathered CLI dashboards). */
export function getPublished(): Promise<PublishedDashboardOut[]> {
  return request<PublishedDashboardOut[]>(`/admin/published`);
}

/** Admin: approve a request — runs the atomic deploy. `confirm_token` from
 *  preflight; a rebuild since then makes the server refuse with 409. */
export function approvePublishRequest(
  requestId: string,
  confirmToken: string,
  opts: { route?: string; allow_shrink?: boolean } = {},
): Promise<PublishedDashboardOut> {
  return request<PublishedDashboardOut>(
    `/admin/publish-requests/${requestId}/approve`,
    { method: "POST", body: { confirm_token: confirmToken, ...opts } },
  );
}

/** Admin: reject a request with a reason. */
export function rejectPublishRequest(
  requestId: string,
  reason: string,
): Promise<PublishRequestOut> {
  return request<PublishRequestOut>(
    `/admin/publish-requests/${requestId}/reject`,
    { method: "POST", body: { reason } },
  );
}
