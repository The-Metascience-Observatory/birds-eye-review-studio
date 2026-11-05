"use client";

/** The run-check panel — the AI agent's post-run "blocker" warnings, in their own
 *  column to the RIGHT of the agent rail (not crammed inside the chat).
 *
 *  Self-contained: it reads the same `["agent-messages", reviewId]` query the
 *  rail uses, so dismissing a card here updates the rail's view too (shared
 *  react-query cache — no lifted state). Renders nothing when there are no
 *  blockers for this stage, so the column collapses.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, X } from "lucide-react";
import { useMemo } from "react";

import { agentDismiss, getAgentMessages } from "@/lib/api";
import type { AgentBlocker, StageOut } from "@/lib/types";

export function BlockersPanel({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();

  const messages = useQuery({
    queryKey: ["agent-messages", reviewId],
    queryFn: () => getAgentMessages(reviewId),
  });

  const dismiss = useMutation({
    mutationFn: (args: { code: string; reason: string }) =>
      agentDismiss(reviewId, stage.slug, args.code, args.reason),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["agent-messages", reviewId] }),
  });

  // Blockers for THIS stage: latest review message's cards, minus dismissals
  // (system rows carry those, so messages alone reconstruct the state).
  const blockers = useMemo(() => {
    const all = messages.data ?? [];
    const dismissed = new Set(
      all
        .filter((m) => m.role === "system" && m.meta?.kind === "dismiss")
        .map((m) => m.meta?.code)
        .filter((c): c is string => !!c),
    );
    const lastReview = [...all]
      .reverse()
      .find(
        (m) =>
          m.role === "assistant" &&
          m.meta?.kind === "review" &&
          m.stage === stage.slug,
      );
    const cards: AgentBlocker[] = lastReview?.meta?.blockers ?? [];
    return cards.filter((b) => !dismissed.has(b.code));
  }, [messages.data, stage.slug]);

  // Nothing to show → render nothing, so the flex row gives this column zero
  // width (no reserved white space beside the agent rail).
  if (blockers.length === 0) return null;

  return (
    <aside className="hidden w-80 shrink-0 lg:block">
     <div className="sticky top-6 rounded-lg border border-border bg-card">
      <header className="flex items-center gap-2 border-b border-border px-3 py-2.5 text-sm font-semibold">
        <AlertTriangle size={15} className="text-amber-500" />
        Run checks
      </header>
      <div className="space-y-2 p-3">
        {blockers.map((b) => (
          <div
            key={b.code}
            className={`rounded border p-2 text-xs ${
              b.severity === "high"
                ? "border-red-500/40 bg-red-500/10"
                : "border-amber-500/40 bg-amber-500/10"
            }`}
          >
            <div className="flex items-start gap-1.5">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="font-medium">{b.message}</p>
                {b.evidence && (
                  <p className="mt-0.5 text-muted-foreground">
                    evidence: {b.evidence}
                  </p>
                )}
                {b.suggested_fix && <p className="mt-0.5">{b.suggested_fix}</p>}
              </div>
              <button
                type="button"
                title="Dismiss — I know, proceed"
                onClick={() =>
                  dismiss.mutate({ code: b.code, reason: "dismissed from rail" })
                }
                className="shrink-0 text-muted-foreground hover:text-foreground"
              >
                <X size={13} />
              </button>
            </div>
          </div>
        ))}
      </div>
     </div>
    </aside>
  );
}
