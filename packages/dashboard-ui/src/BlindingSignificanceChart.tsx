"use client";

/** Blinding vs significance — do open-label trials report more positive
 *  results than blinded ones?
 *
 *  One stacked bar per blinding level: significant (p < 0.05 on the primary
 *  outcome) / not significant / no p reported. The unfiltered baseline comes
 *  PRECOMPUTED from `summary.charts.blinding_significance` (the studio never
 *  re-derives a published number); a filtered subset is reduced from the rows
 *  with the same rule the summary used (see dashboard_summary.py).
 */

import { useMemo } from "react";

import { representativeStudies, type DashboardSummary, type TrialRow } from "./summary";
import { formatLabel } from "./utils";

interface Bin {
  blinding: string;
  significant: number;
  not_significant: number;
  unknown: number;
}

function binsFrom(summary: DashboardSummary, subset: TrialRow[] | null): Bin[] {
  if (!subset) return summary.charts.blinding_significance ?? [];
  const order = summary.vocab.blinding_order ?? [];
  return order
    .filter((b) => subset.some((r) => r.blinding === b))
    .map((b) => {
      const sub = representativeStudies(subset).filter((r) => r.blinding === b);
      const sig = sub.filter(
        (r) => r.result_details?.selected.some(s => s.significance === "significant"),
      ).length;
      const nsig = sub.filter(
        (r) => !!r.result_details?.selected.length && r.result_details.selected.every(s => s.significance === "not_detected"),
      ).length;
      return {
        blinding: b,
        significant: sig,
        not_significant: nsig,
        unknown: sub.length - sig - nsig,
      };
    });
}

const SEGMENTS = [
  { key: "significant" as const, label: "Statistically significant", cls: "bg-green-500/70" },
  { key: "not_significant" as const, label: "Not significant", cls: "bg-foreground/25" },
  { key: "unknown" as const, label: "Insufficient extracted data", cls: "bg-foreground/10" },
];

export function BlindingSignificanceChart({
  summary,
  subset,
}: {
  summary: DashboardSummary;
  subset: TrialRow[] | null;
}) {
  const bins = useMemo(() => binsFrom(summary, subset), [summary, subset]);
  const nonEmpty = bins.filter(
    (b) => b.significant + b.not_significant + b.unknown > 0,
  );

  return (
    <div className="border border-border rounded-lg bg-card p-3 sm:p-4 mb-6">
      <h2 className="text-lg font-semibold">Blinding vs significance</h2>
      <p className="mb-3 text-xs text-foreground/55">
        Share of studies with a statistically significant primary comparison, by blinding level. This descriptive pattern alone does not establish bias.
      </p>
      {nonEmpty.length === 0 ? (
        <p className="py-4 text-center text-sm text-foreground/45">
          No blinding data for these trials.
        </p>
      ) : (
        <div className="space-y-2">
          {nonEmpty.map((b) => {
            const total = b.significant + b.not_significant + b.unknown;
            const pctSig = Math.round((100 * b.significant) / total);
            return (
              <div key={b.blinding}>
                <div className="mb-0.5 flex items-baseline justify-between text-xs">
                  <span className="font-medium text-foreground/75">
                    {formatLabel(b.blinding)}
                  </span>
                  <span className="tabular-nums text-foreground/55">
                    {b.significant}/{total} significant ({pctSig}%)
                  </span>
                </div>
                <div className="flex h-3 w-full overflow-hidden rounded bg-foreground/[0.04]">
                  {SEGMENTS.map((s) =>
                    b[s.key] > 0 ? (
                      <div
                        key={s.key}
                        className={s.cls}
                        style={{ width: `${(100 * b[s.key]) / total}%` }}
                        title={`${s.label}: ${b[s.key]}`}
                      />
                    ) : null,
                  )}
                </div>
              </div>
            );
          })}
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-foreground/55">
            {SEGMENTS.map((s) => (
              <span key={s.key} className="inline-flex items-center gap-1.5">
                <span className={`h-2.5 w-2.5 rounded-sm ${s.cls}`} />
                {s.label}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
