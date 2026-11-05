"use client";

/** The PRISMA screening funnel — the single canonical diagram.
 *
 *  Replaces the per-review hand-copied diagrams the website used to carry. It
 *  consumes the `{main_flow, exclusions, sources}` shape that
 *  `ber_core.prisma.to_visualize_json` produces (and the studio's `/prisma` endpoint
 *  serves live), so the same component renders a published dashboard's funnel
 *  and a mid-pipeline "funnel so far".
 *
 *  Styled entirely with Tailwind utilities resolving through the host's design
 *  tokens — no hardcoded colours, so it follows light/dark automatically.
 *  Layout is a vertical spine of stage boxes; each exclusion branches to the
 *  right from the stage it applies to. A partial funnel (search only) just draws
 *  fewer boxes rather than erroring.
 */

import { Fragment } from "react";
import { ArrowDown, ArrowRight } from "lucide-react";

export interface PrismaStage {
  label: string;
  n: number;
  stage: string;
}

export interface PrismaExclusion {
  /** 1-indexed spine position this exclusion branches from. */
  from_step: number;
  label: string;
  n: number;
  details?: string[];
}

export interface PrismaSource {
  name: string;
  n: number;
}

export interface PrismaData {
  main_flow: PrismaStage[];
  exclusions: PrismaExclusion[];
  sources: PrismaSource[];
  issues?: { code: string; reason?: string; paper_ids?: string[]; artifact?: string }[];
  conservation?: { valid: boolean; cohort: number; assigned: number };
  counts?: {
    included_warning?: string;
    [k: string]: unknown;
  };
}

function fmt(n: number): string {
  return n.toLocaleString();
}

export function PrismaFlow({ data }: { data: PrismaData }) {
  const flow = data.main_flow ?? [];
  const exclusions = data.exclusions ?? [];
  const warning = data.counts?.included_warning;

  if (flow.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        No screening funnel yet — it fills in as the review runs.
      </p>
    );
  }

  // Group exclusions by the spine step they branch from, so each stage box can
  // render its own side branch.
  const byStep = new Map<number, PrismaExclusion[]>();
  for (const e of exclusions) {
    const arr = byStep.get(e.from_step) ?? [];
    arr.push(e);
    byStep.set(e.from_step, arr);
  }

  // Any exclusions at all? If so every row reserves the right-hand column so the
  // spine (and its down-arrows) stay a consistent width whether or not THIS row
  // has a branch. Two-column grid: spine | (arrow + exclusion).
  const hasExclusions = byStep.size > 0;

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      {data.issues && data.issues.length > 0 && <details className="mb-3 rounded border border-amber-500 p-2 text-sm"><summary>Historical artifacts need reconciliation ({data.issues.length} findings)</summary>
        <p>Counts below describe the verified cohort. Historical extracted reports may have no matching eligibility decision.</p>
        {data.issues.map((i, n) => <p key={n}>{i.artifact}: {i.reason ?? i.code.replace(/_/g, " ")}{i.paper_ids?.length ? ` (${i.paper_ids.length} reports)` : ""}</p>)}
      </details>}
      <ol
        className="mx-auto grid max-w-2xl items-center gap-x-0 gap-y-0"
        style={{
          gridTemplateColumns: hasExclusions ? "1fr auto 14rem" : "1fr",
        }}
      >
        {flow.map((s, i) => {
          const branches = byStep.get(i + 1) ?? [];
          const last = i === flow.length - 1;
          return (
            <Fragment key={`${s.stage}-${s.label}`}>
              {/* Spine box */}
              <div
                className={`min-w-0 rounded-md border px-3 py-2 ${
                  last
                    ? "border-primary/50 bg-primary/5"
                    : "border-border bg-background"
                }`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-sm font-medium">{s.label}</span>
                  <span className="shrink-0 text-sm font-semibold tabular-nums">
                    {fmt(s.n)}
                  </span>
                </div>
              </div>

              {hasExclusions &&
                (branches.length > 0 ? (
                  <>
                    {/* Horizontal arrow: spine → exclusion box. */}
                    <div className="flex shrink-0 items-center px-1 text-muted-foreground">
                      <span className="h-0.5 w-3 bg-current" aria-hidden />
                      <ArrowRight size={16} strokeWidth={2.5} className="-ml-1" aria-hidden />
                    </div>
                    <div className="space-y-1.5">
                      {branches.map((e) => (
                        <div
                          key={e.label}
                          className="rounded-md border border-border bg-muted/40 px-3 py-2"
                        >
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="text-xs font-medium text-muted-foreground">
                              {e.label}
                            </span>
                            <span className="shrink-0 text-xs font-semibold tabular-nums text-muted-foreground">
                              −{fmt(e.n)}
                            </span>
                          </div>
                          {e.details && e.details.length > 0 && (
                            <ul className="mt-1 space-y-0.5 text-[11px] text-muted-foreground/80">
                              {e.details.map((d) => (
                                <li key={d}>{d}</li>
                              ))}
                            </ul>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  // Empty arrow + exclusion cells so the grid columns align.
                  <>
                    <div aria-hidden />
                    <div aria-hidden />
                  </>
                ))}

              {/* Gapless down-arrow under the spine box (first grid column). */}
              {!last && (
                <>
                  <div className="flex flex-col items-center py-1 text-muted-foreground">
                    <span className="h-4 w-0.5 bg-current" aria-hidden />
                    <ArrowDown size={16} strokeWidth={2.5} className="-mt-1" aria-hidden />
                  </div>
                  {hasExclusions && (
                    <>
                      <div aria-hidden />
                      <div aria-hidden />
                    </>
                  )}
                </>
              )}
            </Fragment>
          );
        })}
      </ol>

      {warning && (
        <p className="mx-auto mt-4 max-w-2xl rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          {warning}
        </p>
      )}
    </div>
  );
}
