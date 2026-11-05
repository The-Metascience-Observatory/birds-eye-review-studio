"use client";

// Compact list of interventions/ingredients shown as: name + one rectangle per
// trial (coloured by result/verdict segment), laid out in N columns, with a
// shared hover tooltip listing the individual studies. This is the "X with N or
// fewer trials (rectangles = trial results)" format used below the BreakdownChart
// bars and, grouped by category, in the long-covid "fewer trials" section.

import { useMemo } from "react";
import {
  Segment, HoverTrial, HoverPanel, useHoverPanel, rectAnchor,
} from "./hover/trialHoverPanel";

const MUTED_COLOR = "#94a3b8";

export interface TrialRectItem {
  key: string;
  label: string;
  counts: Record<string, number>; // segment key -> trial count
  selected?: boolean;
  // Spans all columns so a long row of rectangles (many trials) has room to lay
  // out flat instead of overflowing a 1/3-width column.
  wide?: boolean;
}

export function TrialRectList({
  items, segments, hoverTrials, onItemClick, columns = 3,
}: {
  items: TrialRectItem[];
  segments: Segment[];
  hoverTrials?: Record<string, HoverTrial[]>;
  onItemClick?: (key: string) => void;
  columns?: 1 | 2 | 3;
}) {
  const { openKey, anchor, show, scheduleHide, cancelHide } = useHoverPanel();

  // CSS multi-column (not grid) so items flow top-to-bottom down the first column,
  // then the next — i.e. column-major fill. `columns={1}` is a single full-width
  // column, used for `wide` items whose long row of rectangles needs the full width.
  const colCls = columns === 1
    ? "columns-1 gap-x-5"
    : columns === 2
    ? "columns-1 sm:columns-2 gap-x-5"
    : "columns-1 sm:columns-2 lg:columns-3 gap-x-5";

  // Build, per item, one tile per trial in segment order — and resolve each tile
  // to the specific study it represents so a hover targets that single trial
  // (not the whole intervention). Trials are bucketed by verdict and consumed in
  // order within each segment, mirroring the stacked bar's per-tile mapping.
  const itemTiles = useMemo(() => {
    const byItem: Record<string, { color: string; segLabel: string; trial: HoverTrial | null; hoverKey: string }[]> = {};
    const tileTrial = new Map<string, HoverTrial>();
    for (const d of items) {
      const all = hoverTrials?.[d.key] ?? [];
      // verdict bucket -> queue of trials, in push order.
      const byVerdict = new Map<string, HoverTrial[]>();
      for (const t of all) {
        const v = t.verdict && segments.some((s) => s.key === t.verdict) ? t.verdict : "unknown";
        const queue = byVerdict.get(v) ?? [];
        queue.push(t);
        byVerdict.set(v, queue);
      }
      const tiles: { color: string; segLabel: string; trial: HoverTrial | null; hoverKey: string }[] = [];
      for (const s of segments) {
        const c = d.counts[s.key] || 0;
        const queue = byVerdict.get(s.key) ?? [];
        for (let i = 0; i < c; i++) {
          const trial = queue[i] ?? null;
          const hoverKey = `${d.key}#${tiles.length}`;
          if (trial) tileTrial.set(hoverKey, trial);
          tiles.push({ color: s.color, segLabel: s.label, trial, hoverKey });
        }
      }
      if (tiles.length === 0) tiles.push({ color: MUTED_COLOR, segLabel: "No result", trial: null, hoverKey: `${d.key}#0` });
      byItem[d.key] = tiles;
    }
    return { byItem, tileTrial };
  }, [items, segments, hoverTrials]);

  const panelTrial = openKey ? itemTiles.tileTrial.get(openKey) ?? null : null;
  // hoverKey is `${itemKey}#${tileIndex}`; item keys never contain "#", so the
  // text before the final "#" is the intervention name (used as the panel header).
  const panelItemKey = openKey ? openKey.slice(0, openKey.lastIndexOf("#")) : "";

  return (
    <>
      <div className={colCls}>
        {items.map((d) => {
          const tiles = itemTiles.byItem[d.key] ?? [];
          const total = tiles.length;
          return (
            <button
              key={d.key}
              type="button"
              onClick={onItemClick ? () => onItemClick(d.key) : undefined}
              aria-label={`${d.label}: ${total} trial${total === 1 ? "" : "s"}`}
              className={`w-full break-inside-avoid mb-0.5 text-left text-xs rounded px-1 py-0.5 ${
                // Wide items (many trials) stay in a SINGLE column and stack the
                // rectangles below the label, so the long row wraps into multiple
                // rows within that column. Narrow items keep the inline
                // label | rectangles layout, one per column slot.
                d.wide ? "flex flex-col gap-0.5" : "flex items-center gap-2"
              } ${
                onItemClick ? "hover:bg-foreground/5 cursor-pointer" : ""
              } ${d.selected ? "bg-foreground/10 font-semibold" : ""}`}
            >
              {/* Wide items: name wraps in full above the rectangles (never truncated)
                  so the whole intervention name is readable. Narrow items: truncate
                  to fit the inline label | rectangles slot. */}
              <span className={`text-foreground/80 ${d.wide ? "w-full font-medium leading-snug" : "min-w-0 flex-1 truncate"}`}>{d.label}</span>
              <span className={`flex flex-wrap gap-px ${d.wide ? "w-full" : "shrink-0 justify-end"}`}>
                {tiles.map((t) => (
                  <span
                    key={t.hoverKey}
                    className="inline-block w-5 h-4"
                    style={{ backgroundColor: t.color }}
                    title={t.trial ? undefined : t.segLabel}
                    onMouseEnter={t.trial ? (e) => { e.stopPropagation(); show(t.hoverKey, rectAnchor(e.currentTarget)); } : undefined}
                    onMouseLeave={t.trial ? scheduleHide : undefined}
                  />
                ))}
              </span>
            </button>
          );
        })}
      </div>

      {panelTrial && openKey && (
        <HoverPanel
          trials={[panelTrial]}
          label={items.find((i) => i.key === panelItemKey)?.label ?? ""}
          anchor={anchor}
          onMouseEnter={cancelHide}
          onMouseLeave={scheduleHide}
        />
      )}
    </>
  );
}
