"use client";

/** The report stage (slug "report") — synthesize an AI narrative review.
 *
 *  The final wizard stage: once the dashboard bundle exists, the user picks a
 *  depth (how much evidence the LLM actually reads, not just length), an
 *  audience, optionally trims the section list, and clicks Synthesize. The
 *  server runs `synthesize_review.py` as a background job (kind "synthesis")
 *  on the LLM queue; this page polls it and renders the finished markdown.
 *
 *  Money rule: synthesis is NEVER auto-fired (contrast useAutoBuildDashboard —
 *  the dashboard build is free, this one spends LLM tokens). The only trigger
 *  is the explicit button.
 *
 *  Options persist to the review config (`synthesis.*` / `models.synthesis`,
 *  both depth-2 so the server's two-level merge keeps siblings), AND the
 *  chosen values ride the POST body so the run uses exactly what's on screen
 *  even if a config PATCH is still in flight.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Download,
  FileText,
  Loader2,
  RotateCw,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { useDashboardState, useReviewConfig } from "@/hooks/useReview";
import {
  getJob,
  getReviewDocument,
  listJobs,
  patchReviewConfig,
  synthesizeReview,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtDur } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { ReviewDocumentOut, StageOut } from "@/lib/types";

// ── Mirrors of synthesize_review.py (SECTIONS / DEPTH_MODELS / defaults) ──────
// Kept in sync by hand like other pipeline-constant mirrors; the server
// validates every value against the script's literals, so drift 400s loudly
// rather than running with a bad option.

type Depth = "brief" | "standard" | "deep";

const DEPTH_RANK: Record<Depth, number> = { brief: 0, standard: 1, deep: 2 };

const DEPTH_TIERS: {
  key: Depth;
  label: string;
  desc: string;
  cost: string;
}[] = [
  {
    key: "brief",
    label: "Brief",
    desc: "Reads the aggregates only — totals, pooled effects, PRISMA. Key findings and limitations.",
    cost: "1 LLM call · haiku · ~1,000 words",
  },
  {
    key: "standard",
    label: "Standard",
    desc: "Adds every study's extracted row and per-paper summary. A full structured review.",
    cost: "1 LLM call · sonnet · ~3,000 words",
  },
  {
    key: "deep",
    label: "Deep",
    desc: "Adds per-study effect sizes, risk-of-bias and adverse-event detail; each section written separately.",
    cost: "~13 LLM calls · opus · ~8,000 words",
  },
];

const SECTIONS: { id: string; title: string; tier: Depth }[] = [
  { id: "key_findings", title: "Key findings", tier: "brief" },
  { id: "background", title: "Background", tier: "standard" },
  { id: "methods_prisma", title: "Methods and study flow", tier: "standard" },
  { id: "evidence_base", title: "The evidence base at a glance", tier: "brief" },
  { id: "results_by_intervention", title: "Results by intervention", tier: "standard" },
  { id: "pooled_results", title: "Pooled results", tier: "brief" },
  { id: "intervention_deep_dives", title: "Intervention deep dives", tier: "deep" },
  { id: "safety", title: "Safety and adverse events", tier: "standard" },
  { id: "risk_of_bias", title: "Risk of bias", tier: "deep" },
  { id: "certainty_grade", title: "Certainty of the evidence", tier: "deep" },
  { id: "limitations", title: "Limitations", tier: "brief" },
  { id: "research_gaps", title: "Research gaps", tier: "deep" },
  { id: "conclusions", title: "Conclusions", tier: "standard" },
];

const MODELS = ["auto", "haiku", "sonnet", "opus"] as const;

function defaultSections(depth: Depth): string[] {
  return SECTIONS.filter((s) => DEPTH_RANK[s.tier] <= DEPTH_RANK[depth]).map(
    (s) => s.id,
  );
}

interface SynthesisConfig {
  depth?: Depth;
  audience?: string;
  sections?: string[] | null;
}

export function ReportStage({
  reviewId,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();
  const { isAuthenticated, ready } = useAuth();
  const enabled = ready && isAuthenticated && !!reviewId;

  const dash = useDashboardState(reviewId);
  const config = useReviewConfig(reviewId);
  const doc = useQuery<ReviewDocumentOut>({
    queryKey: ["review-document", reviewId],
    queryFn: () => getReviewDocument(reviewId),
    enabled,
    retry: false,
  });

  // Poll the job list only while something is active — same rule as the export
  // stage's BuildingPanel, so a finished page costs no requests.
  const jobs = useQuery({
    queryKey: ["review-jobs", reviewId],
    queryFn: () => listJobs(reviewId),
    enabled,
    refetchInterval: (q) =>
      q.state.data?.some((j) => j.state === "running" || j.state === "queued")
        ? 3000
        : false,
  });
  const synthJob = jobs.data?.find(
    (j) =>
      j.kind === "synthesis" && (j.state === "running" || j.state === "queued"),
  );
  // Jobs come newest-first; the latest synthesis attempt tells us about failure.
  const lastSynth = jobs.data?.find((j) => j.kind === "synthesis");
  // A pipeline/step run blocks synthesis (the server 409s) — say so up front.
  const blockingJob = jobs.data?.find(
    (j) =>
      (j.state === "running" || j.state === "queued") &&
      !["synthesis", "metadata", "pdf", "fulltext_screen"].includes(j.kind),
  );

  // Options: config is the source of truth; PATCHes are small and merged
  // server-side at depth 2, so each control writes only its own key.
  const cfgJson = config.data?.config_json as
    | { synthesis?: SynthesisConfig; models?: Record<string, string> }
    | undefined;
  const syn = cfgJson?.synthesis ?? {};
  const depth: Depth = syn.depth && syn.depth in DEPTH_RANK ? syn.depth : "standard";
  const audience = syn.audience === "plain" ? "plain" : "clinician";
  const sections = syn.sections ?? null; // null → depth's default set
  const model = cfgJson?.models?.synthesis ?? "auto";
  const effectiveSections = sections ?? defaultSections(depth);

  const save = useMutation({
    mutationFn: (partial: {
      synthesis?: SynthesisConfig;
      models?: Record<string, string>;
    }) => patchReviewConfig(reviewId, partial),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) =>
      toast.error((e as Error)?.message ?? "Could not save the option"),
  });

  const run = useMutation({
    mutationFn: () =>
      synthesizeReview(reviewId, {
        depth,
        audience,
        model,
        // Only send an explicit list when the user customized it; otherwise the
        // script derives the depth's default set itself.
        sections: sections ?? undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-jobs", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) =>
      toast.error((e as Error)?.message ?? "Could not start the synthesis"),
  });

  // When the synthesis finishes (active→idle), refresh the document. useJob's
  // finish-invalidation can miss a job it never latched (synthesis is not a
  // foreground kind), so this transition is the reliable link.
  const wasRunning = useRef(false);
  useEffect(() => {
    const running = !!synthJob;
    if (wasRunning.current && !running) {
      qc.invalidateQueries({ queryKey: ["review-document", reviewId] });
      qc.invalidateQueries({ queryKey: ["review-jobs", reviewId] });
    }
    wasRunning.current = running;
  }, [synthJob, reviewId, qc]);

  const download = () => {
    const md = doc.data?.markdown;
    if (!md) return;
    const url = URL.createObjectURL(
      new Blob([md], { type: "text/markdown;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "review.md";
    a.click();
    URL.revokeObjectURL(url);
  };

  if (dash.isLoading || config.isLoading) {
    return (
      <div className="animate-pulse space-y-4">
        <div className="h-40 rounded-lg border border-border bg-muted" />
        <div className="h-64 rounded-lg border border-border bg-muted" />
      </div>
    );
  }

  if (dash.data?.exists !== true) {
    return (
      <div className="rounded-lg border border-border p-4 text-sm">
        <p className="flex items-center gap-2 font-medium">
          <FileText size={15} />
          Synthesize an AI-written narrative review
        </p>
        <p className="mt-2 text-muted-foreground">
          The synthesis reads the built dashboard bundle — pooled results,
          per-study extractions, and the PRISMA flow — so the dashboard has to
          exist first.
        </p>
        <Link
          href={`/reviews/${reviewId}/stages/view-dashboard`}
          className="mt-3 inline-block text-blue-600 underline"
        >
          Open the View dashboard stage (it builds automatically) →
        </Link>
      </div>
    );
  }

  const running = !!synthJob || run.isPending;
  const failed = !running && lastSynth?.state === "failed";

  return (
    <div className="space-y-4">
      {/* ── Options ── */}
      <section className="rounded-lg border border-border p-4">
        <h3 className="text-sm font-semibold">Review depth</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Depth controls how much of the evidence the AI actually reads — not
          just how long the write-up is.
        </p>
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          {DEPTH_TIERS.map((t) => (
            <label
              key={t.key}
              className={`flex cursor-pointer flex-col rounded-lg border p-3 text-sm ${
                depth === t.key
                  ? "border-primary bg-primary/5"
                  : "border-border hover:bg-accent"
              }`}
            >
              <span className="flex items-center gap-2 font-medium">
                <input
                  type="radio"
                  name="synthesis-depth"
                  className="accent-primary"
                  checked={depth === t.key}
                  disabled={save.isPending || running}
                  onChange={() => save.mutate({ synthesis: { depth: t.key } })}
                />
                {t.label}
              </span>
              <span className="mt-1 text-xs text-muted-foreground">
                {t.desc}
              </span>
              <span className="mt-2 text-xs font-medium text-muted-foreground">
                {t.cost}
              </span>
            </label>
          ))}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
          <div className="flex items-center gap-3">
            <span className="font-medium">Audience</span>
            {(
              [
                ["clinician", "Clinician / researcher"],
                ["plain", "Plain language"],
              ] as const
            ).map(([value, label]) => (
              <label
                key={value}
                className="flex cursor-pointer items-center gap-1.5"
              >
                <input
                  type="radio"
                  name="synthesis-audience"
                  className="accent-primary"
                  checked={audience === value}
                  disabled={save.isPending || running}
                  onChange={() =>
                    save.mutate({ synthesis: { audience: value } })
                  }
                />
                {label}
              </label>
            ))}
          </div>
          <label className="flex items-center gap-2">
            <span className="font-medium">Model</span>
            <select
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
              value={model}
              disabled={save.isPending || running}
              onChange={(e) =>
                save.mutate({ models: { synthesis: e.target.value } })
              }
            >
              {MODELS.map((m) => (
                <option key={m} value={m}>
                  {m === "auto" ? "Auto (match depth)" : m}
                </option>
              ))}
            </select>
          </label>
        </div>

        <details className="mt-4" open={sections !== null}>
          <summary className="cursor-pointer text-sm font-medium">
            Sections{" "}
            <span className="font-normal text-muted-foreground">
              — {effectiveSections.length} of {SECTIONS.length} included
              {sections === null && " (the depth's default set)"}
            </span>
          </summary>
          <div className="mt-2 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {SECTIONS.map((s) => {
              const on = effectiveSections.includes(s.id);
              return (
                <label
                  key={s.id}
                  className="flex cursor-pointer items-center gap-2 text-sm"
                >
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-primary"
                    checked={on}
                    disabled={save.isPending || running}
                    onChange={() => {
                      const next = on
                        ? effectiveSections.filter((id) => id !== s.id)
                        : SECTIONS.map((x) => x.id).filter(
                            (id) => id === s.id || effectiveSections.includes(id),
                          );
                      if (next.length === 0) {
                        toast.error("Keep at least one section");
                        return;
                      }
                      save.mutate({ synthesis: { sections: next } });
                    }}
                  />
                  <span>{s.title}</span>
                </label>
              );
            })}
          </div>
          {sections !== null && (
            <button
              type="button"
              className="mt-2 text-xs text-blue-600 underline"
              disabled={save.isPending || running}
              onClick={() => save.mutate({ synthesis: { sections: null } })}
            >
              Reset to the depth&apos;s default sections
            </button>
          )}
        </details>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => run.mutate()}
            disabled={running || !!blockingJob || save.isPending}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {running ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <FileText size={14} />
            )}
            {doc.data?.exists ? "Regenerate review" : "Synthesize review"}
          </button>
          <span className="text-xs text-muted-foreground">
            Runs in the background and spends LLM credits — it never starts on
            its own.
          </span>
        </div>
        {blockingJob && (
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
            Another job is {blockingJob.state} on this review — synthesis can
            start once it finishes.
          </p>
        )}
      </section>

      {failed && (
        <p className="flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span>
            The last synthesis failed
            {lastSynth?.error ? `: ${lastSynth.error}` : ""}. Adjust the options
            if needed and try again.
          </span>
        </p>
      )}

      {synthJob && <SynthesizingPanel jobId={synthJob.id} />}

      {doc.data?.exists && doc.data.markdown && (
        <DocumentPanel
          doc={doc.data}
          onDownload={download}
          regenerating={running}
        />
      )}
    </div>
  );
}

/** Live progress while the synthesis job runs (the BuildingPanel pattern:
 *  listJobs has only the between-steps DB row; GET /jobs/{id} overlays the
 *  worker's live sidecar, fed by the script's `Progress: i/N` lines). */
function SynthesizingPanel({ jobId }: { jobId: string }) {
  const detail = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId),
    refetchInterval: 2000,
  });
  const live = detail.data?.progress?.live ?? null;
  const total = live?.total ?? 0;

  return (
    <div className="rounded-lg border border-primary/40 bg-primary/5 p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
        <Loader2 size={14} className="shrink-0 animate-spin text-primary" />
        <span className="font-medium">Synthesizing the review</span>
        {total > 1 ? (
          <span className="tabular-nums text-muted-foreground">
            section {Math.min((live?.done ?? 0) + 1, total)} of {total}
            {live?.eta_s != null && ` · ~${fmtDur(live.eta_s)} left`}
          </span>
        ) : (
          <span className="text-muted-foreground">
            writing… (a single-call synthesis has no mid-run progress)
          </span>
        )}
      </div>
      {total > 1 && (
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-primary/15">
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${Math.round((100 * (live?.done ?? 0)) / total)}%` }}
          />
        </div>
      )}
      <p className="mt-1.5 text-xs text-muted-foreground">
        The document will appear below when the run finishes.
      </p>
    </div>
  );
}

function DocumentPanel({
  doc,
  onDownload,
  regenerating,
}: {
  doc: ReviewDocumentOut;
  onDownload: () => void;
  regenerating: boolean;
}) {
  const fallbacks = (doc.sections ?? []).filter(
    (s) => s.source === "template_fallback",
  ).length;

  return (
    <section className="rounded-lg border border-border">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-2.5 text-xs text-muted-foreground">
        <span className="text-sm font-semibold text-foreground">
          Synthesized review
        </span>
        {doc.generated_at && (
          <span>{new Date(doc.generated_at).toLocaleString()}</span>
        )}
        {doc.depth && <Badge>{doc.depth}</Badge>}
        {doc.audience && <Badge>{doc.audience}</Badge>}
        {doc.model && <Badge>{doc.model}</Badge>}
        {doc.word_count != null && (
          <span>{doc.word_count.toLocaleString()} words</span>
        )}
        <button
          type="button"
          onClick={onDownload}
          className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-foreground hover:bg-accent"
        >
          <Download size={13} />
          Download .md
        </button>
      </header>

      {doc.stale && (
        <p className="m-4 mb-0 flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-sm text-amber-700 dark:text-amber-400">
          {regenerating ? (
            <Loader2 size={14} className="shrink-0 animate-spin" />
          ) : (
            <RotateCw size={14} className="shrink-0" />
          )}
          <span>
            This document is out of date
            {doc.stale_reason ? ` — ${doc.stale_reason}` : ""}. Use{" "}
            <strong>Regenerate review</strong> above to refresh it.
          </span>
        </p>
      )}
      {fallbacks > 0 && (
        <p className="m-4 mb-0 rounded-md border border-border bg-muted/50 px-3 py-1.5 text-xs text-muted-foreground">
          {fallbacks} section{fallbacks > 1 ? "s" : ""} used the deterministic
          template because the AI draft cited a number not present in the data
          (each is marked in the sidecar).
        </p>
      )}

      <div className="max-w-none px-6 py-5 text-[15px] leading-relaxed">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            h1: (p) => <h1 className="mb-3 text-2xl font-bold" {...p} />,
            h2: (p) => (
              <h2
                className="mb-2 mt-6 border-b border-border pb-1 text-lg font-semibold first:mt-0"
                {...p}
              />
            ),
            h3: (p) => <h3 className="mb-1.5 mt-4 font-semibold" {...p} />,
            p: (p) => <p className="mb-3" {...p} />,
            ul: (p) => <ul className="mb-3 list-disc space-y-1 pl-5" {...p} />,
            ol: (p) => <ol className="mb-3 list-decimal space-y-1 pl-5" {...p} />,
            em: (p) => <em className="text-muted-foreground" {...p} />,
            blockquote: (p) => (
              <blockquote
                className="mb-3 border-l-2 border-border pl-3 text-muted-foreground"
                {...p}
              />
            ),
            a: (p) => <a className="text-blue-600 underline" {...p} />,
            code: (p) => (
              <code className="rounded bg-muted px-1 py-0.5 text-[13px]" {...p} />
            ),
            table: (p) => (
              <div className="mb-3 overflow-x-auto">
                <table className="w-full border-collapse text-sm" {...p} />
              </div>
            ),
            th: (p) => (
              <th
                className="border-b border-border px-2 py-1.5 text-left font-semibold"
                {...p}
              />
            ),
            td: (p) => (
              <td className="border-b border-border/60 px-2 py-1.5 align-top" {...p} />
            ),
          }}
        >
          {doc.markdown ?? ""}
        </ReactMarkdown>
      </div>
    </section>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full border border-border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide">
      {children}
    </span>
  );
}
