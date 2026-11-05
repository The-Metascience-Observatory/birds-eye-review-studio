/* ── Access codes (admin) ─────────────────────────────────────────────────
 * Who may hold an account at all. See server/app/services/invites.py. */

import { request } from "./client";
import type { InviteCreatedOut, InviteCreateIn, InviteOut } from "../types";

/** Admin: every access code ever issued, newest first. Never the codes. */
export function getInvites(): Promise<InviteOut[]> {
  return request<InviteOut[]>(`/admin/invites`);
}

/** Admin: mint one code for one person.
 *
 * The `code` in the response is the ONLY copy — the server stores just its
 * hash, so it cannot be shown again. The caller must surface it immediately.
 */
export function createInvite(payload: InviteCreateIn): Promise<InviteCreatedOut> {
  return request<InviteCreatedOut>(`/admin/invites`, {
    method: "POST",
    body: payload,
  });
}

/** Admin: withdraw a code, and with it the account that redeemed it. Takes
 *  effect on that user's next request — the server rechecks on every call. */
export function revokeInvite(inviteId: string): Promise<InviteOut> {
  return request<InviteOut>(`/admin/invites/${inviteId}/revoke`, {
    method: "POST",
  });
}
