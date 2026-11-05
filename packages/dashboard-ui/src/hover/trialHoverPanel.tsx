"use client";

// Shared trial hover/tooltip primitives used by both the BreakdownChart bars and
// the compact "per-trial rectangle" lists (TrialRectList). Kept in one module so
// the hover panel + anchoring stay identical everywhere.

import { useState, useRef, useCallback, useEffect, useLayoutEffect } from "react";
import { ExternalLink } from "lucide-react";

/** A stacked-segment definition: field key, legend label, colour. */
export type Segment = { key: string; label: string; color: string };

/** One study, as shown in the hover panel. */
export interface HoverTrial {
  doi: string;
  label: string; // lead author, e.g. "Smith et al." (no year — year is rendered last)
  title?: string; // paper title, linked to the external source in the reference
  year?: string | number | null; // shown at the end of the reference
  journal: string;
  n: number | null;
  design: string;
  phase: string | null;
  verdict?: string; // must match a segment key for stacked-mode per-tile filtering
  note?: string;    // optional one-line note (e.g. the verdict rationale)
}

/** Viewport-space box of the hovered rectangle, used to anchor the hover panel
 *  below (or, on overflow, above) the tile/bar rather than at the cursor. */
export type Anchor = { left: number; top: number; bottom: number };

export function rectAnchor(el: Element): Anchor {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, bottom: r.bottom };
}

/** Resolve a study identifier (DOI or PMID) to an external URL. */
export function trialLink(doi: string): string {
  if (/^pmid/i.test(doi)) {
    const digits = doi.match(/\d+/)?.[0];
    return digits ? `https://pubmed.ncbi.nlm.nih.gov/${digits}/` : "#";
  }
  return `https://doi.org/${doi}`;
}

/** Floating reference-style panel listing the studies for a hovered bar/tile/row. */
export function HoverPanel({
  trials, label, segColor, anchor, onMouseEnter, onMouseLeave,
}: {
  trials: HoverTrial[]; label: string; segColor?: string;
  anchor: Anchor; onMouseEnter: () => void; onMouseLeave: () => void;
}) {
  const panelW = 288;
  const gap = 8; // space between the rectangle and the panel

  // Measure the panel so we can flip it above the rectangle when placing it
  // below would overflow the viewport bottom.
  const panelRef = useRef<HTMLDivElement>(null);
  const [panelH, setPanelH] = useState(0);
  useLayoutEffect(() => {
    if (panelRef.current) setPanelH(panelRef.current.offsetHeight);
  }, [trials]);

  const vw = typeof window !== "undefined" ? window.innerWidth : 1200;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  // Align the panel's left edge to the rectangle, clamped into the viewport.
  const left = Math.max(8, Math.min(anchor.left, vw - panelW - 8));
  // Prefer below the rectangle; flip above only when below overflows and there
  // is room above.
  const fitsBelow = anchor.bottom + gap + panelH <= vh;
  const roomAbove = anchor.top - gap - panelH >= 8;
  const top = fitsBelow || !roomAbove ? anchor.bottom + gap : anchor.top - gap - panelH;
  const single = trials.length === 1;

  return (
    <div
      ref={panelRef}
      style={{ position: "fixed", left, top, zIndex: 9999, width: panelW }}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="max-h-80 overflow-y-auto rounded-lg border border-border bg-card shadow-xl text-xs"
    >
      <div className="sticky top-0 bg-card px-3 py-2 border-b border-border">
        <div className="font-semibold text-foreground/90">{label}</div>
        {!single && (
          <div className="flex items-center gap-1.5 text-[11px] text-foreground/60 mt-0.5">
            {segColor && <span className="inline-block w-2 h-2 rounded-sm shrink-0" style={{ backgroundColor: segColor }} />}
            <span>{trials.length} trials</span>
          </div>
        )}
      </div>
      <div className="divide-y divide-border/50">
        {trials.map((t, i) => {
          // Reference-style line: Author Year. Title (linked). Journal. Design · N. Phase.
          const meta = [t.design, t.n != null ? `N = ${t.n.toLocaleString()}` : null]
            .filter(Boolean)
            .join(" · ");
          return (
            <div key={i} className="px-3 py-2 leading-snug text-foreground/70">
              {t.label && (
                <span className="font-medium text-foreground/90">
                  {t.label}{t.label.endsWith(".") ? "" : "."}{" "}
                </span>
              )}
              {t.title && (
                <>
                  <a href={trialLink(t.doi)} target="_blank" rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="text-blue-600 hover:text-blue-700 hover:underline">
                    {t.title}
                    <ExternalLink size={10} className="inline align-baseline ml-0.5 mb-px" />
                  </a>
                  {". "}
                </>
              )}
              {t.journal && <><em className="italic">{t.journal}</em>{". "}</>}
              {meta && <>{meta}. </>}
              {t.phase && <>{t.phase}. </>}
              {t.year != null && String(t.year).trim() !== "" && <>{t.year}. </>}
              {!t.title && (
                <a href={trialLink(t.doi)} target="_blank" rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="text-blue-500 hover:text-blue-700 align-baseline">
                  <ExternalLink size={11} className="inline" />
                </a>
              )}
              {t.note && (
                <div className="mt-1 text-[11px] italic text-foreground/55">{t.note}</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Simple key-based hover state (with a grace delay so the cursor can travel from
 *  the row to the panel). Used by row/rectangle lists; BreakdownChart's bars use a
 *  richer state of their own but render the same HoverPanel. */
export function useHoverPanel() {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<Anchor>({ left: 0, top: 0, bottom: 0 });
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelHide = useCallback(() => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
  }, []);
  const scheduleHide = useCallback(() => {
    cancelHide();
    hideTimer.current = setTimeout(() => setOpenKey(null), 150);
  }, [cancelHide]);
  const show = useCallback((key: string, a: Anchor) => {
    cancelHide();
    setOpenKey(key);
    setAnchor(a);
  }, [cancelHide]);
  useEffect(() => () => { if (hideTimer.current) clearTimeout(hideTimer.current); }, []);

  return { openKey, anchor, show, scheduleHide, cancelHide };
}
