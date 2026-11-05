/** The one HTTP primitive every API module goes through.
 *
 *  Requests go to the same-origin `/api` path, which next.config.mjs rewrites
 *  to the API (see API_BASE below); the Bearer token is pulled from
 *  localStorage on each call (client-side only). JSON calls use `request`;
 *  multipart uploads, blob downloads and the SSE log stream use `authFetch`
 *  directly — all of them share its auth header, error parsing and 401
 *  handling, so none can drift from the others. */

import { clearToken, getToken, isPublicPath, loginUrlFor } from "../auth";

/**
 * Where API calls go.
 *
 * Default is the SAME-ORIGIN path `/api`, which `next.config.mjs` rewrites to
 * the FastAPI server. That replaced a build-time-inlined absolute URL: the old
 * value was baked into the browser bundle, so it went stale whenever the host's
 * LAN IP changed, could not be reached at all by anyone outside that LAN, and
 * was blocked as mixed content inside an HTTPS page. Same-origin also removes
 * CORS from the picture entirely and lets the API bind loopback.
 *
 * NEXT_PUBLIC_API_BASE remains as an explicit escape hatch for pointing the app
 * at an API somewhere else. Setting it opts back into cross-origin requests, so
 * that origin must then appear in the server's CORS_ORIGINS.
 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Pull a human-readable message out of an error response.
 *
 *  FastAPI is not consistent here and the difference is user-visible:
 *  - `HTTPException(status, "msg")` -> `{"detail": "msg"}` (a string)
 *  - request-validation failures    -> `{"detail": [{loc, msg, type}, …]}`
 *
 *  Only handling the string case meant every 422 surfaced as the useless
 *  "Request failed (422)" while the server's actual complaint was discarded.
 *  Field names are included because "Input should be a valid string" alone
 *  doesn't tell anyone WHICH field is wrong.
 */
async function extractErrorDetail(res: Response): Promise<string> {
  const fallback = `Request failed (${res.status})`;
  let data: unknown;
  try {
    // Keep the original body readable if JSON parsing fails.
    data = await res.clone().json();
  } catch {
    // Non-JSON body (a proxy's HTML error page, an empty 502, …).
    try {
      const text = (await res.text()).trim();
      return text ? `${fallback}: ${text.slice(0, 300)}` : fallback;
    } catch {
      return fallback;
    }
  }

  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;

  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        const item = d as { loc?: unknown[]; msg?: string } | null;
        if (!item?.msg) return null;
        // loc looks like ["body", "field", 0] — drop the "body"/"query" prefix.
        const field = Array.isArray(item.loc)
          ? item.loc.filter((p) => p !== "body" && p !== "query").join(".")
          : "";
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }

  // Some handlers return {"message": …} or a bare string body.
  const message = (data as { message?: unknown } | null)?.message;
  if (typeof message === "string" && message.trim()) return message;
  if (typeof data === "string" && data.trim()) return data;

  return fallback;
}

/** Latched once the first 401 starts the redirect, so a burst of parallel
 *  queries failing together navigates exactly once. */
let redirectingToLogin = false;

/** The server rejected our token (expired, revoked, signing key rotated). A
 *  stored-but-dead token used to leave every page in a permanent error state,
 *  because `useAuth` treats any stored token as valid. Drop it, and — unless
 *  the user is already on a page a signed-out visitor may see — send them to
 *  /login with a `next` param that brings them back afterwards. */
function handleUnauthorized(): void {
  if (typeof window === "undefined") return;
  const { pathname, search } = window.location;
  if (isPublicPath(pathname)) {
    // Never loop on /login itself; just let the UI notice the token is gone.
    clearToken();
    return;
  }
  if (redirectingToLogin) return;
  redirectingToLogin = true;
  // Silent: a full navigation follows, so the in-page guards must not race it
  // with their own client-side redirect (which would drop the `next` param).
  clearToken({ silent: true });
  window.location.replace(loginUrlFor(pathname + search));
}

export interface AuthFetchOptions extends Omit<RequestInit, "headers"> {
  headers?: Record<string, string>;
  /** Attach the Bearer token (default true). */
  auth?: boolean;
  /** Message prefix for a non-2xx response; the server's detail follows. When
   *  omitted the server's detail is used as-is. */
  errorPrefix?: string;
}

/**
 * fetch() against the API with the session token attached.
 *
 * Throws `ApiError(0, …)` when the API can't be reached at all, and
 * `ApiError(status, detail)` for any non-2xx response — except where the
 * caller lists the status in `allow`, in which case the raw Response comes
 * back for it to interpret (e.g. a 404 meaning "no PDF yet").
 */
export async function authFetch(
  path: string,
  opts: AuthFetchOptions = {},
  allow: readonly number[] = [],
): Promise<Response> {
  const { auth = true, errorPrefix, headers: extra, ...init } = opts;
  const headers: Record<string, string> = { ...extra };
  const token = auth ? getToken() : null;
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { cache: "no-store", ...init, headers });
  } catch (err) {
    // A caller's own abort is not a connectivity problem — let it through.
    if ((err as Error)?.name === "AbortError") throw err;
    // fetch rejects before any HTTP status on network/CORS failures. With the
    // default same-origin `/api` there is no CORS involved, so the only
    // realistic cause is the API being down — pointing the reader at
    // CORS_ORIGINS there would send them after a problem that cannot exist.
    // Mention it only when someone has explicitly opted into a cross-origin
    // base, which is the one case where it can still bite.
    const origin =
      typeof window !== "undefined" ? window.location.origin : "unknown";
    const crossOrigin = /^https?:\/\//.test(API_BASE);
    throw new ApiError(
      0,
      crossOrigin
        ? `Cannot reach the API at ${API_BASE} — is the server running, and is ` +
            `this page's origin (${origin}) in its CORS_ORIGINS?`
        : `Cannot reach the API at ${API_BASE} — is the server running?`,
    );
  }

  if (res.ok || allow.includes(res.status)) return res;

  // Only a request that actually SENT a token can prove the token is dead; a
  // 401 from /auth/login (wrong password) must stay an ordinary error.
  if (res.status === 401 && token) handleUnauthorized();

  const detail = await extractErrorDetail(res);
  throw new ApiError(res.status, errorPrefix ? `${errorPrefix}: ${detail}` : detail);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  auth?: boolean;
  signal?: AbortSignal;
}

/** A JSON request/response round-trip. 204 resolves to undefined. */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true, signal } = opts;
  const res = await authFetch(path, {
    method,
    auth,
    signal,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** `?doi=…` — the evidence endpoints key one paper by its DOI as a query param. */
export function doiQuery(doi: string): string {
  return `?doi=${encodeURIComponent(doi)}`;
}
