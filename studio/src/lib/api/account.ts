/** Sign-up / sign-in, the signed-in user, their AI-provider settings, and the
 *  shared Claude CLI lease. */

import { request } from "./client";
import type {
  LlmSessionOut,
  RegisterOut,
  TokenOut,
  UserOut,
  UserSettingsIn,
  UserSettingsOut,
} from "../types";

/** Create an account. The response includes an access token — registering logs
 *  you in, so callers should NOT follow this with a separate login() call. */
export function register(
  email: string,
  password: string,
  inviteCode?: string,
): Promise<RegisterOut> {
  // Send invite_code only when filled: the field is optional server-side, and
  // omitting it keeps open-signup instances byte-identical to the old request.
  const body: Record<string, string> = { email, password };
  const code = inviteCode?.trim();
  if (code) body.invite_code = code;
  return request<RegisterOut>("/auth/register", {
    method: "POST",
    body,
    auth: false,
  });
}

export function login(email: string, password: string): Promise<TokenOut> {
  return request<TokenOut>("/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export function me(): Promise<UserOut> {
  return request<UserOut>("/auth/me");
}

/** The user's AI-provider settings. Never includes the raw key — only whether
 *  one is stored (`has_openrouter_key`). */
export function getUserSettings(): Promise<UserSettingsOut> {
  return request<UserSettingsOut>("/auth/settings");
}

/** Update provider and/or the OpenRouter key. `openrouter_key`: omit = unchanged,
 *  "" = clear, non-empty = set. */
export function updateUserSettings(
  patch: UserSettingsIn,
): Promise<UserSettingsOut> {
  return request<UserSettingsOut>("/auth/settings", {
    method: "PATCH",
    body: patch,
  });
}

// --- Shared Claude CLI login (single-occupancy lease) ---

/** Who currently holds the host's shared `claude` CLI login. */
export function getLlmSession(): Promise<LlmSessionOut> {
  return request<LlmSessionOut>("/llm/session");
}

/** Claim the CLI for this user. 409 while someone else holds a live lease;
 *  `force` takes over a lease that has gone idle past the TTL. */
export function claimLlmSession(force = false): Promise<LlmSessionOut> {
  return request<LlmSessionOut>("/llm/session/claim", {
    method: "POST",
    body: { force },
  });
}

/** Give up the lease. Idempotent. */
export function releaseLlmSession(): Promise<LlmSessionOut> {
  return request<LlmSessionOut>("/llm/session/release", { method: "POST" });
}
