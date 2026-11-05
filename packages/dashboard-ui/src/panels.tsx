"use client";

/** The honesty panels — what a Bird's Eye dashboard says about its own limits.
 *
 *  These have no counterpart on the public website because the website only ever
 *  renders topics that already worked. A studio preview must also render the
 *  topics that DIDN'T: nattokinase, where the correct output is "no
 *  meta-analysis is possible" and not a diamond built from whatever survived.
 *
 *  Styled entirely with Tailwind utilities resolving through the host's design
 *  tokens, so they follow light/dark automatically. No hardcoded colours. */

import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import type { DashboardSummary, Feasibility } from "./summary";
import { formatLabel } from "./utils";

/* ── Feasibility: the panel that must never over-claim ──────────────────── */

const FEASIBILITY: Record<
  string,
  { accent: string; chip: string; Icon: typeof Info; label: string }
> = {
  supported: {
    accent: "border-l-green-600",
    chip: "bg-green-500/10 text-green-600 border-green-500/30",
    Icon: CheckCircle2,
    label: "Pooling supported",
  },
  thin: {
    accent: "border-l-amber-500",
    chip: "bg-amber-500/10 text-amber-600 border-amber-500/30",
    Icon: AlertTriangle,
    label: "Thin evidence",
  },
  fragmented: {
    accent: "border-l-amber-500",
    chip: "bg-amber-500/10 text-amber-600 border-amber-500/30",
    Icon: AlertTriangle,
    label: "Fragmented",
  },
  not_possible: {
    accent: "border-l-red-600",
    chip: "bg-red-500/10 text-red-600 border-red-500/30",
    Icon: XCircle,
    label: "No meta-analysis possible",
  },
};

/** States plainly whether the topic supports a pooled estimate, and why not.
 *
 *  Renders the SAME numbers the publish gate reads, so the preview and the gate
 *  can never disagree about what the data supports. */
/** Accepts the whole `summary` (published dashboard) OR just the `feasibility`
 *  block (the wizard's meta stage, which fetches it live and has no full
 *  summary). It only ever reads `.feasibility`, so both are equivalent. */
export function FeasibilityCard({
  summary,
  feasibility,
}: {
  summary?: DashboardSummary;
  feasibility?: Feasibility;
}) {
  const f = feasibility ?? summary?.feasibility;
  if (!f) return null;
  // `fragmented` is the safe default: it neither promises poolability nor
  // declares it impossible, so an unrecognised verdict cannot over-claim.
  const style = FEASIBILITY[f.overall] ?? FEASIBILITY["fragmented"]!;
  const Icon = style.Icon;
  const poolable = f.domains?.filter((d) => d.verdict === "poolable") ?? [];

  return (
    <section
      className={`mb-6 rounded-lg border border-l-4 border-border bg-card p-4 ${style.accent}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-lg font-semibold">
          <Icon size={18} className="shrink-0" />
          Meta-analysis feasibility
        </h2>
        <span
          className={`rounded border px-2 py-0.5 text-[11px] font-medium ${style.chip}`}
        >
          {style.label}
        </span>
      </div>

      <p className="mt-2 max-w-prose text-sm leading-relaxed text-foreground/80">
        {f.recommendation}
      </p>

      <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm text-foreground/70">
        {/* Two different things: a paper can REPORT a comparison yet have none
            that can be pooled (effects stated with no SD/CI/SE). Labelling the
            poolable count as "reports a comparison" read as "0 papers report a
            comparison" on corpora where several plainly did. */}
        {typeof f.n_with_comparison === "number" && (
          <span>
            <strong className="tabular-nums text-foreground">
              {f.n_with_comparison}
            </strong>{" "}
            of{" "}
            <strong className="tabular-nums text-foreground">
              {f.n_papers}
            </strong>{" "}
            papers report a between-group comparison
          </span>
        )}
        <span>
          <strong className="tabular-nums text-foreground">
            {f.n_with_contrast}
          </strong>{" "}
          of{" "}
          <strong className="tabular-nums text-foreground">{f.n_papers}</strong>{" "}
          papers have a poolable comparison
        </span>
        <span>
          <strong className="tabular-nums text-foreground">{f.n_pooled}</strong>{" "}
          of{" "}
          <strong className="tabular-nums text-foreground">
            {f.n_comparisons ?? f.n_outcomes}
          </strong>{" "}
          comparison estimates enter supported pools
        </span>
      </div>

      {poolable.length > 0 && (
        <p className="mt-2 text-sm text-foreground/70">
          Compatible outcome groups:{" "}
          {poolable.map((d) => `${formatLabel(String(d.outcome ?? d.domain))} (${d.intervention ?? "treatment"} vs ${d.comparator ?? "control"})`).join(", ")}
        </p>
      )}

      {f.blockers?.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-sm text-foreground/60 hover:text-foreground/80">
            Why outcomes could not be pooled ({f.blockers.length} reasons)
          </summary>
          <ul className="mt-2 space-y-1 text-sm text-foreground/70">
            {f.blockers.map((b) => (
              <li key={b.code} className="flex gap-2">
                <span className="w-12 shrink-0 text-right font-medium tabular-nums text-foreground">
                  {b.n}
                </span>
                <span>{formatLabel(b.code)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/* ── Panels not shown, and panels you could add ─────────────────────────── */

/** A panel that silently vanishes is the failure this project exists to remove:
 *  the user cannot tell "this topic has no data for that" from "the dashboard
 *  is broken". Every omission states its reason. */
export function PanelSkipList({
  skipped,
  available,
}: {
  skipped: { key: string; label?: string; reason: string }[];
  available?: { key: string; label: string; why: string }[];
}) {
  if (!skipped.length && !available?.length) return null;
  return (
    <section className="mb-6 rounded-lg border border-border bg-card p-4">
      <h2 className="text-lg font-semibold">Panels</h2>
      {skipped.length > 0 && (
        <ul className="mt-2 space-y-1.5 text-sm text-foreground/70">
          {skipped.map((p) => (
            <li key={p.key} className="flex gap-2">
              <Info size={14} className="mt-0.5 shrink-0 text-foreground/40" />
              <span>
                <strong className="font-medium text-foreground">
                  {p.label ?? formatLabel(p.key)}
                </strong>{" "}
                — not shown: {p.reason}
              </span>
            </li>
          ))}
        </ul>
      )}
      {available && available.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-sm text-foreground/60 hover:text-foreground/80">
            Panels you could add ({available.length})
          </summary>
          <ul className="mt-2 space-y-1.5 text-sm text-foreground/70">
            {available.map((p) => (
              <li key={p.key}>
                <strong className="font-medium text-foreground">
                  {p.label}
                </strong>{" "}
                — {p.why}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/* ── Schema-drift report ────────────────────────────────────────────────── */

/** The contract probe's output surface. Studio-only: a published dashboard has
 *  no business showing its own extraction warnings, but the person deciding
 *  whether to publish very much needs to see them. */
export function ContractReport({ summary }: { summary: DashboardSummary }) {
  const c = summary.contract;
  if (c.clean) return null;
  const rows: [string, Record<string, number>][] = [
    ["Records missing a required key", c.missing_keys],
    ["Unrecognised risk-of-bias values", c.unknown_rob],
    ["Unrecognised arm types", c.unknown_arm_type],
  ];
  return (
    <details className="mb-6 rounded-lg border border-border bg-card p-4">
      <summary className="cursor-pointer text-sm font-medium">
        <AlertTriangle size={14} className="mr-1.5 inline text-amber-600" />
        Schema drift detected in this corpus
      </summary>
      <div className="mt-3 space-y-3">
        {rows
          .filter(([, v]) => Object.keys(v).length > 0)
          .map(([label, v]) => (
            <div key={label}>
              <div className="text-sm font-medium">{label}</div>
              <ul className="mt-1 space-y-0.5 text-sm text-foreground/70">
                {Object.entries(v).map(([k, n]) => (
                  <li key={k} className="flex gap-2">
                    <span className="w-12 shrink-0 text-right tabular-nums">
                      {n}
                    </span>
                    <code className="text-xs">{k || "(empty)"}</code>
                  </li>
                ))}
              </ul>
            </div>
          ))}
      </div>
    </details>
  );
}
