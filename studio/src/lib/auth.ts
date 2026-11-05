"use client";

import { useCallback, useEffect, useState } from "react";

/** localStorage-backed JWT storage + a tiny client-side auth hook. The token is
 *  the only source of truth on the client; the server validates it per request. */

const TOKEN_KEY = "studio.token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
  window.dispatchEvent(new Event("studio-auth-change"));
}

/** Forget the session token. `silent` skips the auth-change event — used when
 *  a full-page redirect to /login is about to happen anyway, so the in-page
 *  guards don't race it with their own (param-less) client-side redirect. */
export function clearToken(opts: { silent?: boolean } = {}): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  if (!opts.silent) window.dispatchEvent(new Event("studio-auth-change"));
}

/** Pages a signed-out visitor may sit on. A 401 there just drops the dead
 *  token; anywhere else it sends the user to /login. */
const PUBLIC_PATHS = new Set(["/", "/login", "/signup"]);

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname.replace(/\/+$/, "") || "/");
}

/** The /login URL that returns the user to `path` (pathname + search) after
 *  signing in. */
export function loginUrlFor(path: string): string {
  return isPublicPath(path.split("?")[0])
    ? "/login"
    : `/login?next=${encodeURIComponent(path)}`;
}

/** /login with a `next` param pointing back at the page the browser is on —
 *  what a signed-in-only page's guard redirects to. */
export function loginUrlForHere(): string {
  if (typeof window === "undefined") return "/login";
  return loginUrlFor(window.location.pathname + window.location.search);
}

/** The post-login destination from the current URL's `next` param, or
 *  `fallback`. Only same-origin PATHS are honoured ("/reviews/x") — never
 *  "//host", "/\host" or "https://…" — so the param can't become an open
 *  redirect. */
export function nextPathFromLocation(fallback = "/dashboard"): string {
  if (typeof window === "undefined") return fallback;
  const raw = new URLSearchParams(window.location.search).get("next");
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) {
    return fallback;
  }
  return isPublicPath(raw.split("?")[0]) ? fallback : raw;
}

export interface UseAuth {
  token: string | null;
  isAuthenticated: boolean;
  ready: boolean;
  signOut: () => void;
}

export function useAuth(): UseAuth {
  // Seed from localStorage via a LAZY INITIALIZER, so the very first client
  // render already knows the token. Starting at null and filling it in from an
  // effect made freshly-authenticated navigations flash as logged-out: a guard
  // reading `ready && !isAuthenticated` could fire before the token was seen and
  // bounce a just-signed-up user to /login. getToken() is SSR-safe (returns null
  // when there's no window), so the server render stays unauthenticated and
  // hydration matches.
  const [token, setTok] = useState<string | null>(() => getToken());
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const sync = () => setTok(getToken());
    // Re-sync on mount: during SSR/hydration the initializer saw no window, and
    // the token may also have changed between render and effect.
    sync();
    setReady(true);
    window.addEventListener("studio-auth-change", sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener("studio-auth-change", sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const signOut = useCallback(() => clearToken(), []);

  return { token, isAuthenticated: !!token, ready, signOut };
}
