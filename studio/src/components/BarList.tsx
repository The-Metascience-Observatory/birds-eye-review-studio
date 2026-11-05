"use client";

// Classify labels are LLM free-text with a long tail of one-off variants, so
// each bar group shows the top categories and folds the rest into one bar.
const MAX_BARS = 10;

/** One titled group of horizontal bars: label · bar · count, sorted by count. */
export function BarList({
  title,
  data,
}: {
  title: string;
  data: Record<string, number>;
}) {
  let entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return null;
  if (entries.length > MAX_BARS) {
    const tail = entries.slice(MAX_BARS - 1);
    entries = entries.slice(0, MAX_BARS - 1);
    entries.push([
      `other (${tail.length} categories)`,
      tail.reduce((n, [, v]) => n + v, 0),
    ]);
    entries.sort((a, b) => b[1] - a[1]);
  }
  const max = Math.max(...entries.map(([, n]) => n));
  return (
    <div>
      <div className="text-xs font-medium">{title}</div>
      <ul className="mt-1.5 space-y-1">
        {entries.map(([label, n]) => (
          <li key={label} className="flex items-center gap-2 text-xs">
            <span
              title={label}
              className="w-28 shrink-0 truncate text-muted-foreground"
            >
              {label}
            </span>
            <span className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
              <span
                className="block h-full rounded-full bg-primary"
                style={{ width: `${Math.max(2, Math.round((n / max) * 100))}%` }}
              />
            </span>
            <span className="w-12 shrink-0 text-right tabular-nums text-foreground/80">
              {n.toLocaleString()}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
