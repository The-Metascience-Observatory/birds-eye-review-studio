"use client";

/** Find papers: per-source search queries.
 *
 *  The manual editor is ALWAYS rendered, one card per source. Previously the
 *  whole editor was hidden until queries existed, so a brand-new review had no
 *  way to type any — the only path was the AI button, and if that returned
 *  nothing the page looked identical to "nothing happened". AI drafting is now
 *  one option beside manual entry, not the only door.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { useState } from "react";
import { toast } from "@/lib/toast";

import { ListEditorCard } from "@/components/ListEditorCard";
import { useReviewConfig } from "@/hooks/useReview";
import { draftSearchQueries, patchReviewConfig } from "@/lib/api";
import type { StageOut } from "@/lib/types";

/** Mirrors services/search.py's source list. */
const SOURCES: { key: string; label: string }[] = [
  { key: "pubmed", label: "PubMed" },
  { key: "openalex_title", label: "OpenAlex (title)" },
  { key: "openalex_abstract", label: "OpenAlex (abstract)" },
  { key: "europepmc", label: "Europe PMC" },
  { key: "semantic_scholar", label: "Semantic Scholar" },
  { key: "cyberleninka", label: "CyberLeninka" },
];

export function SearchStage({
  reviewId,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [cost, setCost] = useState<number | null>(null);

  const config = useReviewConfig(reviewId);

  const search = (config.data?.config_json?.search ?? {}) as Record<
    string,
    string[] | undefined
  >;
  const total = Object.values(search).reduce((n, v) => n + (v?.length ?? 0), 0);

  const save = useMutation({
    mutationFn: (args: { src: string; lines: string[] }) =>
      // PATCH, not PUT: the server merges, so saving one source cannot clobber
      // a concurrent edit to another.
      patchReviewConfig(reviewId, { search: { [args.src]: args.lines } }),
    onSuccess: () => {
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Save failed"),
  });

  const ai = useMutation({
    mutationFn: () => draftSearchQueries(reviewId),
    onSuccess: (res) => {
      setCost(res.meta?.cost_usd ?? null);
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
      toast.success("Search terms drafted");
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Drafting failed"),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card p-3">
        <div className="min-w-0">
          <div className="text-sm font-medium">
            {total > 0
              ? `${total} quer${total === 1 ? "y" : "ies"} across ${
                  Object.values(search).filter((v) => v?.length).length
                } sources`
              : "No search queries yet"}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {total > 0
              ? "Edit any source below, or re-draft with AI."
              : "Write queries yourself below, or let Claude draft them from your topic and synonyms."}
            {cost != null && ` · last draft cost $${cost.toFixed(3)}`}
          </p>
        </div>
        <button
          type="button"
          onClick={() => ai.mutate()}
          disabled={ai.isPending}
          className="inline-flex shrink-0 items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-accent disabled:opacity-60"
        >
          <Sparkles size={15} />
          {ai.isPending ? "Drafting… (10–30s)" : "Draft with AI"}
        </button>
      </div>

      {SOURCES.map((src) => {
        const values = search[src.key] ?? [];
        return (
          <ListEditorCard
            key={src.key}
            title={src.label}
            values={values}
            note={values.length === 0 ? "No queries for this source yet." : undefined}
            placeholder="One query per line"
            editing={editing === src.key}
            draft={draftText}
            isPending={save.isPending && editing === src.key}
            onStartEdit={() => {
              setEditing(src.key);
              setDraftText(values.join("\n"));
            }}
            onCancel={() => setEditing(null)}
            onDraftChange={setDraftText}
            onSave={(lines) => save.mutate({ src: src.key, lines })}
          />
        );
      })}
    </div>
  );
}
