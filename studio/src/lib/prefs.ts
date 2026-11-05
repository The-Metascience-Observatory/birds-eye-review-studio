"use client";

import { useCallback, useSyncExternalStore } from "react";

/** localStorage-backed UI preferences. Client-only, per-browser — these are
 *  view toggles (like debug mode), not account data, so they don't belong on
 *  the server. Mirrors the auth.ts storage pattern. */

const PREFIX = "ber:pref:";

export interface Prefs {
  /** Show the raw job terminal (log console) on the overview and stage pages.
   *  Off by default: most users want the progress bar, not the log. */
  debugMode: boolean;
  /** Evidence-table column ids the user picked. `null` = "never customized",
   *  which resolves to the table's DEFAULT_COLUMNS — so users who never touch
   *  the picker still receive future changes to the defaults. */
  evidenceColumns: string[] | null;
  /** Open the downloaded PDF pane automatically when the paper detail view
   *  opens (desktop only — narrow viewports have no room for the pane). */
  autoOpenPdf: boolean;
}

const DEFAULTS: Prefs = {
  debugMode: false,
  evidenceColumns: null,
  autoOpenPdf: false,
};

// getSnapshot (below) must return a STABLE reference for unchanged data —
// re-parsing JSON each call would hand useSyncExternalStore a fresh array
// identity every render and loop it. Cache the parsed value keyed on the raw
// string, so it only changes when the stored string does.
const snapshots = new Map<string, { raw: string | null; value: unknown }>();

function read<K extends keyof Prefs>(key: K): Prefs[K] {
  if (typeof window === "undefined") return DEFAULTS[key];
  const raw = window.localStorage.getItem(PREFIX + key);
  const hit = snapshots.get(key);
  if (hit && hit.raw === raw) return hit.value as Prefs[K];
  let value: Prefs[K];
  if (raw === null) {
    value = DEFAULTS[key];
  } else {
    try {
      value = JSON.parse(raw) as Prefs[K];
    } catch {
      value = DEFAULTS[key];
    }
  }
  snapshots.set(key, { raw, value });
  return value;
}

// A tiny store so every component re-renders when a preference changes,
// including across tabs (the `storage` event) and same-tab writes (a custom
// event, since `storage` doesn't fire in the writing tab).
const listeners = new Set<() => void>();
function subscribe(cb: () => void) {
  listeners.add(cb);
  const onStorage = () => cb();
  window.addEventListener("storage", onStorage);
  window.addEventListener("ber:pref-change", onStorage);
  return () => {
    listeners.delete(cb);
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("ber:pref-change", onStorage);
  };
}

export function usePref<K extends keyof Prefs>(
  key: K,
): [Prefs[K], (value: Prefs[K]) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => read(key),
    () => DEFAULTS[key],
  );
  const set = useCallback(
    (v: Prefs[K]) => {
      window.localStorage.setItem(PREFIX + key, JSON.stringify(v));
      window.dispatchEvent(new Event("ber:pref-change"));
    },
    [key],
  );
  return [value, set];
}

// --- Last-visited stage per review -----------------------------------------
// The review's bare URL (/reviews/<id>) redirects to the stage the user last
// left off on, so there is no separate overview page in the walkthrough. Keyed
// per review (unlike Prefs), so it lives beside the prefs rather than in them.
// Non-reactive on purpose: it is only read once, during the redirect.

const LAST_STAGE_PREFIX = "ber:laststage:";

export function rememberStage(reviewId: string, slug: string): void {
  if (typeof window === "undefined" || !reviewId || !slug) return;
  try {
    window.localStorage.setItem(LAST_STAGE_PREFIX + reviewId, slug);
  } catch {
    /* storage full/blocked — losing the bookmark is fine */
  }
}

export function lastStage(reviewId: string): string | null {
  if (typeof window === "undefined" || !reviewId) return null;
  return window.localStorage.getItem(LAST_STAGE_PREFIX + reviewId);
}
