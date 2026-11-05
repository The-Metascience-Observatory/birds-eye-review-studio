"use client";

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

/** Vertical "Trials by Year" histogram, zero-filled across min..max so a gap
 *  year reads as a trough rather than a missing bar that compresses the axis.
 *
 *  PREFER `data`: the pipeline already computes and zero-fills this histogram
 *  into `summary.charts.by_year`, and passing it through means the studio and a
 *  published dashboard cannot disagree about it. `years` re-derives the bins and
 *  exists only for the filtered path (where no precomputed answer can exist) and
 *  for the older website callers. */
export function YearChart({
  years,
  data: precomputed,
  nMissing = 0,
}: {
  years?: number[];
  data?: { year: number; count: number }[];
  /** Trials with no publication year: excluded from the chart, stated below it
   *  rather than silently dropped. */
  nMissing?: number;
}) {
  let data: { year: number; count: number }[];
  if (precomputed) {
    data = precomputed;
  } else {
    const valid = (years ?? []).filter(
      (y) => Number.isFinite(y) && y > 1900 && y < 2100,
    );
    const counts = new Map<number, number>();
    for (const y of valid) counts.set(y, (counts.get(y) ?? 0) + 1);
    data = [];
    if (counts.size) {
      const ks = [...counts.keys()];
      for (let y = Math.min(...ks); y <= Math.max(...ks); y++) {
        data.push({ year: y, count: counts.get(y) ?? 0 });
      }
    }
  }
  const total = data.reduce((a, d) => a + d.count, 0);
  if (!data.length) {
    return (
      <div className="border border-border rounded-lg bg-card p-3 sm:p-4 mb-6">
        <h2 className="text-lg font-semibold">Trials by Year</h2>
        <p className="py-6 text-center text-sm text-foreground/45">
          No publication years available for these trials.
        </p>
      </div>
    );
  }

  // Thin the x-axis labels to ~14 regardless of span.
  const interval = Math.max(0, Math.ceil(data.length / 14) - 1);

  return (
    <div className="border border-border rounded-lg bg-card p-3 sm:p-4 mb-6">
      <h2 className="text-lg font-semibold mb-3">
        Trials by Year (n = {total.toLocaleString()})
      </h2>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={data} margin={{ left: 0, right: 10, top: 5, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} vertical={false} />
          <XAxis
            dataKey="year"
            type="category"
            interval={interval}
            fontSize={12}
            tickLine={false}
          />
          <YAxis allowDecimals={false} fontSize={12} width={32} />
          <Tooltip
            formatter={(v: number) => [v.toLocaleString(), "Trials"]}
            labelFormatter={(l) => `Year ${l}`}
            cursor={{ fill: "rgba(0,0,0,0.04)" }}
          />
          <Bar dataKey="count" fill="hsl(var(--primary))" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
      {nMissing > 0 && (
        <p className="mt-2 text-xs text-foreground/50">
          {nMissing.toLocaleString()} trial{nMissing === 1 ? "" : "s"} with no
          publication year {nMissing === 1 ? "is" : "are"} not shown.
        </p>
      )}
    </div>
  );
}
