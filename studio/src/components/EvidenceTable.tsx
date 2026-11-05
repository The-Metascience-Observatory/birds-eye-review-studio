"use client";

/** The live evidence table — one row per paper, filling in as jobs run.
 *
 *  Papertrace's Evidence workspace: a wide, column-configurable table of paper
 *  metadata + AI outputs, with a per-paper PICO expand and a manual-exclude
 *  column. It reads the /evidence endpoint (a progressive DOI-keyed merge of
 *  every artifact on disk), so rows appear during search and gain columns
 *  through screening and extraction without the user leaving the page.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronsUpDown,
  Columns3,
  Loader2,
  Search,
  X,
} from "lucide-react";

import { VERDICT_LABEL } from "@bird/dashboard-ui";

import { PaperDetailPanel } from "@/components/PaperDetailPanel";
import type { EvidenceRow } from "@/lib/api";
import { fixAuthorCaps, stripTags, titleCase } from "@/lib/format";
import { usePref } from "@/lib/prefs";

// Risk of bias sorts by severity, not alphabetically ("some concerns" would
// otherwise land after "low" AND "high").
const ROB_RANK: Record<string, number> = { low: 0, some_concerns: 1, high: 2 };

/** Long free-text cell (PICO fields, journal): truncate with the full text on
 *  hover. `block` is required — the row renderer wraps cell content in an
 *  inline span, where `truncate` is a no-op. */
function Clipped({ text, max = "max-w-xs", italic = false }: {
  text: string; max?: string; italic?: boolean;
}) {
  return (
    <span className={`block ${max} truncate ${italic ? "italic" : ""}`} title={text}>
      {text}
    </span>
  );
}

// Case-insensitive sort key for a joined string list; null (sorts last) when empty.
const joined = (xs: string[] | undefined) =>
  xs && xs.length ? xs.join(", ").toLowerCase() : null;

/** Every column the table can show, in display order. `stageNeeded` dims the
 *  header until the pipeline has reached the stage that populates it. */
const COLUMNS: {
  id: string;
  label: string;
  stageNeeded: string;
  get: (r: EvidenceRow) => React.ReactNode;
  className?: string;
  // The header's sort toggle orders by this value; null/undefined rows always
  // sort last regardless of direction. Every column is sortable.
  sortValue: (r: EvidenceRow) => string | number | null | undefined;
}[] = [
  { id: "title", label: "Title", stageNeeded: "screen",
    // Every row is DOI-keyed but metadata lands progressively, so before a
    // title arrives fall back to the DOI (muted) — never a fully blank row.
    get: (r) =>
      r.title ? (
        stripTags(r.title)
      ) : (
        <span className="font-normal text-muted-foreground">
          {r.doi || "—"}
        </span>
      ),
    className: "min-w-64 max-w-md font-medium",
    sortValue: (r) => (r.title ? stripTags(r.title).toLowerCase() : null) },
  { id: "authors", label: "Author / Year", stageNeeded: "screen",
    get: (r) => (r.authors || r.year ? `${leadAuthor(r.authors)} ${r.year ? `(${r.year})` : ""}` : "—"),
    sortValue: (r) => (r.authors || "").toLowerCase() || null },
  { id: "journal", label: "Journal", stageNeeded: "screen",
    get: (r) => (r.journal ? <Clipped text={r.journal} max="max-w-40" italic /> : "—"),
    className: "max-w-40",
    sortValue: (r) => (r.journal || "").toLowerCase() || null },
  { id: "population", label: "Population", stageNeeded: "extract",
    get: (r) => (r.pico?.population ? <Clipped text={r.pico.population} /> : "—"),
    className: "max-w-xs",
    sortValue: (r) => r.pico?.population?.toLowerCase() || null },
  { id: "intervention", label: "Intervention", stageNeeded: "extract",
    get: (r) =>
      r.pico?.intervention?.length
        ? <Clipped text={r.pico.intervention.join(", ")} />
        : "—",
    className: "max-w-xs",
    sortValue: (r) => joined(r.pico?.intervention) },
  { id: "comparator", label: "Comparator", stageNeeded: "extract",
    get: (r) => r.pico?.comparator?.join(", ") || "—", className: "max-w-xs",
    sortValue: (r) => joined(r.pico?.comparator) },
  { id: "design_type", label: "Study design", stageNeeded: "extract",
    get: (r) => titleCase(r.design_type || r.trial_type || "") || "—",
    sortValue: (r) => (r.design_type || r.trial_type || "").toLowerCase() || null },
  { id: "n", label: "Sample size", stageNeeded: "extract",
    get: (r) => (r.n != null ? Number(r.n).toLocaleString() : "—"),
    sortValue: (r) => (r.n != null ? Number(r.n) : null) },
  { id: "has_fulltext", label: "Full-text", stageNeeded: "screen",
    get: (r) => <FullTextCell row={r} />, className: "text-center",
    sortValue: (r) => (r.has_fulltext ? 1 : 0) },
  { id: "primary_name", label: "Primary outcome", stageNeeded: "extract",
    get: (r) => r.primary_name ?? "—", className: "max-w-xs",
    sortValue: (r) => r.primary_name?.toLowerCase() || null },
  { id: "outcomes", label: "All outcomes", stageNeeded: "extract",
    get: (r) =>
      r.pico?.outcome?.length
        ? <Clipped text={r.pico.outcome.join("; ")} max="max-w-sm" />
        : "—",
    className: "max-w-sm",
    sortValue: (r) => joined(r.pico?.outcome) },
  { id: "verdict", label: "Verdict", stageNeeded: "extract",
    // The study's direction-of-effect (e.g. "Favors treatment"). "unknown" is a
    // non-verdict, so it reads as "—" like an absent value.
    get: (r) =>
      r.verdict && r.verdict !== "unknown" ? (VERDICT_LABEL[r.verdict] ?? titleCase(r.verdict)) : "—",
    sortValue: (r) =>
      r.verdict && r.verdict !== "unknown" ? r.verdict.toLowerCase() : null },
  { id: "follow_up_weeks", label: "Follow-up", stageNeeded: "extract",
    get: (r) => (r.follow_up_weeks != null ? `${r.follow_up_weeks} wk` : "—"),
    sortValue: (r) => (r.follow_up_weeks != null ? Number(r.follow_up_weeks) : null) },
  { id: "rob", label: "Risk of bias", stageNeeded: "extract",
    get: (r) => titleCase(r.rob || "") || "—",
    sortValue: (r) => (r.rob != null ? ROB_RANK[r.rob] ?? null : null) },
  { id: "relevance", label: "Relevant", stageNeeded: "search",
    get: (r) => (r.relevance ? titleCase(r.relevance) : "—"),
    sortValue: (r) => r.relevance?.toLowerCase() || null },
  { id: "organism", label: "Organism", stageNeeded: "screen",
    get: (r) => (r.organism ? organismLabel(r.organism) : "—"),
    sortValue: (r) => r.organism ?? null },
];

const DEFAULT_COLUMNS = [
  "title", "authors", "journal", "intervention", "design_type", "n",
  "has_fulltext", "primary_name", "comparator", "verdict", "follow_up_weeks",
];

const STAGE_ORDER = ["setup", "search", "screen", "extract", "synthesize", "publish"];

// Rows rendered per chunk: the initial window and each infinite-scroll step.
const PAGE_SIZE = 100;

// Default-sort rank: included → pending → screened_out → excluded, computed
// LIVE from the current screen_state. Included (green) rows always float to
// the top: a paper that becomes included mid-run — extraction lands, the
// full-text screen rules, or a manual include — rises on the next poll.
const RANK: Record<string, number> = {
  included: 0, pending: 1, screened_out: 2, excluded: 3,
};

export function EvidenceTable({
  reviewId,
  rows,
  stageReached,
  onExclude,
  onInclude,
  isMutating,
}: {
  reviewId: string;
  rows: EvidenceRow[];
  stageReached: string;
  onExclude: (doi: string) => void;
  onInclude: (doi: string) => void;
  isMutating?: boolean;
}) {
  // Column choice persists per-browser. `null` (never customized) resolves to
  // DEFAULT_COLUMNS; stored ids are intersected with the current registry so a
  // renamed/removed column can't linger as a ghost entry.
  const [storedColumns, setStoredColumns] = usePref("evidenceColumns");
  const visible = useMemo(() => {
    const valid = new Set(COLUMNS.map((c) => c.id));
    return new Set((storedColumns ?? DEFAULT_COLUMNS).filter((id) => valid.has(id)));
  }, [storedColumns]);
  const toggleColumn = (id: string) => {
    const next = new Set(visible);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setStoredColumns(Array.from(next));
  };
  // Which paper's detail drawer is open (by DOI).
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showPicker, setShowPicker] = useState(false);
  const [filter, setFilter] = useState<"all" | "extracted" | "excluded" | "needs_review">("all");
  // Column sort. null key = the natural (as-loaded) order.
  const [sort, setSort] = useState<{ key: string; dir: "asc" | "desc" } | null>(null);

  // How many rows are currently rendered. Starts at one page and grows by a page
  // each time the bottom sentinel scrolls into view (infinite scroll), so a
  // 500-row corpus renders ~100 rows up front and fills in as the user scrolls.
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  const reachedIdx = STAGE_ORDER.indexOf(stageReached);
  const shown = COLUMNS.filter((c) => visible.has(c.id));
  const selectedRow = selected ? rows.find((r) => r.doi === selected) : null;

  // Header counts, from the same screen_state the pill/tint reads.
  const includedCount = rows.filter((r) => r.screen_state === "included").length;
  const excludedCount = rows.filter((r) => r.screen_state === "excluded").length;

  // Rows the user manually toggles are PINNED at their pre-toggle sort position
  // for the rest of the session: the click recolors the row in place instead of
  // teleporting it to its new group, so the pill stays under the cursor for an
  // easy undo. Rows changed by the pipeline (polling) still re-rank live.
  const pinnedRank = useRef<Map<string, number>>(new Map());
  const pinRow = (doi: string) => {
    if (pinnedRank.current.has(doi)) return; // keep the FIRST pinned spot
    const r = rows.find((x) => x.doi === doi);
    pinnedRank.current.set(doi, RANK[r?.screen_state ?? "screened_out"] ?? 2);
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = rows.filter((r) => {
      if (filter === "extracted" && !r.extracted) return false;
      if (filter === "excluded" && !r.is_excluded) return false;
      if (filter === "needs_review" && (r.is_excluded || r.extracted)) return false;
      if (!q) return true;
      return (
        (r.title || "").toLowerCase().includes(q) ||
        (r.authors || "").toLowerCase().includes(q) ||
        (r.journal || "").toLowerCase().includes(q) ||
        r.doi.toLowerCase().includes(q)
      );
    });

    const col = sort && COLUMNS.find((c) => c.id === sort.key);
    if (sort && col?.sortValue) {
      const getV = col.sortValue;
      const sign = sort.dir === "asc" ? 1 : -1;
      // Decorate–sort–undecorate: carry the original index so the sort is stable
      // and missing values ("—") stay pinned last in BOTH directions rather than
      // elbowing ahead of real data when sorting descending.
      return out
        .map((r, i) => ({ r, i, v: getV(r) }))
        .sort((a, b) => {
          const ma = a.v == null || a.v === "";
          const mb = b.v == null || b.v === "";
          if (ma && mb) return a.i - b.i;
          if (ma) return 1;
          if (mb) return -1;
          if (a.v! < b.v!) return -1 * sign;
          if (a.v! > b.v!) return 1 * sign;
          return a.i - b.i;
        })
        .map((d) => d.r);
    }
    // Default order (no column sort chosen): included first, then papers still
    // Pending the full-text verdict, then screened-out, then excluded last — so
    // what's going (or likely going) to extraction sits at the top. Ranked live
    // (a poll tick moves a row to its new group immediately), EXCEPT rows the
    // user hand-toggled, which keep their pinned spot. Stable within each group.
    const rank = (r: EvidenceRow) =>
      pinnedRank.current.get(r.doi) ??
      RANK[r.screen_state ?? "screened_out"] ?? 2;
    return out
      .map((r, i) => ({ r, i }))
      .sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i)
      .map((d) => d.r);
  }, [rows, query, filter, sort]);

  // Render only the first `visibleCount` rows; scrolling the sentinel into view
  // grows the window. Reset to one page whenever the filter/search/sort changes
  // the result set, so a new query starts at the top with a fresh 100.
  useEffect(() => setVisibleCount(PAGE_SIZE), [query, filter, sort]);
  const paged = filtered.slice(0, visibleCount);
  const hasMore = visibleCount < filtered.length;

  // Auto-load the next page when the bottom sentinel scrolls into view. Observe
  // the row itself; `rootMargin` starts the load a little before it's on screen
  // so the fill-in feels seamless rather than catching up after a pause.
  const sentinelRef = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    if (!hasMore) return;
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setVisibleCount((c) => Math.min(c + PAGE_SIZE, filtered.length));
        }
      },
      { rootMargin: "400px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore, filtered.length]);

  // Cycle a column's sort: asc → desc → off. Clicking a different column starts
  // fresh at ascending.
  const toggleSort = (key: string) =>
    setSort((s) =>
      !s || s.key !== key
        ? { key, dir: "asc" }
        : s.dir === "asc"
          ? { key, dir: "desc" }
          : null,
    );

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <h2 className="text-sm font-semibold">
          Papers found
          <span className="ml-1.5 font-normal text-muted-foreground">
            {rows.length.toLocaleString()} paper{rows.length === 1 ? "" : "s"},{" "}
            {includedCount.toLocaleString()} included,{" "}
            {excludedCount.toLocaleString()} excluded
          </span>
        </h2>

        <div className="relative ml-2">
          <Search size={13} className="absolute left-2 top-1.5 text-muted-foreground" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search…"
            className="w-48 rounded border border-border bg-background py-1 pl-7 pr-2 text-xs placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>

        <div className="flex gap-1 text-xs">
          {(["all", "extracted", "needs_review", "excluded"] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`rounded px-2 py-1 ${
                filter === f
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {f === "needs_review" ? "Needs review" : titleCase(f)}
            </button>
          ))}
        </div>

        <div className="relative ml-auto">
          <button
            type="button"
            onClick={() => setShowPicker((v) => !v)}
            className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <Columns3 size={13} />
            Columns
          </button>
          {showPicker && (
            <div className="absolute right-0 z-10 mt-1 w-52 rounded-md border border-border bg-card p-2 shadow-lg">
              {COLUMNS.map((c) => (
                <label key={c.id} className="flex items-center gap-2 px-1 py-1 text-xs">
                  <input
                    type="checkbox"
                    checked={visible.has(c.id)}
                    onChange={() => toggleColumn(c.id)}
                    className="h-3.5 w-3.5 accent-primary"
                  />
                  {c.label}
                </label>
              ))}
            </div>
          )}
        </div>
      </header>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-border">
              <th className="w-28 px-2 text-center font-semibold text-muted-foreground">
                Include / Exclude
              </th>
              {shown.map((c) => {
                const dim = STAGE_ORDER.indexOf(c.stageNeeded) > reachedIdx;
                const sortable = !!c.sortValue;
                const active = sort?.key === c.id;
                return (
                  <th
                    key={c.id}
                    className={`whitespace-nowrap px-2 py-1.5 text-left font-semibold ${
                      dim ? "text-muted-foreground/40" : "text-muted-foreground"
                    }`}
                    title={dim ? `Fills in during the ${c.stageNeeded} phase` : undefined}
                    aria-sort={
                      active ? (sort!.dir === "asc" ? "ascending" : "descending") : undefined
                    }
                  >
                    {sortable ? (
                      <button
                        type="button"
                        onClick={() => toggleSort(c.id)}
                        className={`-mx-1 inline-flex items-center gap-1 rounded px-1 py-0.5 hover:text-foreground ${
                          active ? "text-foreground" : ""
                        }`}
                        title={`Sort by ${c.label}`}
                      >
                        {c.label}
                        {active ? (
                          sort!.dir === "asc" ? (
                            <ArrowUp size={12} className="shrink-0" />
                          ) : (
                            <ArrowDown size={12} className="shrink-0" />
                          )
                        ) : (
                          <ChevronsUpDown
                            size={12}
                            className="shrink-0 opacity-40 group-hover:opacity-100"
                          />
                        )}
                      </button>
                    ) : (
                      c.label
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {paged.map((r) => (
              <EvidenceRowView
                key={r.doi}
                row={r}
                columns={shown}
                isSelected={selected === r.doi}
                onOpen={() => setSelected(r.doi)}
                onExclude={() => { pinRow(r.doi); onExclude(r.doi); }}
                onInclude={() => { pinRow(r.doi); onInclude(r.doi); }}
                isMutating={isMutating}
              />
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={shown.length + 1} className="py-8 text-center text-muted-foreground">
                  {rows.length === 0
                    ? "No papers yet — they appear as the search runs."
                    : "No papers match this filter."}
                </td>
              </tr>
            )}
            {/* Infinite-scroll sentinel: scrolling this into view grows the
                window. A manual button rides along as a fallback for when the
                observer can't fire (e.g. the whole list fits without scrolling
                on a very tall viewport). */}
            {hasMore && (
              <tr ref={sentinelRef}>
                <td colSpan={shown.length + 1} className="py-4 text-center">
                  <button
                    type="button"
                    onClick={() =>
                      setVisibleCount((c) => Math.min(c + PAGE_SIZE, filtered.length))
                    }
                    className="inline-flex items-center gap-1.5 rounded border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                  >
                    <Loader2 size={13} className="animate-spin" />
                    Loading more… ({paged.length} of {filtered.length})
                  </button>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {filtered.length > PAGE_SIZE && !hasMore && (
        <footer className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
          <span className="tabular-nums">
            Showing all {filtered.length.toLocaleString()} papers
          </span>
        </footer>
      )}

      {selectedRow && (
        <PaperDetailPanel
          key={selectedRow.doi || selectedRow.title || undefined}
          reviewId={reviewId}
          row={selectedRow}
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  );
}

function EvidenceRowView({
  row,
  columns,
  isSelected,
  onOpen,
  onExclude,
  onInclude,
  isMutating,
}: {
  row: EvidenceRow;
  columns: typeof COLUMNS;
  isSelected: boolean;
  onOpen: () => void;
  onExclude: () => void;
  onInclude: () => void;
  isMutating?: boolean;
}) {
  // Screening verdict tints the whole row, keyed on the server's authoritative
  // `screen_state`: included → light green, pending (awaiting the full-text
  // eligibility verdict) → light amber, excluded → light red, screened-out →
  // neutral. The selected-row highlight wins over all. Opacity ramps keep the
  // tint readable in dark mode, where a fixed *-50 would glare.
  const state = row.screen_state ?? (row.is_excluded ? "excluded" : "screened_out");
  const verdictTint = isSelected
    ? "bg-primary/5"
    : state === "excluded"
      ? "bg-red-500/10 hover:bg-red-500/[0.15]"
      : state === "included"
        ? "bg-green-500/10 hover:bg-green-500/[0.15]"
        : state === "pending"
          ? "bg-amber-500/10 hover:bg-amber-500/[0.15]"
          : "hover:bg-foreground/5";

  return (
    <tr
      onClick={onOpen}
      // Keyboard access to the detail drawer: the row is focusable and opens
      // on Enter/Space. Only when the ROW itself has focus — a keypress on the
      // include/exclude pill inside it must not also open the drawer.
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      aria-label={`Open details: ${stripTags(row.title) || row.doi || "untitled paper"}`}
      aria-selected={isSelected}
      className={`group cursor-pointer border-b border-border/50 align-top focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary ${verdictTint} ${
        row.is_excluded ? "opacity-60" : ""
      }`}
    >
      {/* Three-state screening pill in the FIRST column. Stops propagation so it
          doesn't open the detail panel. Reflects the CURRENT verdict; clicking
          flips the manual override, and the row recolors instantly via the
          optimistic cache update. A screened-OUT paper (didn't pass relevance)
          reads muted and only offers Exclude — you can't hand-"include" an
          off-topic paper into extraction (the pipeline gate reads relevance, not
          this file). */}
      <td className="px-2 py-2 text-center" onClick={(e) => e.stopPropagation()}>
        {state === "excluded" ? (
          // Rest: red "Excluded". Hover: green "Include" — the label previews
          // the click's EFFECT, so the toggle is never a surprise.
          <button
            type="button"
            onClick={onInclude}
            disabled={isMutating}
            className="group/pill inline-flex min-w-[4.75rem] items-center justify-center gap-1 rounded-full border border-red-500/40 bg-red-500/10 px-2 py-0.5 text-[11px] font-medium text-red-600 hover:bg-green-500/10 hover:border-green-500/40 hover:text-green-600 disabled:opacity-50 dark:text-red-400 dark:hover:text-green-400"
            title={
              row.exclusion_reason
                ? `Excluded: ${row.exclusion_reason} — click to include`
                : "Excluded — click to include"
            }
          >
            <span className="inline-flex items-center gap-1 group-hover/pill:hidden">
              <X size={12} className="shrink-0" />
              Excluded
            </span>
            <span className="hidden items-center gap-1 group-hover/pill:inline-flex">
              <Check size={12} className="shrink-0" />
              Include
            </span>
          </button>
        ) : state === "included" ? (
          // Rest: green "Included". Hover: red "Exclude".
          <button
            type="button"
            onClick={onExclude}
            disabled={isMutating}
            className="group/pill inline-flex min-w-[4.75rem] items-center justify-center gap-1 rounded-full border border-green-500/40 bg-green-500/10 px-2 py-0.5 text-[11px] font-medium text-green-600 hover:bg-red-500/10 hover:border-red-500/40 hover:text-red-600 disabled:opacity-50 dark:text-green-400 dark:hover:text-red-400"
            title={
              row.user_included
                ? "Included by you — click to exclude"
                : "Included — click to exclude from extraction"
            }
          >
            <span className="inline-flex items-center gap-1 group-hover/pill:hidden">
              <Check size={12} className="shrink-0" />
              Included
            </span>
            <span className="hidden items-center gap-1 group-hover/pill:inline-flex">
              <X size={12} className="shrink-0" />
              Exclude
            </span>
          </button>
        ) : state === "pending" ? (
          // Awaiting the full-text eligibility verdict. The label reads Pending;
          // the user can decide NOW with the two small actions (which force the
          // verdict and short-circuit the background screen for this paper).
          <span className="inline-flex items-center gap-1">
            <span
              className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-600 dark:text-amber-400"
              title="Screened in — the background full-text eligibility screen hasn't ruled yet. It will resolve to Included or Excluded, or decide now:"
            >
              <Loader2 size={11} className="shrink-0 animate-spin" />
              Pending
            </span>
            <DecideNow onInclude={onInclude} onExclude={onExclude} disabled={isMutating} />
          </span>
        ) : (
          // Screened out at relevance. Rest: neutral "Screened out". Hover:
          // green "Include" — clicking force-includes it into extraction (the
          // user's call overrides the automatic gate). min-w matches the wider
          // rest label so the button doesn't shrink under the cursor and
          // flicker the hover state.
          <button
            type="button"
            onClick={onInclude}
            disabled={isMutating}
            className="group/pill inline-flex min-w-[6rem] items-center justify-center gap-1 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[11px] font-medium text-muted-foreground hover:border-green-500/40 hover:bg-green-500/10 hover:text-green-600 disabled:opacity-50 dark:hover:text-green-400"
            title="Screened out at relevance — not going to extraction. Click to include it anyway."
          >
            <span className="group-hover/pill:hidden">Screened out</span>
            <span className="hidden items-center gap-1 group-hover/pill:inline-flex">
              <Check size={12} className="shrink-0" />
              Include
            </span>
          </button>
        )}
      </td>
      {columns.map((c) => (
        <td key={c.id} className={`px-2 py-2 ${c.className ?? ""}`}>
          <span className={c.id === "title" ? "text-foreground" : "text-foreground/80"}>
            {c.get(row)}
          </span>
        </td>
      ))}
    </tr>
  );
}

/** Two tiny ✓/✗ actions beside a Pending pill: decide a paper's inclusion NOW
 *  instead of waiting for the background full-text screen. A manual decision
 *  overrides the automatic verdict and short-circuits the screen for this paper. */
function DecideNow({
  onInclude,
  onExclude,
  disabled,
}: {
  onInclude: () => void;
  onExclude: () => void;
  disabled?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-0.5">
      <button
        type="button"
        onClick={onInclude}
        disabled={disabled}
        className="rounded-full border border-border p-0.5 text-muted-foreground hover:border-green-500/40 hover:bg-green-500/10 hover:text-green-600 disabled:opacity-50 dark:hover:text-green-400"
        title="Include now (don't wait for the full-text screen)"
      >
        <Check size={11} className="shrink-0" />
      </button>
      <button
        type="button"
        onClick={onExclude}
        disabled={disabled}
        className="rounded-full border border-border p-0.5 text-muted-foreground hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-600 disabled:opacity-50 dark:hover:text-red-400"
        title="Exclude now"
      >
        <X size={11} className="shrink-0" />
      </button>
    </span>
  );
}

/** Full-text column: a check when we have the text, otherwise the ✗.
 *  Manual PDF retrieval lives on the dedicated /missing-pdfs page (reachable from
 *  the "missing PDFs" link above the table), so there's no per-row hover link. */
function FullTextCell({ row }: { row: EvidenceRow }) {
  if (row.has_fulltext) return <span className="text-green-600">✓</span>;
  return <span className="text-muted-foreground">✗</span>;
}

/** Human-readable organism label for the column. */
function organismLabel(o: string): string {
  const map: Record<string, string> = {
    human: "Human",
    mouse: "Mouse",
    other_animal: "Other animal",
    other: "Other",
  };
  return map[o] ?? titleCase(o);
}

function leadAuthor(authors?: string): string {
  const raw = (authors || "").trim();
  if (!raw) return "—";
  const first = raw.split(/;|,| and /)[0].trim();
  const surname = fixAuthorCaps(
    first.includes(" ") ? first.split(/\s+/).slice(-1)[0] : first,
  );
  return raw.match(/;| and |,/) ? `${surname} et al.` : surname;
}
