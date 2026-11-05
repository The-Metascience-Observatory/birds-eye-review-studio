"use client";

/** The collaborator rail — always present on the right during the walkthrough.
 *
 *  Three bands, cheapest first:
 *  1. An INSTANT orientation header built from data already on the client (no
 *     LLM, no latency) — the rail is never empty while a paid call runs.
 *  2. Blocker cards from the latest post-run sanity check, with Dismiss
 *     recording "I know, proceed" — advisory by design: Approve keeps working.
 *  3. The chat transcript. The agent holds context across every stage, so
 *     asking "why did so many drop out?" here can be answered with what you
 *     decided three stages ago.
 *
 *  The auto sanity-check fires from here when this stage's run completes. It
 *  is a PAID LLM call, so the client fires it at most once per job: the
 *  server's own per-job_id dedupe only scans the review's recent messages, and
 *  a check fired before the transcript loaded (an empty list looks exactly
 *  like "never reviewed") used to go out on every remount and reload.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Loader2, RefreshCcw, Send } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "@/lib/toast";

import { agentAsk, agentReview, getAgentMessages } from "@/lib/api";
import type { AgentMessageOut, JobOut, StageOut } from "@/lib/types";

/* ── "Already auto-checked" ledger ──────────────────────────────────────────
 * Job ids this browser has auto-fired a check for. In memory for this page
 * session (survives the rail remounting on navigation), mirrored to
 * localStorage per review so a reload or a second tab doesn't fire again
 * either. Bounded: only recent jobs can still be "the latest job". */
const FIRED_MAX = 100;
const firedThisSession = new Set<string>();
const firedKey = (reviewId: string) => `ber:agent-checked:${reviewId}`;

function readFired(reviewId: string): string[] {
  try {
    const raw = window.localStorage.getItem(firedKey(reviewId));
    const ids: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(ids) ? ids.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function alreadyAutoChecked(reviewId: string, jobId: string): boolean {
  return (
    firedThisSession.has(`${reviewId}:${jobId}`) ||
    readFired(reviewId).includes(jobId)
  );
}

function markAutoChecked(reviewId: string, jobId: string): void {
  firedThisSession.add(`${reviewId}:${jobId}`);
  try {
    const ids = [...readFired(reviewId).filter((j) => j !== jobId), jobId];
    window.localStorage.setItem(
      firedKey(reviewId),
      JSON.stringify(ids.slice(-FIRED_MAX)),
    );
  } catch {
    /* storage blocked — the in-memory set still covers this page session */
  }
}

export function AgentRail({
  reviewId,
  stage,
  job,
}: {
  reviewId: string;
  stage: StageOut;
  /** The review's latest job, from useJob — used to auto-fire the sanity check. */
  job: JobOut | null | undefined;
}) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  const messages = useQuery({
    queryKey: ["agent-messages", reviewId],
    queryFn: () => getAgentMessages(reviewId),
  });

  const ask = useMutation({
    mutationFn: (message: string) => agentAsk(reviewId, stage.slug, message),
    onSuccess: () => {
      setText("");
      qc.invalidateQueries({ queryKey: ["agent-messages", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "The assistant failed"),
  });

  const review = useMutation({
    mutationFn: (jobId: string) => agentReview(reviewId, stage.slug, jobId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["agent-messages", reviewId] }),
    // A 409 here is normal (busy, or the stage is approved) — stay quiet.
    onError: () => undefined,
  });

  const all = useMemo(() => messages.data ?? [], [messages.data]);

  // Auto sanity-check: this stage's run just finished and has no review yet.
  const transcriptLoaded = messages.isSuccess;
  useEffect(() => {
    if (!job || job.state !== "succeeded") return;
    // Decide only against the real transcript: while it loads, `all` is empty
    // and every finished job would look unreviewed.
    if (!transcriptLoaded) return;
    const touchesStage = (job.progress?.steps ?? []).some((r) =>
      stage.step_ids.includes(r.step_id),
    );
    if (!touchesStage) return;
    if (stage.gate?.state === "approved") return; // approval closes the question
    const already = all.some(
      (m) => m.meta?.kind === "review" && m.meta?.job_id === job.id,
    );
    if (already || alreadyAutoChecked(reviewId, job.id)) return;
    // Marked BEFORE the call (strict-mode double effects, a second tab) and
    // kept on failure: a refused check is re-run by hand ("Check again"),
    // never by a retry loop that could spend money on every render.
    markAutoChecked(reviewId, job.id);
    review.mutate(job.id);
  }, [job, all, transcriptLoaded, stage, review, reviewId]);

  const chat = all.filter((m) => m.role !== "system");
  const totalCost = all.reduce((n, m) => n + (m.meta?.cost_usd || 0), 0);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "nearest" });
  }, [chat.length, ask.isPending]);

  function send() {
    const message = text.trim();
    if (message && !ask.isPending) ask.mutate(message);
  }

  return (
    <div className="flex max-h-[min(36rem,calc(100vh-6rem))] flex-col rounded-lg border border-border bg-card">
      {/* Static identity — this is the chatbot, not the stage. */}
      <header className="flex items-center gap-2 border-b border-border px-3 py-2.5 text-sm font-semibold">
        <Bot size={15} className="text-primary" />
        AI helper
      </header>

      {review.isPending && (
        <p className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
          <Loader2 size={12} className="animate-spin" />
          Sanity-checking the last run…
        </p>
      )}

      {/* 3. The conversation. */}
      <div className="min-h-32 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {chat.length === 0 && !ask.isPending && (
          <p className="text-xs text-muted-foreground">
            Ask anything about this review — the assistant follows you through
            every stage and remembers what you decided.
          </p>
        )}
        {chat.map((m) => (
          <Message key={m.id} m={m} />
        ))}
        {ask.isPending && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 size={12} className="animate-spin" />
            Thinking… (this can take up to a minute)
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <footer className="border-t border-border p-2">
        <div className="flex items-end gap-1.5">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={2}
            placeholder="Ask the assistant…"
            className="min-h-9 flex-1 resize-none rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <button
            type="button"
            onClick={send}
            disabled={ask.isPending || !text.trim()}
            className="rounded-md bg-primary p-2 text-primary-foreground hover:opacity-90 disabled:opacity-50"
            title="Send (Enter)"
          >
            <Send size={14} />
          </button>
        </div>
        <div className="mt-1 flex items-center justify-between text-[11px] text-muted-foreground">
          <span className="tabular-nums">
            {totalCost > 0 && `conversation cost $${totalCost.toFixed(3)}`}
          </span>
          {job?.state === "succeeded" && (
            <button
              type="button"
              onClick={() => job && review.mutate(job.id)}
              disabled={review.isPending}
              className="inline-flex items-center gap-1 hover:text-foreground disabled:opacity-50"
            >
              <RefreshCcw size={11} />
              Check again
            </button>
          )}
        </div>
      </footer>
    </div>
  );
}

function Message({ m }: { m: AgentMessageOut }) {
  const isUser = m.role === "user";
  const isReview = m.meta?.kind === "review";
  return (
    <div className={isUser ? "flex justify-end" : "flex justify-start"}>
      <div
        className={`max-w-[85%] whitespace-pre-wrap rounded-lg px-2.5 py-1.5 text-sm leading-relaxed ${
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-foreground"
        }`}
      >
        {isReview && (
          <span className="mb-0.5 block text-[10px] font-medium uppercase tracking-wide opacity-60">
            run check
          </span>
        )}
        {m.content}
      </div>
    </div>
  );
}
