"use client";

import type { ReactNode } from "react";

/** A cross-filtered checkbox facet card.
 *
 *  The counts shown here are computed against the rows passing every OTHER
 *  card's filters — never this card's own. That is what makes the numbers
 *  useful: "Germany (4)" means "ticking Germany leaves 4", not "Germany has 4
 *  somewhere in the corpus". Ticking an option whose count is 0 would be a dead
 *  end, so those are dimmed rather than hidden (hiding them makes options
 *  flicker in and out as you filter, which is worse).
 *
 *  Selection semantics match the website's: an EMPTY set means "no constraint"
 *  and renders as all-checked. Unchecking from that state seeds the set with
 *  everything except the clicked option; re-checking the last one collapses
 *  back to empty. So "all selected" and "no filter" are the same state, and the
 *  URL stays clean. */
export function FilterCard({
  title,
  options,
  selected,
  onChange,
  filteredCount,
  totalCount,
  hint,
  mapSlot,
  mapToggleLabel = "show map",
  maxVisible = 12,
}: {
  title: string;
  options: { key: string; label: string; count: number }[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  /** Trials passing ALL filters, including this card's. */
  filteredCount: number;
  /** Trials passing every OTHER card's filters — this card's denominator. */
  totalCount: number;
  hint?: string;
  /** Optional geo escape hatch: the website passes a choropleth, the studio
   *  passes nothing, so no topology dependency enters this package. */
  mapSlot?: ReactNode;
  mapToggleLabel?: string;
  maxVisible?: number;
}) {
  const active = selected.size > 0;
  const allKeys = options.map((o) => o.key);
  const isOn = (k: string) => !active || selected.has(k);

  function toggle(key: string) {
    if (!active) {
      // From "no filter" (all shown as checked), unticking one means
      // "everything except this".
      onChange(new Set(allKeys.filter((k) => k !== key)));
      return;
    }
    const next = new Set(selected);
    next.has(key) ? next.delete(key) : next.add(key);
    // Re-selecting everything is the same as no filter at all.
    onChange(next.size === allKeys.length || next.size === 0 ? new Set() : next);
  }

  /** "Select none" is deliberately NOT offered: it would render an empty
   *  dashboard, which nobody wants and which is one click from looking like a
   *  bug. Inverting is the useful operation. */
  function invert() {
    onChange(new Set(allKeys.filter((k) => !isOn(k))));
  }

  const visible = options.slice(0, maxVisible);
  const hidden = options.length - visible.length;

  return (
    <div
      className={`mb-4 rounded-lg border border-border p-3 ${
        active ? "bg-foreground/[0.07]" : "bg-foreground/[0.02]"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-sm font-medium">{title}</span>
        <span className="text-xs text-foreground/50 tabular-nums">
          ({filteredCount.toLocaleString()} of {totalCount.toLocaleString()}{" "}
          trials selected)
        </span>
        <div className="ml-auto flex gap-2 text-xs">
          <button
            type="button"
            className="text-blue-600 hover:text-blue-700 disabled:opacity-40"
            onClick={() => onChange(new Set())}
            disabled={!active}
          >
            Select all
          </button>
          <button
            type="button"
            className="text-blue-600 hover:text-blue-700 disabled:opacity-40"
            onClick={invert}
            disabled={!active}
          >
            Invert
          </button>
        </div>
      </div>
      {hint && <p className="mt-0.5 text-xs text-foreground/50">{hint}</p>}

      <div className="mt-2 flex flex-wrap gap-1.5">
        {visible.map((o) => {
          const on = isOn(o.key);
          return (
            <button
              key={o.key}
              type="button"
              onClick={() => toggle(o.key)}
              title={`${o.label} (${o.count})`}
              className={`inline-flex max-w-full items-center gap-1 rounded border px-2 py-0.5 text-xs transition-colors ${
                on
                  ? "border-border bg-card text-foreground hover:bg-foreground/5"
                  : "border-transparent bg-foreground/[0.08] text-foreground/50 hover:text-foreground/70"
              } ${o.count === 0 ? "opacity-50" : ""}`}
            >
              <span className="truncate">{o.label}</span>
              <span className="tabular-nums text-foreground/40">
                ({o.count.toLocaleString()})
              </span>
            </button>
          );
        })}
        {hidden > 0 && (
          <span className="self-center text-xs text-foreground/40">
            +{hidden} more
          </span>
        )}
      </div>

      {mapSlot && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-blue-600 underline hover:text-blue-700">
            {mapToggleLabel}
          </summary>
          <div className="mt-2">{mapSlot}</div>
        </details>
      )}
    </div>
  );
}
