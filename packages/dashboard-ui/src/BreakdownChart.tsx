"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  LabelList,
  Cell,
  Rectangle,
} from "recharts";

import {
  Segment, HoverTrial, Anchor, rectAnchor, HoverPanel,
} from "./hover/trialHoverPanel";
import { TrialRectList } from "./TrialRectList";

// Re-export the shared types so existing imports of these from "@/components/BreakdownChart"
// (and the per-dashboard shims) keep working.
export type { Segment, HoverTrial };

const DEFAULT_COLOR = "#1a5276";
const MUTED_COLOR = "#94a3b8";
// SVG stroke/fill attributes do NOT resolve Tailwind classes, so every colour
// inside the chart must be an explicit hsl(var(--token)) or currentColor.
// Hardcoding them (as the website does) renders invisible axis labels and
// white "shattered" tile dividers on a dark card.
const SELECT_OUTLINE = "hsl(var(--foreground))";
const HOVER_OUTLINE = "hsl(var(--foreground))";

// ─── TiledSegment ─────────────────────────────────────────────────────────────
// Renders one stacked-bar segment as `value` individual tiles. Each tile is an
// individual study, with its own transparent hit-rect on top firing per-tile
// hover/click events so the tooltip can target a single study.
interface TiledSegmentProps {
  x: number; y: number; width: number; height: number;
  fill?: string; fillOpacity?: number; radius?: number | number[];
  payload: Record<string, unknown>; segKey: string; leading: boolean;
  isLast?: boolean; selectedKey?: string;
  // Hover highlight target: a tile index (≥0), -1 for the whole segment, or null/none.
  hoveredTileIndex?: number | null;
  // tileIndex is null when the cursor is over a dense segment (too thin to tile).
  onTileHover?: (tileIndex: number | null, anchor: Anchor) => void;
  onTileLeave?: () => void;
  onTileClick?: () => void;
  [key: string]: unknown;
}

// Minimum tile width (px) for which per-study hit-targets are worth rendering.
// Below this, tiles are sub-pixel — un-hoverable and thousands of extra DOM nodes —
// so the whole segment becomes one hover target (shows that segment's trial list).
const MIN_TILE_PX = 4;
function TiledSegment(props: TiledSegmentProps) {
  const {
    x, y, width, height, fill, radius, payload, segKey, leading, isLast, selectedKey,
    hoveredTileIndex, onTileHover, onTileLeave, onTileClick,
  } = props;
  const value = Number(payload?.[segKey] ?? 0);
  const hasSelection = selectedKey != null && selectedKey !== "";
  const isSelected = hasSelection && payload?.key === selectedKey;
  const dimmed = hasSelection && !isSelected;

  if (!(value > 0 && width > 0)) return <g />;

  const per = width / value;
  const opacity = dimmed ? 0.25 : 1;
  // Tile mode: render one hit-target (and divider) per study. Disabled when tiles
  // would be sub-pixel — then the whole segment is a single hover target instead.
  const tileMode = per >= MIN_TILE_PX;

  // Base fill for the whole segment (rounded right corners only on the last one).
  const baseRect = (
    <Rectangle x={x} y={y} width={width} height={height} fill={fill}
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      fillOpacity={opacity} radius={radius as any} />
  );

  // White dividers between tiles (+ leading edge when not the first segment).
  // Only in tile mode — dense segments render as a single solid block.
  const dividers: React.ReactNode[] = [];
  if (tileMode) {
    for (let k = leading ? 0 : 1; k < value; k++) {
      const lx = x + k * per;
      dividers.push(
        <line key={`d${k}`} x1={lx} x2={lx} y1={y} y2={y + height}
          stroke="hsl(var(--card))" strokeWidth={1.5} pointerEvents="none" opacity={dimmed ? 0.5 : 1} />
      );
    }
  } else if (leading) {
    // Still divide adjacent segments from each other.
    dividers.push(
      <line key="dlead" x1={x} x2={x} y1={y} y2={y + height}
        stroke="hsl(var(--card))" strokeWidth={1.5} pointerEvents="none" opacity={dimmed ? 0.5 : 1} />
    );
  }

  // Click-select outline runs along the OUTER edges of the full bar row.
  const clickOutline: React.ReactNode[] = [];
  if (isSelected) {
    const ol = (key: string, x1: number, y1: number, x2: number, y2: number) => (
      <line key={key} x1={x1} y1={y1} x2={x2} y2={y2}
        stroke={SELECT_OUTLINE} strokeWidth={2} pointerEvents="none" />
    );
    clickOutline.push(ol("t", x, y, x + width, y));
    clickOutline.push(ol("b", x, y + height, x + width, y + height));
    if (!leading) clickOutline.push(ol("l", x, y, x, y + height));
    if (isLast) clickOutline.push(ol("r", x + width, y, x + width, y + height));
  }

  // Hover highlight: dark outline around the single tile (tile mode) or the whole
  // segment (dense mode, signalled by hoveredTileIndex === -1).
  let hoverRect: React.ReactNode = null;
  if (hoveredTileIndex === -1) {
    hoverRect = (
      <rect x={x} y={y} width={width} height={height} fill="none"
        stroke={HOVER_OUTLINE} strokeWidth={2} pointerEvents="none" />
    );
  } else if (hoveredTileIndex != null && hoveredTileIndex >= 0 && hoveredTileIndex < value) {
    const hx = x + hoveredTileIndex * per;
    hoverRect = (
      <rect x={hx} y={y} width={per} height={height} fill="none"
        stroke={HOVER_OUTLINE} strokeWidth={2} pointerEvents="none" />
    );
  }

  // Transparent hit targets on top. Tile mode → one per study (per-study tooltip);
  // dense mode → a single segment-wide target (shows that segment's trial list).
  const hits: React.ReactNode[] = [];
  if (onTileHover || onTileClick) {
    if (tileMode) {
      for (let k = 0; k < value; k++) {
        const tx = x + k * per;
        hits.push(
          <rect key={`h${k}`} x={tx} y={y} width={per} height={height}
            fill="transparent" pointerEvents="all"
            style={{ cursor: onTileClick ? "pointer" : "default" }}
            onMouseEnter={onTileHover ? (e) => onTileHover(k, rectAnchor(e.currentTarget)) : undefined}
            onMouseLeave={onTileLeave}
            onClick={onTileClick}
          />
        );
      }
    } else {
      hits.push(
        <rect key="hseg" x={x} y={y} width={width} height={height}
          fill="transparent" pointerEvents="all"
          style={{ cursor: onTileClick ? "pointer" : "default" }}
          onMouseEnter={onTileHover ? (e) => onTileHover(null, rectAnchor(e.currentTarget)) : undefined}
          onMouseLeave={onTileLeave}
          onClick={onTileClick}
        />
      );
    }
  }

  return <g>{baseRect}{dividers}{clickOutline}{hoverRect}{hits}</g>;
}

// ─── RowBackground (simple, non-stacked mode only) ───────────────────────────
interface RowBackgroundProps {
  x?: number; y?: number; width?: number; height?: number; index?: number;
  gap?: number;
  onPick?: (i: number) => void;
  onHover?: (i: number, anchor: Anchor) => void;
  onHoverEnd?: () => void;
}
function RowBackground(props: RowBackgroundProps) {
  const { x = 0, y = 0, width = 0, height = 0, index = 0, gap = 0, onPick, onHover, onHoverEnd } = props;
  return (
    <rect
      x={x} y={y - gap / 2} width={width} height={height + gap}
      fill="transparent"
      cursor={onPick ? "pointer" : "default"}
      onClick={() => onPick?.(index)}
      onMouseEnter={(e) => onHover?.(index, rectAnchor(e.currentTarget))}
      onMouseLeave={onHoverEnd}
    />
  );
}

// ─── BreakdownChart ───────────────────────────────────────────────────────────
export function BreakdownChart({
  title, breakdown, labels, colors, onBarClick, clickHint,
  segments, selectedKey, collapseSingletons, collapseMaxTrials, hoverTrials, footer, studyCount,
}: {
  title: string;
  /** Unique studies, when categories overlap (e.g. multi-arm interventions). */
  studyCount?: number;
  breakdown: Record<string, number> | Record<string, Record<string, number>>;
  labels?: Record<string, string>;
  colors?: Record<string, string>;
  onBarClick?: (key: string) => void;
  clickHint?: string;
  segments?: Segment[];
  selectedKey?: string;
  collapseSingletons?: boolean;
  // Max trial count an entry can have to be moved into the compact list below the
  // bars (instead of getting its own bar). Default 1 (singletons only).
  collapseMaxTrials?: number;
  hoverTrials?: Record<string, HoverTrial[]>;
  // Extra content rendered inside the card, below the chart (e.g. a grouped
  // "fewer trials" list) so it shares the same card as the bars.
  footer?: React.ReactNode;
}) {
  const prettyLabel = (key: string) =>
    labels?.[key] ?? key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  const [isMobile, setIsMobile] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 640px)");
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  // ── Hover state ──────────────────────────────────────────────────────────────
  // segKey + tileIndex set in stacked mode (per study); neither in simple mode.
  const [hover, setHover] = useState<{ key: string; segKey?: string; tileIndex?: number } | null>(null);
  const [panelPos, setPanelPos] = useState<Anchor>({ left: 0, top: 0, bottom: 0 });
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelHide = useCallback(() => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
  }, []);
  const startHide = useCallback(() => {
    cancelHide();
    hideTimer.current = setTimeout(() => setHover(null), 150);
  }, [cancelHide]);
  useEffect(() => () => { if (hideTimer.current) clearTimeout(hideTimer.current); }, []);

  // Simple-mode: whole-row hover.
  const handleRowHover = useCallback((key: string, anchor: Anchor) => {
    cancelHide();
    setHover({ key });
    setPanelPos(anchor);
  }, [cancelHide]);

  // Stacked-mode hover: a single study (tileIndex) or, on dense segments too thin
  // to tile, the whole segment (tileIndex null → panel shows that segment's list).
  const handleTileHover = useCallback(
    (key: string, segKey: string, tileIndex: number | null, anchor: Anchor) => {
      cancelHide();
      setHover({ key, segKey, tileIndex: tileIndex ?? undefined });
      setPanelPos(anchor);
    },
    [cancelHide]
  );

  // ── Segment-key bucketing (so trials sort into the same tiles as the counts) ──
  const segKeySet = useMemo(() => new Set(segments?.map((s) => s.key) ?? []), [segments]);
  const bucketVerdict = useCallback(
    (v: string | undefined) => (v && segKeySet.has(v) ? v : "unknown"),
    [segKeySet]
  );

  // ── Trials to show in the panel ──────────────────────────────────────────────
  const panelTrials = useMemo(() => {
    if (!hover || !hoverTrials) return null;
    const all = hoverTrials[hover.key] ?? [];
    if (hover.segKey == null) return all;                  // simple mode → all trials
    const inSeg = all.filter((t) => bucketVerdict(t.verdict) === hover.segKey);
    if (hover.tileIndex == null) return inSeg;             // fallback → whole segment
    const one = inSeg[hover.tileIndex];                    // per-tile → single study
    return one ? [one] : null;
  }, [hover, hoverTrials, bucketVerdict]);

  const panelSegment = hover?.segKey ? segments?.find((s) => s.key === hover.segKey) : undefined;

  // ── Data ─────────────────────────────────────────────────────────────────────
  const barHeight = 26;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const allData: any[] = segments
    ? Object.entries(breakdown as Record<string, Record<string, number>>)
        .map(([key, segCounts]) => {
          const total = segments.reduce((s, seg) => s + (segCounts[seg.key] || 0), 0);
          const row: Record<string, number | string> = { key, label: prettyLabel(key), total };
          for (const seg of segments) row[seg.key] = segCounts[seg.key] || 0;
          return row;
        })
        .sort((a, b) => (b.total as number) - (a.total as number))
    : Object.entries(breakdown as Record<string, number>)
        .map(([key, count]) => ({
          key, label: prettyLabel(key), count,
          color: colors?.[key] ?? (key === "unclassified" || key === "unclear" ? MUTED_COLOR : DEFAULT_COLOR),
        }))
        .sort((a, b) => b.count - a.count);

  const AUTO_SINGLETON_THRESHOLD = 8;
  // Entries with `total <= compactMax` are pulled out of the bar chart and shown
  // as a compact alphabetized list below it (each trial drawn as one rectangle).
  const compactMax = collapseMaxTrials ?? 1;
  const compactCount = segments ? allData.filter((d) => (d.total as number) <= compactMax).length : 0;
  const useCollapse = !!segments && collapseSingletons !== false &&
    (collapseSingletons === true || compactCount >= AUTO_SINGLETON_THRESHOLD);
  const data = useCollapse ? allData.filter((d) => (d.total as number) > compactMax) : allData;
  const singletonData = useCollapse
    ? allData
        .filter((d) => (d.total as number) <= compactMax)
        // Sort by number of trials (descending, like the bars above); within each
        // trial count, sort alphabetically by label.
        .sort((a, b) =>
          (b.total as number) - (a.total as number) ||
          (a.label as string).localeCompare(b.label as string)
        )
    : [];
  const total = allData.reduce((s, d) => s + (segments ? (d.total as number) : (d.count as number)), 0);

  const labelRow = isMobile ? 20 : 0;
  const chartHeight = data.length * (barHeight + 14 + labelRow) + 50;

  const labelToKey: Record<string, string> = {};
  for (const d of data) labelToKey[d.label as string] = d.key as string;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const CategoryTick = (p: any) => {
    const label = p.payload?.value as string;
    const key = labelToKey[label];
    const isSelected = selectedKey != null && selectedKey !== "" && key === selectedKey;
    const clickable = !!onBarClick;
    const common = {
      fontSize: 12, fontWeight: isSelected ? 700 : 500, fill: "currentColor",
      cursor: clickable ? "pointer" : undefined,
      onClick: clickable && key != null ? () => onBarClick!(key) : undefined,
      className: clickable ? "hover:underline" : undefined,
    } as const;
    return isMobile
      ? <text x={p.x} y={p.y - barHeight / 2 - 6} textAnchor="start" {...common}>{label}</text>
      : <text x={p.x} y={p.y} dy="0.355em" textAnchor="end" {...common}>{label}</text>;
  };

  // RowBackground: simple (non-stacked) mode only.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const rowBg = !segments && (onBarClick || hoverTrials)
    ? (p: any) => (
        <RowBackground
          {...(p as RowBackgroundProps)}
          gap={14}
          onPick={onBarClick ? (i) => onBarClick(data[i].key as string) : undefined}
          onHover={hoverTrials ? (i, anchor) => handleRowHover(data[i].key as string, anchor) : undefined}
          onHoverEnd={hoverTrials ? startHide : undefined}
        />
      )
    : undefined;

  return (
    <div className="border border-border rounded-lg bg-card p-3 sm:p-4 mb-6 text-foreground/75">
      <div className="flex flex-wrap items-baseline gap-x-4 mb-2">
        <h2 className="text-lg font-semibold">{title} (n = {(studyCount ?? total).toLocaleString()})</h2>
        {studyCount != null && total !== studyCount && (
          <p className="basis-full text-sm text-foreground/60">
            {total.toLocaleString()} category assignments across {studyCount.toLocaleString()} studies.
            A study can appear in multiple categories; missing categories are not counted.
          </p>
        )}
        {onBarClick && clickHint && (
          <p className="basis-full text-sm text-foreground/60">{clickHint}</p>
        )}
      </div>

      {segments && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 mb-3">
          {segments.map((seg) => (
            <span key={seg.key} className="inline-flex items-center gap-1.5 text-xs text-foreground/70">
              <span className="inline-block w-3 h-3 rounded-sm" style={{ backgroundColor: seg.color }} />
              {seg.label}
            </span>
          ))}
        </div>
      )}

      <ResponsiveContainer width="100%" height={chartHeight}>
        <BarChart data={data} layout="vertical" margin={{ left: 10, right: 50, top: 5, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} horizontal={false} />
          <XAxis type="number" domain={[0, "dataMax"]} tickFormatter={(v: number) => v.toLocaleString()} fontSize={12} allowDecimals={false} />
          <YAxis
            type="category" dataKey="label"
            width={isMobile ? 1 : 220} fontSize={12}
            tickLine={false} axisLine={false} interval={0}
            tick={<CategoryTick />}
          />
          {/* No recharts tooltip in stacked mode — per-tile HoverPanel replaces it. */}
          {!segments && (
            <Tooltip
              formatter={(value: number) => [value.toLocaleString(), "Studies"]}
              labelStyle={{ fontWeight: 600 }}
            />
          )}

          {segments ? (
            segments.map((seg, si) => {
              const isLast = si === segments.length - 1;
              return (
                <Bar
                  key={seg.key}
                  dataKey={seg.key}
                  name={seg.label}
                  stackId="a"
                  fill={seg.color}
                  barSize={barHeight}
                  radius={isLast ? [0, 4, 4, 0] : undefined}
                  isAnimationActive={false}
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  shape={(p: any) => {
                    const rowKey = p.payload?.key as string | undefined;
                    // -1 highlights the whole segment (dense mode); else the tile index.
                    const hoveredTileIndex =
                      hover && hover.key === rowKey && hover.segKey === seg.key
                        ? hover.tileIndex ?? -1
                        : null;
                    return (
                      <TiledSegment
                        {...p}
                        segKey={seg.key}
                        leading={si > 0}
                        isLast={isLast}
                        selectedKey={selectedKey}
                        hoveredTileIndex={hoveredTileIndex}
                        onTileHover={
                          hoverTrials && rowKey
                            ? (tileIndex: number | null, anchor: Anchor) =>
                                handleTileHover(rowKey, seg.key, tileIndex, anchor)
                            : undefined
                        }
                        onTileLeave={hoverTrials ? startHide : undefined}
                        onTileClick={onBarClick && rowKey ? () => onBarClick(rowKey) : undefined}
                      />
                    );
                  }}
                >
                  {isLast && (
                    <LabelList dataKey="total" position="right" fontSize={12} fill="currentColor"
                      formatter={(v: number) => v.toLocaleString()} />
                  )}
                </Bar>
              );
            })
          ) : (
            <Bar
              dataKey="count" radius={[0, 4, 4, 0]} barSize={barHeight}
              cursor={onBarClick ? "pointer" : undefined}
              onClick={onBarClick ? (_: unknown, index: number) => onBarClick(data[index].key as string) : undefined}
              background={rowBg}
            >
              {data.map((d) => {
                const hasSelection = selectedKey != null && selectedKey !== "";
                const isSelected = hasSelection && d.key === selectedKey;
                return (
                  <Cell key={d.key} fill={d.color}
                    fillOpacity={hasSelection && !isSelected ? 0.25 : 1}
                    stroke={isSelected ? SELECT_OUTLINE : undefined}
                    strokeWidth={isSelected ? 2 : undefined}
                  />
                );
              })}
              <LabelList dataKey="count" position="right" fontSize={12} fill="currentColor"
                formatter={(v: number) => v.toLocaleString()} />
            </Bar>
          )}
        </BarChart>
      </ResponsiveContainer>

      {/* Low-trial keys: compact alphabetized list, one rectangle per trial */}
      {singletonData.length > 0 && segments && (
        <div className="mt-3 pt-3 border-t border-border">
          <p className="text-xs text-foreground/60 mb-2">
            {singletonData.length.toLocaleString()}{" "}
            {compactMax === 1 ? "with a single trial" : `with ${compactMax} or fewer trials`}:
          </p>
          <TrialRectList
            items={singletonData.map((d) => ({
              key: d.key as string,
              label: d.label as string,
              counts: Object.fromEntries(segments.map((s) => [s.key, (d[s.key] as number) || 0])),
              selected: selectedKey != null && selectedKey !== "" && d.key === selectedKey,
            }))}
            segments={segments}
            hoverTrials={hoverTrials}
            onItemClick={onBarClick}
          />
        </div>
      )}

      {/* Extra in-card content (e.g. a grouped "fewer trials" list). */}
      {footer && <div className="mt-4 pt-4 border-t border-border">{footer}</div>}

      {/* Hover panel: single study in stacked mode, all-row trials in simple mode */}
      {panelTrials && panelTrials.length > 0 && hover && (
        <HoverPanel
          trials={panelTrials}
          label={prettyLabel(hover.key)}
          segColor={panelSegment?.color}
          anchor={panelPos}
          onMouseEnter={cancelHide}
          onMouseLeave={startHide}
        />
      )}
    </div>
  );
}
