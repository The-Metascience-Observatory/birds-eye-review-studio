/** Shared formatting utilities for Bird's Eye Review dashboards.
 *
 *  Seeded from the (identical) per-topic `utils.ts` on the public website. Pure
 *  functions — no DOM, no styling — so they are safe to share everywhere. */

/** Format a number for a forest plot / results cell: 0 decimals >= 100, 1 dp
 *  >= 10, else 2 dp; em-dash for null / non-finite. */
export function fmt(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  return a >= 100 ? n.toFixed(0) : a >= 10 ? n.toFixed(1) : n.toFixed(2);
}

/** snake_case / lower text -> Title Case, with "RCT" special-cased. */
export function formatLabel(s: string): string {
  return s.replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bRct\b/g, "RCT");
}
