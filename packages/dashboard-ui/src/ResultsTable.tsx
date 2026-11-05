"use client";

/** The trials table: sortable, searchable, with a real citation line and a
 *  separate mobile card layout.
 *
 *  Presentation ported from the website's per-topic `ResultsTable.tsx`, retyped
 *  against the canonical `TrialRow` so one table serves every topic instead of
 *  each dashboard carrying its own near-identical copy.
 *
 *  Columns are a registry (`RESULT_COLUMNS`), not hand-written JSX: the studio's
 *  dashboard editor persists a per-review selection (`columns` prop, from
 *  config via the manifest), while read-only hosts still get a local, in-memory
 *  column picker. Desktop cells, header, mobile chips, and the empty-state
 *  colSpan all derive from the same registry, so a column cannot half-exist. */

import { ExternalLink } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { leadAuthor } from "./adapters";
import { VERDICT_LABEL } from "./labels";
import type { DashboardEdit } from "./layout";
import { representativeStudies, type DashboardSummary, type TrialRow } from "./summary";
import { formatLabel } from "./utils";

/** Tinted with alpha over the host background so the badges read correctly in
 *  both themes (the website's `bg-green-50` is light-mode-only). */
const VERDICT_STYLE: Record<string, string> = {
  favors_treatment: "bg-green-500/10 text-green-600 border-green-500/30",
  favors_control: "bg-red-500/10 text-red-600 border-red-500/30",
  no_difference: "bg-foreground/[0.05] text-foreground/60 border-border",
  mixed: "bg-amber-500/10 text-amber-600 border-amber-500/30",
  insufficient_data: "bg-amber-500/10 text-amber-700 border-border",
  inconclusive: "bg-foreground/[0.03] text-foreground/45 border-border",
  unknown: "bg-foreground/[0.03] text-foreground/45 border-border",
};

type SortKey =
  | "year"
  | "title"
  | "design_type"
  | "n"
  | "rob"
  | "verdict"
  | "primary_p_value"
  // Optional on TrialRow (absent from pre-column bundles). SORT_VALUE returns
  // null for them — sorted last — so an old bundle still sorts.
  | "organism"
  | "primary_name"
  | "comparator"
  | "follow_up_weeks"
  // PICO columns (schema v3+), read via `pico`.
  | "population"
  | "intervention"
  | "outcomes"
  // Editor-era optional columns.
  | "blinding"
  | "outcome_domain"
  | "outcome_type"
  | "effect"
  | "countries";

const joinLower = (xs: string[] | null | undefined) =>
  xs && xs.length ? xs.join(", ").toLowerCase() : null;

// Risk of bias sorts by severity, not alphabetically. Covers both RoB2
// (some_concerns/high) and ROBINS-I (moderate/serious/critical) vocabularies;
// "unknown" is missing data and sorts last via null.
const ROB_RANK: Record<string, number> = {
  low: 0, some_concerns: 1, moderate: 1, high: 2, serious: 2, critical: 3,
};

/** One sort accessor per column, so every column sorts — including the nested
 *  PICO fields a direct `row[key]` lookup can't reach. Missing values map to
 *  null and always sort last regardless of direction. */
const SORT_VALUE: Record<SortKey, (r: TrialRow) => string | number | null> = {
  year: (r) => r.year,
  title: (r) => r.title?.toLowerCase() || null,
  design_type: (r) => r.design_type?.toLowerCase() || null,
  organism: (r) => r.organism ?? null,
  population: (r) => r.pico?.population?.toLowerCase() ?? null,
  intervention: (r) => joinLower(r.pico?.intervention),
  comparator: (r) => joinLower(r.comparator ?? r.pico?.comparator),
  primary_name: (r) => r.primary_name?.toLowerCase() ?? null,
  outcomes: (r) => joinLower(r.pico?.outcome),
  n: (r) => r.n,
  follow_up_weeks: (r) => r.follow_up_weeks ?? null,
  rob: (r) => ROB_RANK[r.rob] ?? null,
  verdict: (r) => r.verdict || null,
  primary_p_value: (r) => r.primary_p_value,
  blinding: (r) => r.blinding?.toLowerCase() || null,
  outcome_domain: (r) => r.primary_domain?.toLowerCase() || null,
  outcome_type: (r) => r.outcome_type?.toLowerCase() || null,
  effect: (r) => r.eff?.value ?? null,
  countries: (r) => joinLower(r.countries),
};

// Text columns start ascending (A→Z); numeric/severity ones start descending.
const ASC_FIRST = new Set<SortKey>([
  "title", "design_type", "organism", "population", "intervention",
  "comparator", "primary_name", "outcomes", "blinding", "outcome_domain",
  "outcome_type", "countries",
]);

/** Render a p-value at the precision journals actually report it.
 *
 * Very small values become "<0.001" rather than "0.000", which would read as
 * exactly zero; anything at or above 0.001 keeps three decimals so 0.04 and
 * 0.049 stay distinguishable. Null (not reported) is an em dash, never "1.0" —
 * an unreported p-value is missing data, not a null result.
 */
function formatP(p: number | null | undefined, operator?: string | null): string {
  if (p == null) return "—";
  const value = p !== 0 && p < .001 ? p.toExponential(2) : p.toFixed(3);
  return `${operator ?? "(operator unknown)"} ${value}`;
}

/** "SMD −0.42 [−0.71, −0.13]" — the primary effect in one cell. */
function formatEff(eff: TrialRow["eff"]): string {
  if (!eff || eff.value == null) return "—";
  const ci =
    eff.ci_low != null && eff.ci_high != null
      ? ` [${eff.ci_low.toFixed(2)}, ${eff.ci_high.toFixed(2)}]`
      : "";
  return `${eff.measure ? `${eff.measure} ` : ""}${eff.value.toFixed(2)}${ci}`;
}

const clamp2 = (text: string | null | undefined) => (
  <span className="line-clamp-2">{text || "—"}</span>
);

export interface ColumnDef {
  key: string;
  label: string;
  /** Absent = the header is a plain (unsortable) label. */
  sortKey?: SortKey;
  align?: "left" | "right";
  /** Overrides the default `<td>` classes (max-widths, tabular-nums, …). */
  tdClass?: string;
  /** The cell's `title` attribute (full text behind a line-clamp). */
  title?: (r: TrialRow) => string;
  cell: (r: TrialRow) => ReactNode;
  /** Chip in the mobile card's meta line; columns without one are desktop-only
   *  (long free-text is hostile to cards). Return null to skip for a row. */
  mobile?: (r: TrialRow) => ReactNode;
}

/** Every column the table can render, in display order. Keys are the contract
 *  with `dashboard.columns` in review config and `results_columns` in the
 *  manifest (see generate_dashboard.py DEFAULT_COLUMNS). */
export const RESULT_COLUMNS: ColumnDef[] = [
  {
    key: "year", label: "Year", sortKey: "year",
    tdClass: "px-2 py-2 tabular-nums text-foreground/70",
    cell: (r) => r.year ?? "—",
  },
  {
    key: "study", label: "Study", sortKey: "title",
    tdClass: "max-w-xl px-2 py-2",
    cell: (r) => <CitationLine row={r} />,
  },
  {
    key: "population", label: "Population", sortKey: "population",
    tdClass: "max-w-[16rem] px-2 py-2 text-foreground/70",
    title: (r) => r.pico?.population ?? "",
    cell: (r) => clamp2(r.pico?.population),
  },
  {
    key: "design", label: "Design", sortKey: "design_type",
    cell: (r) => formatLabel(r.design_type),
    mobile: (r) => <span>{formatLabel(r.design_type)}</span>,
  },
  {
    key: "organism", label: "Organism", sortKey: "organism",
    cell: (r) => (r.organism ? formatLabel(r.organism) : "—"),
    mobile: (r) => (r.organism ? <span>{formatLabel(r.organism)}</span> : null),
  },
  {
    key: "intervention", label: "Intervention", sortKey: "intervention",
    tdClass: "max-w-[12rem] px-2 py-2 text-foreground/70",
    title: (r) => (r.pico?.intervention ?? []).join(", "),
    cell: (r) =>
      clamp2(r.pico?.intervention?.length ? r.pico.intervention.join(", ") : null),
    // "Intervention vs comparator" in one breath on mobile.
    mobile: (r) =>
      r.pico?.intervention?.length || r.comparator?.length ? (
        <span>
          {r.pico?.intervention?.length ? r.pico.intervention.join(", ") : ""}
          {!!r.comparator?.length && ` vs ${r.comparator.join(", ")}`}
        </span>
      ) : null,
  },
  {
    key: "comparator", label: "Comparator", sortKey: "comparator",
    tdClass: "max-w-[12rem] px-2 py-2 text-foreground/70",
    title: (r) => (r.comparator ?? r.pico?.comparator ?? []).join(", "),
    cell: (r) => {
      const c = r.comparator ?? r.pico?.comparator;
      return clamp2(c?.length ? c.join(", ") : null);
    },
  },
  {
    key: "primary_outcome", label: "Primary outcome", sortKey: "primary_name",
    tdClass: "max-w-[16rem] px-2 py-2 text-foreground/70",
    title: (r) => r.primary_name ?? "",
    cell: (r) => clamp2(r.primary_name),
  },
  {
    key: "outcomes", label: "All outcomes", sortKey: "outcomes",
    tdClass: "max-w-[16rem] px-2 py-2 text-foreground/70",
    title: (r) => (r.pico?.outcome ?? []).join("; "),
    cell: (r) =>
      clamp2(r.pico?.outcome?.length ? r.pico.outcome.join("; ") : null),
  },
  {
    key: "n", label: "N", sortKey: "n", align: "right",
    tdClass: "px-2 py-2 text-right tabular-nums text-foreground/70",
    cell: (r) => r.n?.toLocaleString() ?? "—",
    mobile: (r) =>
      r.n != null ? (
        <span className="tabular-nums">N = {r.n.toLocaleString()}</span>
      ) : null,
  },
  {
    key: "follow_up", label: "Follow-up", sortKey: "follow_up_weeks", align: "right",
    tdClass: "px-2 py-2 text-right tabular-nums text-foreground/70",
    cell: (r) => (r.follow_up_weeks != null ? `${r.follow_up_weeks} wk` : "—"),
    mobile: (r) =>
      r.follow_up_weeks != null ? (
        <span className="tabular-nums">{r.follow_up_weeks} wk</span>
      ) : null,
  },
  {
    key: "rob", label: "Risk of bias", sortKey: "rob",
    cell: (r) => formatLabel(r.rob),
    mobile: (r) => <span>RoB: {formatLabel(r.rob)}</span>,
  },
  {
    key: "blinding", label: "Blinding", sortKey: "blinding",
    cell: (r) => (r.blinding ? formatLabel(r.blinding) : "—"),
  },
  {
    key: "outcome_domain", label: "Outcome domain", sortKey: "outcome_domain",
    cell: (r) => (r.primary_domain ? formatLabel(r.primary_domain) : "—"),
  },
  {
    key: "outcome_type", label: "Outcome type", sortKey: "outcome_type",
    cell: (r) => (r.outcome_type ? formatLabel(r.outcome_type) : "—"),
  },
  {
    key: "effect", label: "Effect", sortKey: "effect", align: "right",
    tdClass: "px-2 py-2 text-right tabular-nums text-foreground/70",
    title: (r) => (r.eff?.p != null ? `p ${formatP(r.eff.p, r.eff.p_operator)}` : ""),
    cell: (r) => formatEff(r.eff),
  },
  {
    key: "countries", label: "Countries", sortKey: "countries",
    tdClass: "max-w-[10rem] px-2 py-2 text-foreground/70",
    title: (r) => r.countries.join(", "),
    cell: (r) => clamp2(r.countries.length ? r.countries.join(", ") : null),
  },
  {
    key: "p", label: "p", sortKey: "primary_p_value", align: "right",
    tdClass: "px-2 py-2 text-right tabular-nums text-foreground/70",
    title: (r) =>
      r.primary_p_value == null
        ? "No p-value reported for the primary outcome"
        : `p ${formatP(r.primary_p_value, r.eff?.p_operator)}`,
    cell: (r) => formatP(r.primary_p_value, r.eff?.p_operator),
    mobile: (r) =>
      r.primary_p_value != null ? (
        <span className="tabular-nums">p {formatP(r.primary_p_value, r.eff?.p_operator)}</span>
      ) : null,
  },
  {
    key: "result", label: "Result", sortKey: "verdict",
    cell: (r) => <VerdictBadge row={r} />,
    mobile: (r) => <VerdictBadge row={r} />,
  },
];

/** What renders when neither config nor the manifest chooses. Mirrors
 *  generate_dashboard.py DEFAULT_COLUMNS — the pre-editor default view. */
export const DEFAULT_COLUMN_KEYS: string[] = [
  "year", "study", "design", "organism", "intervention", "comparator",
  "primary_outcome", "n", "follow_up", "rob", "p", "result",
];

export function ResultsTable({
  rows,
  summary,
  pageSize = 100,
  columns,
  edit,
}: {
  rows: TrialRow[];
  summary: DashboardSummary;
  pageSize?: number;
  /** Persisted column selection (review config via the manifest). Absent →
   *  DEFAULT_COLUMN_KEYS. Unknown keys are ignored, never fatal. */
  columns?: string[];
  /** Studio edit mode: column toggles persist through this instead of local
   *  state. Read-only hosts keep an in-memory picker. */
  edit?: DashboardEdit;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("year");
  const [dir, setDir] = useState<"asc" | "desc">("desc");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(pageSize);
  // Local column tweaks for read-only hosts (no persistence: this package also
  // serves the public website, so it can't reach the studio's config store).
  const [localCols, setLocalCols] = useState<string[] | null>(null);
  const [showCols, setShowCols] = useState(false);

  const selected = edit
    ? (columns ?? DEFAULT_COLUMN_KEYS)
    : (localCols ?? columns ?? DEFAULT_COLUMN_KEYS);
  const cols = RESULT_COLUMNS.filter((c) => selected.includes(c.key));

  const toggleColumn = (key: string) => {
    if (edit) {
      edit.onToggleColumn(key);
      return;
    }
    setLocalCols(
      selected.includes(key)
        ? selected.filter((k) => k !== key)
        : RESULT_COLUMNS.map((c) => c.key).filter(
            (k) => selected.includes(k) || k === key,
          ),
    );
  };

  const searched = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (r) =>
        r.title.toLowerCase().includes(q) ||
        r.journal.toLowerCase().includes(q) ||
        (r.authors ?? "").toLowerCase().includes(q) ||
        r.doi.toLowerCase().includes(q),
    );
  }, [rows, query]);

  // If the active sort's column was removed, fall back to the first sortable
  // visible column (pure compute — no effect, no stale state).
  const effectiveSortKey = cols.some((c) => c.sortKey === sortKey)
    ? sortKey
    : (cols.find((c) => c.sortKey)?.sortKey ?? "year");

  const sorted = useMemo(() => {
    const mult = dir === "asc" ? 1 : -1;
    const getV = SORT_VALUE[effectiveSortKey];
    // Copy before sorting: mutating would reorder the array the charts' hover
    // panels index into, silently re-pointing tiles at the wrong studies.
    return [...searched].sort((a, b) => {
      const av = getV(a);
      const bv = getV(b);
      // Nulls always last, regardless of direction.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * mult;
      }
      return String(av).localeCompare(String(bv)) * mult;
    });
  }, [searched, effectiveSortKey, dir]);

  const visible = sorted.slice(0, shown);

  function sortBy(key: SortKey) {
    if (key === effectiveSortKey) {
      setDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setDir(ASC_FIRST.has(key) ? "asc" : "desc");
    }
  }

  const mobileCols = cols.filter((c) => c.mobile);

  return (
    <section className="mb-6 rounded-lg border border-border bg-card p-3 sm:p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">
          Trials
          <span className="font-normal text-foreground/60">
            {" "}
            (n = {representativeStudies(rows).length.toLocaleString()}{representativeStudies(rows).length !== rows.length ? `; ${rows.length} reports` : ""})
          </span>
        </h2>
        <div className="flex items-center gap-2">
          <input
            type="search"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setShown(pageSize);
            }}
            placeholder="Search title, author, journal, DOI…"
            className="w-full max-w-xs rounded border border-border bg-background px-2 py-1 text-sm
                       placeholder:text-foreground/40 focus:outline-none focus:ring-1 focus:ring-primary"
          />
          {/* Column picker (desktop only — the mobile cards have a compact
              layout). In studio edit mode the checkboxes persist to the review's
              config; elsewhere they are an in-memory view tweak. */}
          <div className="relative hidden md:block">
            <button
              type="button"
              onClick={() => setShowCols((v) => !v)}
              className="rounded border border-border px-2 py-1 text-sm text-foreground/60 hover:text-foreground"
            >
              {edit ? "Edit columns" : "Columns"}
            </button>
            {showCols && (
              <div className="absolute right-0 z-10 mt-1 max-h-80 w-52 overflow-y-auto rounded-md border border-border bg-card p-2 shadow-lg">
                {RESULT_COLUMNS.map((c) => {
                  const on = selected.includes(c.key);
                  return (
                    <label
                      key={c.key}
                      className="flex items-center gap-2 px-1 py-1 text-xs"
                    >
                      <input
                        type="checkbox"
                        checked={on}
                        // Never uncheck the last column — a zero-column table
                        // is a rendering black hole, not a choice.
                        disabled={on && cols.length === 1}
                        onChange={() => toggleColumn(c.key)}
                        className="h-3.5 w-3.5 accent-primary"
                      />
                      {c.label}
                    </label>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {query && (
        <p className="mb-2 text-xs text-foreground/50">
          {searched.length.toLocaleString()} match
          {searched.length === 1 ? "" : "es"}.
        </p>
      )}

      {/* Desktop */}
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              {cols.map((c) =>
                c.sortKey ? (
                  <SortTh
                    key={c.key}
                    label={c.label}
                    k={c.sortKey}
                    active={effectiveSortKey}
                    dir={dir}
                    onSort={sortBy}
                    align={c.align}
                  />
                ) : (
                  <th
                    key={c.key}
                    className={`whitespace-nowrap border-b border-border px-2 py-1.5 text-${c.align ?? "left"} text-xs font-semibold text-foreground/60`}
                  >
                    {c.label}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr
                key={r.id}
                className="border-b border-border/50 align-top hover:bg-foreground/5"
              >
                {cols.map((c) => (
                  <td
                    key={c.key}
                    className={c.tdClass ?? "px-2 py-2 text-foreground/70"}
                    title={c.title?.(r) || undefined}
                  >
                    {c.cell(r)}
                  </td>
                ))}
              </tr>
            ))}
            {!visible.length && (
              <tr>
                <td
                  colSpan={cols.length}
                  className="py-6 text-center text-sm text-foreground/45"
                >
                  No trials match the current filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Mobile */}
      <div className="space-y-3 md:hidden">
        {visible.map((r) => (
          <div key={r.id} className="rounded border border-border p-2.5">
            <CitationLine row={r} />
            {selected.includes("primary_outcome") && r.primary_name && (
              <p className="mt-1 text-xs text-foreground/70">{r.primary_name}</p>
            )}
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-foreground/60">
              {mobileCols.map((c) => {
                const chip = c.mobile!(r);
                return chip == null ? null : (
                  <span key={c.key} className="contents">
                    {chip}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
        {!visible.length && (
          <p className="py-6 text-center text-sm text-foreground/45">
            No trials match the current filters.
          </p>
        )}
      </div>

      {sorted.length > shown && (
        <button
          type="button"
          onClick={() => setShown((n) => n + pageSize)}
          className="mt-3 w-full rounded border border-border py-1.5 text-sm text-foreground/70 hover:bg-foreground/5"
        >
          Show more ({(sorted.length - shown).toLocaleString()} remaining)
        </button>
      )}
    </section>
  );
}

function SortTh({
  label,
  k,
  active,
  dir,
  onSort,
  align = "left",
}: {
  label: string;
  k: SortKey;
  active: SortKey;
  dir: "asc" | "desc";
  onSort: (k: SortKey) => void;
  align?: "left" | "right";
}) {
  const on = active === k;
  return (
    <th
      className={`whitespace-nowrap border-b border-border px-2 py-1.5 text-${align} text-xs font-semibold text-foreground/60`}
    >
      <button
        type="button"
        onClick={() => onSort(k)}
        className="inline-flex items-center gap-1 hover:text-foreground"
      >
        {label}
        <span className="text-[10px] text-foreground/40">
          {on ? (dir === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

/** Author. **Title**↗ *Journal*. (Year) — the academic reference the website
 *  renders, so a trial row is citable rather than just a title string. */
function CitationLine({ row }: { row: TrialRow }) {
  const author = leadAuthor(row.authors);
  return (
    <div className="leading-snug">
      {author && <span className="text-foreground/70">{author} </span>}
      <a
        href={`https://doi.org/${row.doi}`}
        target="_blank"
        rel="noreferrer"
        className="font-medium text-blue-600 hover:underline"
      >
        {row.title || row.doi}
        <ExternalLink size={10} className="ml-0.5 inline align-baseline" />
      </a>
      {row.journal && (
        <span className="text-foreground/55"> {row.journal}.</span>
      )}
      {row.year != null && (
        <span className="text-foreground/55"> ({row.year})</span>
      )}
    </div>
  );
}

function VerdictBadge({ row }: { row: TrialRow }) {
  const style = VERDICT_STYLE[row.verdict] ?? VERDICT_STYLE["unknown"]!;
  return (
    <span
      className={`inline-block rounded border px-1.5 py-0.5 text-[11px] leading-tight ${style}`}
      title={
        row.verdict_source === "proxy"
          ? row.result_details?.reason ?? "Inferred from extracted comparisons"
          : undefined
      }
    >
      {VERDICT_LABEL[row.verdict] ?? formatLabel(row.verdict)}
      {/* A proxy verdict was derived from the numbers rather than reviewed, so
          it is marked rather than presented with equal confidence. */}
      {row.verdict_source === "proxy" && (
        <span className="ml-0.5 opacity-60">~</span>
      )}
    </span>
  );
}
