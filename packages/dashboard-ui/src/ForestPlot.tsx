"use client";

import type { ReactNode } from "react";
import { fmt } from "./utils";
import { prettyMeasure, prettyDomain, type DomainLabels } from "./labels";
import { trialHref } from "./links";

/** A single trial estimate on a forest plot. */
export interface ForestTrial {
  paper_id: string;
  label: string; // "Author 2021" (falls back to DOI)
  doi?: string;
  first_author?: string;
  year?: number | null;
  effect: number;
  ci_low: number | null;
  ci_high: number | null;
  p_value: number | null;
  n_total: number | null;
  weight: number | null; // pooled weight (0..1) or null when not pooled
  outcome_name: string | null;
  /** True when this trial's effect was inverted to match the group's reference orientation. */
  oriented_flipped?: boolean;
}

export interface ForestPooled {
  effect: number;
  ci_low: number;
  ci_high: number;
  i2: number;
  model: string;
  tau2?: number;
  ci_method?: string;
  level?: number;
  pred_low?: number | null;
  pred_high?: number | null;
}

/** One compound's rows + subtotal inside a cross-compound plot. */
export interface ForestSubgroup {
  ingredient: string;
  n_trials: number;
  trials: ForestTrial[];
  pooled?: ForestPooled | null;
}

/** A study that reports the outcome but could not be standardized (shown in the
 *  transparency appendix under a cross-compound plot, never silently dropped). */
export interface ForestExcluded {
  paper_id: string;
  first_author?: string;
  year?: number | string | null;
  ingredient?: string;
  effect_measure?: string | null;
  reason?: string;
}

export interface ForestGroup {
  rules_version?: string;
  unit?: string;
  basis?: string;
  comparator?: string;
  method_dependent?: boolean;
  ci_sensitivity?: Record<string, { effect: number; ci_low: number; ci_high: number; p_value: number | null }>;
  small_study_note?: string;
  ingredient: string;
  outcome_domain: string;
  /** Optional display label that overrides prettyDomain(outcome_domain) for this
   *  specific group (e.g. "Influenza symptom duration" for zanamivir). */
  domainLabel?: string;
  effect_measure: string;
  scale: "log" | "linear";
  n_trials: number;
  trials: ForestTrial[];
  pooled?: ForestPooled | null;
  /** "rct" | "observational" — observational pools are never mixed with RCTs. */
  design_class?: string;
  /** CI method for the pooled estimate: "hksj" (Hartung-Knapp) or "dl_normal". */
  ci_method?: string;
  total_n?: number | null;
  /** Count of trials by risk-of-bias category. */
  rob_summary?: { low: number; some: number; high: number; unknown: number };
  /** GRADE-style certainty of evidence. */
  certainty?: {
    grade: "high" | "moderate" | "low" | "very_low";
    start?: string;
    downgrades?: { domain: string; reason: string }[];
  };
  /** Sensitivity analyses (fixed-effect, leave-one-out, RoB-excluded re-pool). */
  sensitivity?: {
    fixed_effect?: { effect: number; ci_low: number; ci_high: number };
    leave_one_out?: { omitted: string; effect: number; ci_low: number; ci_high: number }[];
    rob_excluded?: { n_trials: number; effect: number; ci_low: number; ci_high: number; i2: number };
  };
  /** Small-study / publication-bias test (only computed at k >= threshold). */
  pub_bias?: { available: boolean; k: number; intercept?: number; t?: number; p?: number };
  /** Cross-compound (all-ingredient) standardized plot: pools every compound on
   *  one Cohen's-d scale, with per-compound subgroups + subtotals. */
  cross_ingredient?: boolean;
  n_compounds?: number;
  subgroups?: ForestSubgroup[];
  excluded?: ForestExcluded[];
  n_excluded?: number;
  /** Set on the unified (drug × outcome) standardized-mean-difference pool, which
   *  combines continuous (Hedges' g) and binary (Chinn OR→SMD) outcomes onto one
   *  comparable Cohen's-d scale. */
  standardized?: boolean;
  /** Underlying measure families folded into a standardized pool, e.g. ["continuous","odds_ratio"]. */
  combines?: string[];
  /** Number of trials whose effect was converted to SMD (vs natively continuous). */
  n_converted?: number;
  /** Reference orientation for the group; trials measured the other way are flipped. */
  reference_higher_is_better?: boolean;
  /** How many trials were inverted to match the group's reference orientation. */
  n_oriented_flipped?: number;
}

const prettyIngredient = (s: string) =>
  s ? s.charAt(0).toUpperCase() + s.slice(1) : s;

// Theme-safe tones (the `-500/10` + `-600` + `dark:` convention every other tone
// map in the package uses). The old `-100/-800` scale was light-mode only and
// rendered dark text on a near-white chip on a dark host.
const GRADE_STYLE: Record<string, { label: string; cls: string }> = {
  high: { label: "High certainty", cls: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400" },
  moderate: { label: "Moderate certainty", cls: "bg-lime-500/10 text-lime-700 dark:text-lime-400" },
  low: { label: "Low certainty", cls: "bg-amber-500/10 text-amber-700 dark:text-amber-400" },
  very_low: { label: "Very low certainty", cls: "bg-rose-500/10 text-rose-700 dark:text-rose-400" },
};

function CertaintyBadge({ grade }: { grade: string }) {
  const s = GRADE_STYLE[grade] ?? { label: grade, cls: "bg-foreground/10 text-foreground/70" };
  return (
    <span className={`text-[10px] font-semibold uppercase tracking-wide rounded px-1.5 py-0.5 ${s.cls}`}
      title="GRADE-style certainty of evidence">
      {s.label}
    </span>
  );
}

/** One-line rigor summary under the header: GRADE downgrade reasons, RoB mix,
 *  prediction interval, and any small-study-bias flag. */
function RigorLine({ group }: { group: ForestGroup }) {
  const c = group.certainty;
  const downs = (c?.downgrades ?? []).map((d) => d.reason);
  const rob = group.rob_summary;
  const robBits = rob
    ? [rob.low ? `${rob.low} low` : "", rob.some ? `${rob.some} some` : "", rob.high ? `${rob.high} high` : ""]
        .filter(Boolean).join(", ")
    : "";
  const pi = group.pooled?.pred_low != null && group.pooled?.pred_high != null
    ? `95% PI ${fmt(group.pooled.pred_low)} to ${fmt(group.pooled.pred_high)}`
    : "";
  const egger = group.pub_bias?.available && group.pub_bias.p != null && group.pub_bias.p < 0.1
    ? `small-study bias (Egger p=${group.pub_bias.p})` : "";
  const parts = [
    downs.length ? `Downgraded: ${downs.join("; ")}` : "No downgrades",
    robBits ? `RoB: ${robBits}` : "",
    pi, egger,
  ].filter(Boolean);
  return (
    <p className="mb-2 text-[11px] leading-snug text-foreground/55">{parts.join(" · ")}</p>
  );
}

/** A self-contained SVG forest plot for one (ingredient × outcome × measure)
 *  group: one row per trial (square sized by pooled weight + CI whisker), a
 *  null reference line, x-axis ticks, and a pooled diamond when available.
 *
 *  `domainLabels` is the dashboard's outcome-domain label map (see labels.ts);
 *  `group.domainLabel` still overrides it for a specific group when set. */
export function ForestPlot({
  group,
  domainLabels = {},
}: {
  group: ForestGroup;
  domainLabels?: DomainLabels;
}) {
  // Cross-compound (subgrouped) plots have their own richer renderer.
  if (group.subgroups && group.subgroups.length > 0) {
    return <CrossCompoundForestPlot group={group} domainLabels={domainLabels} />;
  }
  const isLog = group.scale === "log";
  const nullVal = isLog ? 1 : 0;
  const ratio = isLog;

  const rows = group.trials;
  const ROW_H = 30;
  const HEADER_H = 28;
  const AXIS_H = 42;
  const LABEL_W = 190; // left text column
  const VAL_W = 150; // right value column
  const PLOT_W = 360;
  const W = LABEL_W + PLOT_W + VAL_W;
  const plotLeft = LABEL_W;
  const plotRight = LABEL_W + PLOT_W;
  const diamondRows = group.pooled ? 1 : 0;
  const H = HEADER_H + rows.length * ROW_H + diamondRows * ROW_H + AXIS_H;

  // --- x domain across all finite CI bounds + point estimates + null line ---
  const vals: number[] = [nullVal];
  for (const t of rows) {
    for (const v of [t.effect, t.ci_low, t.ci_high]) if (v != null && Number.isFinite(v)) vals.push(v);
  }
  if (group.pooled) vals.push(group.pooled.effect, group.pooled.ci_low, group.pooled.ci_high);

  const tx = (v: number) => (isLog ? Math.log(Math.max(v, 1e-6)) : v);
  const txVals = vals.map(tx);
  let lo = Math.min(...txVals);
  let hi = Math.max(...txVals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  const xPix = (v: number) => plotLeft + ((tx(v) - lo) / (hi - lo)) * PLOT_W;

  // --- x-axis ticks ---
  const ratioTicks = [0.1, 0.25, 0.5, 1, 2, 4, 10];
  let ticks: number[];
  if (isLog) {
    ticks = ratioTicks.filter((t) => tx(t) >= lo && tx(t) <= hi);
    if (ticks.length < 2) ticks = [Math.exp(lo + (hi - lo) * 0.2), 1, Math.exp(lo + (hi - lo) * 0.8)];
  } else {
    const step = (hi - lo) / 4;
    ticks = [0, 1, 2, 3, 4].map((i) => lo + step * i);
  }

  const sqrtW = rows.map((t) => (t.weight != null ? Math.sqrt(t.weight) : null));
  const maxSqrt = Math.max(...sqrtW.filter((x): x is number => x != null), 1);
  const boxSize = (i: number) => {
    const s = sqrtW[i];
    if (s == null) return 7;
    return 5 + (s / maxSqrt) * 9; // 5..14 px half-size
  };

  const headerDomain = group.domainLabel ?? prettyDomain(group.outcome_domain, domainLabels);

  return (
    <div className="border border-border rounded-lg bg-card p-4 mb-6">
      <div className="mb-1 flex flex-wrap items-baseline gap-x-3">
        <h3 className="font-semibold text-foreground capitalize">{group.ingredient}</h3>
        <span className="text-sm text-foreground/70">{headerDomain}</span>
        {group.standardized && (
          <span className="text-[10px] font-semibold uppercase tracking-wide rounded px-1.5 py-0.5 bg-violet-100 text-violet-700">
            Standardized (SMD)
          </span>
        )}
        {group.design_class === "observational" && (
          <span className="text-[10px] font-semibold uppercase tracking-wide rounded px-1.5 py-0.5 bg-amber-100 text-amber-700">
            Observational
          </span>
        )}
        {group.certainty && <CertaintyBadge grade={group.certainty.grade} />}
        <span className="text-xs text-foreground/50">
          {prettyMeasure(group.effect_measure)} · {group.n_trials} trial{group.n_trials === 1 ? "" : "s"}
          {group.pooled ? ` · I² = ${group.pooled.i2}%` : " · not pooled"}
          {group.pooled?.ci_method === "hksj" ? " · Hartung–Knapp CI" : ""}
        </span>
      </div>
      {group.certainty && <RigorLine group={group} />}
      <p className="text-xs text-foreground/60 mb-2">{group.unit} · {group.basis} · CI: {group.pooled?.ci_method ?? group.ci_method} · Study intervals: 95%; pooled interval: {Math.round((group.pooled?.level ?? .95) * 100)}%</p>
      {group.ci_sensitivity && <details className="mb-3 text-xs"><summary>Small-study sensitivity{group.method_dependent ? " — conclusion depends on interval method" : ""}</summary>
        <p>{group.small_study_note} Ordinary HKSJ can give narrow intervals when heterogeneity is low; variance-floored HKSJ can be very conservative.</p>
        {Object.entries(group.ci_sensitivity).map(([method, result]) => <p key={method}>{method === "modified_hksj" ? "HKSJ (variance floor)" : method.toUpperCase()}: {fmt(result.effect)} [{fmt(result.ci_low)}, {fmt(result.ci_high)}]; p={fmt(result.p_value)}</p>)}
      </details>}
      {(group.standardized || (group.n_oriented_flipped ?? 0) > 0) && (
        <p className="mb-2 text-[11px] leading-snug text-foreground/55">
          {group.standardized && (
            <>Standardized mean difference for explicitly mapped measures of the same construct. Endpoint and change-score standardizations are analyzed separately. Direction follows the original outcome scale.</>
          )}
          {(group.n_oriented_flipped ?? 0) > 0 && (
            <>{group.n_oriented_flipped} trial{group.n_oriented_flipped === 1 ? " was" : "s were"} inverted (†)
            so all estimates share one direction (higher = better).</>
          )}
        </p>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} className="text-foreground" style={{ width: "100%", height: "auto" }}>
        {/* column headers */}
        <text x={0} y={18} fontSize={11} fontWeight={600} fill="currentColor">Trial</text>
        <text x={W} y={18} fontSize={11} fontWeight={600} fill="currentColor" textAnchor="end">
          {prettyMeasure(group.effect_measure)} (CI)
        </text>

        {/* null reference line */}
        <line x1={xPix(nullVal)} y1={HEADER_H} x2={xPix(nullVal)} y2={HEADER_H + rows.length * ROW_H + diamondRows * ROW_H}
          stroke="#9ca3af" strokeWidth={1} strokeDasharray="3 3" />

        {/* trial rows */}
        {rows.map((t, i) => {
          const cy = HEADER_H + i * ROW_H + ROW_H / 2;
          const cx = xPix(t.effect);
          const half = boxSize(i);
          const x1 = t.ci_low != null ? xPix(t.ci_low) : cx;
          const x2 = t.ci_high != null ? xPix(t.ci_high) : cx;
          const valStr = `${fmt(t.effect)} (${fmt(t.ci_low)} to ${fmt(t.ci_high)})`;
          const href = trialHref(t.doi ?? t.paper_id);
          const dagger = t.oriented_flipped ? " †" : "";
          const labelText = (t.label.length > 28 ? t.label.slice(0, 27) + "…" : t.label) + dagger;
          return (
            <g key={`${t.paper_id}-${i}`}>
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  <text x={0} y={cy + 3} fontSize={11} className="fill-blue-600 hover:underline" style={{ cursor: "pointer" }}>
                    {labelText}
                  </text>
                </a>
              ) : (
                <text x={0} y={cy + 3} fontSize={11} fill="currentColor" className="fill-foreground/80">
                  {labelText}
                </text>
              )}
              {/* CI whisker */}
              <line x1={x1} y1={cy} x2={x2} y2={cy} stroke="#2563eb" strokeWidth={1.5} />
              <line x1={x1} y1={cy - 3} x2={x1} y2={cy + 3} stroke="#2563eb" strokeWidth={1.5} />
              <line x1={x2} y1={cy - 3} x2={x2} y2={cy + 3} stroke="#2563eb" strokeWidth={1.5} />
              {/* point estimate */}
              <rect x={cx - half} y={cy - half} width={half * 2} height={half * 2} fill="#1d4ed8" />
              <text x={W} y={cy + 3} fontSize={11} fill="currentColor" textAnchor="end" className="fill-foreground/70 tabular-nums">
                {valStr}
              </text>
            </g>
          );
        })}

        {/* pooled diamond */}
        {group.pooled && (() => {
          const cy = HEADER_H + rows.length * ROW_H + ROW_H / 2;
          const cl = xPix(group.pooled.ci_low);
          const ch = xPix(group.pooled.ci_high);
          const cc = xPix(group.pooled.effect);
          const hh = 7;
          const valStr = `${fmt(group.pooled.effect)} (${fmt(group.pooled.ci_low)} to ${fmt(group.pooled.ci_high)})`;
          return (
            <g>
              <text x={0} y={cy + 4} fontSize={11} fontWeight={700} fill="currentColor">Pooled (random)</text>
              <polygon points={`${cl},${cy} ${cc},${cy - hh} ${ch},${cy} ${cc},${cy + hh}`}
                fill="#b91c1c" stroke="#7f1d1d" />
              <text x={W} y={cy + 4} fontSize={11} fontWeight={700} fill="currentColor" textAnchor="end" className="tabular-nums">
                {valStr}
              </text>
            </g>
          );
        })()}

        {/* x-axis */}
        {(() => {
          const axisY = HEADER_H + rows.length * ROW_H + diamondRows * ROW_H + 6;
          return (
            <g>
              <line x1={plotLeft} y1={axisY} x2={plotRight} y2={axisY} stroke="#9ca3af" strokeWidth={1} />
              {ticks.map((t, i) => (
                <g key={i}>
                  <line x1={xPix(t)} y1={axisY} x2={xPix(t)} y2={axisY + 4} stroke="#9ca3af" strokeWidth={1} />
                  <text x={xPix(t)} y={axisY + 15} fontSize={10} fill="currentColor" textAnchor="middle" className="fill-foreground/60">
                    {fmt(t)}
                  </text>
                </g>
              ))}
              <text x={xPix(nullVal)} y={axisY + 30} fontSize={9} fill="currentColor" textAnchor="middle" className="fill-foreground/45">
                no effect ({ratio ? "ratio = 1" : "difference = 0"})
              </text>
            </g>
          );
        })()}
      </svg>
    </div>
  );
}

// ── Cross-compound (subgrouped) forest plot ──────────────────────────────────
// One standardized (Cohen's d) figure per outcome, pooling every compound: a
// subheading + rows + subtotal diamond per compound, then an overall diamond with
// heterogeneity (I², τ², 95% prediction interval), directional axis labels, and a
// collapsible appendix of studies that couldn't be standardized.
type XItem =
  | { kind: "subhead"; label: string }
  | { kind: "trial"; t: ForestTrial }
  | { kind: "diamond"; label: string; pooled: ForestPooled; bold?: boolean };

function CrossCompoundForestPlot({
  group,
  domainLabels = {},
}: {
  group: ForestGroup;
  domainLabels?: DomainLabels;
}) {
  const subs = group.subgroups ?? [];
  const items: XItem[] = [];
  for (const s of subs) {
    items.push({ kind: "subhead", label: `${prettyIngredient(s.ingredient)} (${s.n_trials})` });
    for (const t of s.trials) items.push({ kind: "trial", t });
    if (s.pooled) items.push({ kind: "diamond", label: "Subtotal", pooled: s.pooled });
  }
  if (group.pooled) items.push({ kind: "diamond", label: "Overall (random effects)", pooled: group.pooled, bold: true });

  const ROW_H = 26, SUBHEAD_H = 24, HEADER_H = 28, AXIS_H = 58;
  const LABEL_W = 210, VAL_W = 140, PLOT_W = 360;
  const W = LABEL_W + PLOT_W + VAL_W;
  const plotLeft = LABEL_W, plotRight = LABEL_W + PLOT_W;
  const H = HEADER_H + items.reduce((s, it) => s + (it.kind === "subhead" ? SUBHEAD_H : ROW_H), 0) + AXIS_H;

  // x-domain across all trial estimates/CIs, subtotal + overall diamonds, and 0.
  const vals: number[] = [0];
  for (const it of items) {
    if (it.kind === "trial") { for (const v of [it.t.effect, it.t.ci_low, it.t.ci_high]) if (v != null && Number.isFinite(v)) vals.push(v); }
    if (it.kind === "diamond") vals.push(it.pooled.effect, it.pooled.ci_low, it.pooled.ci_high, it.pooled.pred_low ?? it.pooled.effect, it.pooled.pred_high ?? it.pooled.effect);
  }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const xPix = (v: number) => plotLeft + ((v - lo) / (hi - lo)) * PLOT_W;
  const step = (hi - lo) / 4;
  const ticks = [0, 1, 2, 3, 4].map((i) => lo + step * i);

  const allW = subs.flatMap((s) => s.trials.map((t) => (t.weight != null ? Math.sqrt(t.weight) : null)));
  const maxSqrt = Math.max(...allW.filter((x): x is number => x != null), 1);

  let y = HEADER_H;
  const nodes: ReactNode[] = [];
  for (let i = 0; i < items.length; i++) {
    const it = items[i]!;
    if (it.kind === "subhead") {
      const cy = y + SUBHEAD_H - 8;
      nodes.push(
        <text key={`sh${i}`} x={0} y={cy} fontSize={11} fontWeight={700} fill="currentColor" className="fill-foreground/80">{it.label}</text>
      );
      y += SUBHEAD_H;
    } else if (it.kind === "trial") {
      const t = it.t; const cy = y + ROW_H / 2; const cx = xPix(t.effect);
      const sw = t.weight != null ? Math.sqrt(t.weight) : null;
      const half = sw == null ? 6 : 4 + (sw / maxSqrt) * 8;
      const x1 = t.ci_low != null ? xPix(t.ci_low) : cx;
      const x2 = t.ci_high != null ? xPix(t.ci_high) : cx;
      const href = trialHref(t.doi ?? t.paper_id);
      const lbl = (t.label.length > 30 ? t.label.slice(0, 29) + "…" : t.label) + (t.oriented_flipped ? " †" : "");
      const valStr = `${fmt(t.effect)} (${fmt(t.ci_low)} to ${fmt(t.ci_high)})`;
      nodes.push(
        <g key={`t${i}`}>
          {href ? (
            <a href={href} target="_blank" rel="noopener noreferrer">
              <text x={12} y={cy + 3} fontSize={11} className="fill-blue-600 hover:underline" style={{ cursor: "pointer" }}>{lbl}</text>
            </a>
          ) : (
            <text x={12} y={cy + 3} fontSize={11} className="fill-foreground/80" fill="currentColor">{lbl}</text>
          )}
          <line x1={x1} y1={cy} x2={x2} y2={cy} stroke="#2563eb" strokeWidth={1.5} />
          <line x1={x1} y1={cy - 3} x2={x1} y2={cy + 3} stroke="#2563eb" strokeWidth={1.5} />
          <line x1={x2} y1={cy - 3} x2={x2} y2={cy + 3} stroke="#2563eb" strokeWidth={1.5} />
          <rect x={cx - half} y={cy - half} width={half * 2} height={half * 2} fill="#1d4ed8" />
          <text x={W} y={cy + 3} fontSize={11} textAnchor="end" fill="currentColor" className="fill-foreground/70 tabular-nums">{valStr}</text>
        </g>
      );
      y += ROW_H;
    } else {
      const p = it.pooled; const cy = y + ROW_H / 2;
      const cl = xPix(p.ci_low), ch = xPix(p.ci_high), cc = xPix(p.effect); const hh = it.bold ? 8 : 6;
      const color = it.bold ? "#b91c1c" : "#6b7280";
      const valStr = `${fmt(p.effect)} (${fmt(p.ci_low)} to ${fmt(p.ci_high)})`;
      nodes.push(
        <g key={`d${i}`}>
          <text x={it.bold ? 0 : 12} y={cy + 4} fontSize={11} fontWeight={it.bold ? 700 : 600} fill="currentColor">{it.label}</text>
          {/* prediction interval line (overall only) */}
          {it.bold && p.pred_low != null && p.pred_high != null && (
            <line x1={xPix(p.pred_low)} y1={cy} x2={xPix(p.pred_high)} y2={cy} stroke="#b91c1c" strokeWidth={1} strokeDasharray="2 2" opacity={0.7} />
          )}
          <polygon points={`${cl},${cy} ${cc},${cy - hh} ${ch},${cy} ${cc},${cy + hh}`} fill={color} stroke={it.bold ? "#7f1d1d" : "#4b5563"} />
          <text x={W} y={cy + 4} fontSize={11} fontWeight={it.bold ? 700 : 600} textAnchor="end" fill="currentColor" className="tabular-nums">{valStr}</text>
        </g>
      );
      y += ROW_H;
    }
  }

  const axisY = y + 6;
  const ov = group.pooled;

  return (
    <div className="border-2 border-primary/40 rounded-lg bg-card p-4 mb-6">
      <div className="mb-1 flex flex-wrap items-baseline gap-x-3">
        <h3 className="font-semibold text-foreground">{group.domainLabel ?? prettyDomain(group.outcome_domain, domainLabels)} — all compounds</h3>
        <span className="text-[10px] font-semibold uppercase tracking-wide rounded px-1.5 py-0.5 bg-violet-100 text-violet-700">Standardized (SMD)</span>
        <span className="text-xs text-foreground/50">
          {group.n_trials} trials · {group.n_compounds} compounds
          {ov ? ` · I² = ${ov.i2}%` : ""}
        </span>
      </div>
      <p className="mb-2 text-[11px] leading-snug text-foreground/55">
        Historical cross-compound analysis. Current scientific rules require matching treatments and outcomes; rebuild before interpreting this estimate.
      </p>
      <svg viewBox={`0 0 ${W} ${H}`} className="text-foreground" style={{ width: "100%", height: "auto" }}>
        <text x={0} y={18} fontSize={11} fontWeight={600} fill="currentColor">Compound / trial</text>
        <text x={W} y={18} fontSize={11} fontWeight={600} textAnchor="end" fill="currentColor">SMD (95% CI)</text>
        <line x1={xPix(0)} y1={HEADER_H} x2={xPix(0)} y2={axisY} stroke="#9ca3af" strokeWidth={1} strokeDasharray="3 3" />
        {nodes}
        {/* x-axis + directional labels */}
        <line x1={plotLeft} y1={axisY} x2={plotRight} y2={axisY} stroke="#9ca3af" strokeWidth={1} />
        {ticks.map((t, i) => (
          <g key={`x${i}`}>
            <line x1={xPix(t)} y1={axisY} x2={xPix(t)} y2={axisY + 4} stroke="#9ca3af" strokeWidth={1} />
            <text x={xPix(t)} y={axisY + 15} fontSize={10} textAnchor="middle" fill="currentColor" className="fill-foreground/60">{fmt(t)}</text>
          </g>
        ))}
        <text x={plotLeft} y={axisY + 30} fontSize={9} textAnchor="start" fill="currentColor" className="fill-foreground/45">← lower outcome values</text>
        <text x={plotRight} y={axisY + 30} fontSize={9} textAnchor="end" fill="currentColor" className="fill-foreground/45">higher outcome values →</text>
      </svg>
      {ov && (
        <p className="mt-1 text-[11px] text-foreground/60">
          Heterogeneity: I² = {ov.i2}%{ov.tau2 != null ? `, τ² = ${ov.tau2}` : ""}
          {ov.pred_low != null && ov.pred_high != null
            ? ` · 95% prediction interval ${fmt(ov.pred_low)} to ${fmt(ov.pred_high)}`
            : ""}
        </p>
      )}
      {group.excluded && group.excluded.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-foreground/60 hover:text-foreground">
            {group.excluded.length} more studies reported this outcome but couldn&apos;t be standardized (shown, not pooled)
          </summary>
          <ul className="mt-2 space-y-0.5 pl-4">
            {group.excluded.map((e) => {
              const href = trialHref(e.paper_id);
              const lbl = `${e.first_author ?? e.paper_id}${e.year ? ` ${e.year}` : ""}`;
              return (
                <li key={e.paper_id} className="text-foreground/60">
                  {href ? <a href={href} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline">{lbl}</a> : lbl}
                  {e.ingredient ? ` — ${prettyIngredient(e.ingredient)}` : ""}
                  {e.effect_measure ? ` · ${prettyMeasure(e.effect_measure)}` : ""}
                  {e.reason ? ` · ${e.reason}` : ""}
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </div>
  );
}
