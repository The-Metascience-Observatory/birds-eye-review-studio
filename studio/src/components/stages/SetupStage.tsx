"use client";

/** Define review — what the review is about, and what it is not about.
 *
 *  Editable at any time, not just at the start. Scope is the field users most
 *  often revise once they have seen what the search actually returned ("this
 *  keeps pulling in animal studies — add that to not_about"), so this stage has
 *  to be a place you can come BACK to. Editing it correctly marks the setup
 *  approval stale, which is the gate doing its job rather than a nuisance:
 *  the corpus was screened against the old scope.
 *
 *  `topic.name` is the one field that cannot change here — it determines the
 *  workspace directory, so a rename would orphan every artifact already
 *  written. The server refuses it (422); this UI shows it read-only with the
 *  reason, rather than offering an edit that will fail.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock } from "lucide-react";
import { useState } from "react";
import { toast } from "@/lib/toast";

import { ListEditorCard } from "@/components/ListEditorCard";
import { Banner } from "@/components/ui/Banner";
import { useReviewConfig } from "@/hooks/useReview";
import { patchReviewConfig } from "@/lib/api";
import type { ReviewConfigOut, StageOut } from "@/lib/types";

export function SetupStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  // Which free-text field is being edited (question|description), and its draft.
  const [textEdit, setTextEdit] = useState<{ key: string; value: string } | null>(null);

  const config = useReviewConfig(reviewId);

  const topic = (config.data?.config_json?.topic ?? {}) as {
    name?: string;
    question?: string;
    synonyms?: string[];
    description?: string;
    not_about?: string[];
  };
  const includeObs = config.data?.config_json?.include_observational !== false;

  /** PATCH one field at a time. The server merges one level deep, so sending
   *  `{topic: {synonyms}}` preserves `topic.name` — no read-modify-write here,
   *  and no way to clobber a concurrent edit to a sibling field. */
  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) =>
      patchReviewConfig(reviewId, patch),
    onSuccess: () => {
      setEditing(null);
      setTextEdit(null);
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      // Scope edits can stale this stage's approval — refresh the gate.
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Save failed"),
  });

  const approved = stage.gate?.state === "approved";

  return (
    <div className="space-y-4">
      {approved && (
        <Banner tone="info">
          You&apos;ve approved this stage. Editing the scope below will mark it
          for re-approval — later stages screened papers against the old scope,
          so the change should be deliberate.
        </Banner>
      )}

      {/* The immutable one. Shown, not hidden: users look for it. */}
      <div className="rounded-lg border border-border bg-card p-3">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-medium">Topic name</div>
            <div className="mt-0.5 truncate text-sm text-muted-foreground">
              {topic.name || "—"}
            </div>
          </div>
          <span
            className="inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
            title="The topic name sets the workspace directory. Renaming would orphan every file already written under the old name, so it needs a migration rather than an edit."
          >
            <Lock size={12} />
            fixed
          </span>
        </div>
        <p className="mt-1.5 text-xs text-muted-foreground">
          Fixed after creation — it names the folder holding this review&apos;s
          data. To rename, create a new review.
        </p>
      </div>

      {/* Free-text fields. The research question leads search-term drafting, so
          it is editable here for when the user revises scope after seeing what
          the search returned. */}
      <TextCard
        title="Research question"
        hint="Plain-language question the AI drafts search terms from. Optional — leave blank to work from the topic and synonyms."
        value={topic.question ?? ""}
        editing={textEdit?.key === "question"}
        draft={textEdit?.key === "question" ? textEdit.value : ""}
        pending={save.isPending}
        onStart={() => setTextEdit({ key: "question", value: topic.question ?? "" })}
        onChange={(v) => setTextEdit({ key: "question", value: v })}
        onCancel={() => setTextEdit(null)}
        onSave={(v) => save.mutate({ topic: { question: v.trim() } })}
      />

      <TextCard
        title="Description"
        hint="One or two sentences on what counts as in-scope. The AI uses this when drafting search terms and screening abstracts."
        value={topic.description ?? ""}
        editing={textEdit?.key === "description"}
        draft={textEdit?.key === "description" ? textEdit.value : ""}
        pending={save.isPending}
        onStart={() => setTextEdit({ key: "description", value: topic.description ?? "" })}
        onChange={(v) => setTextEdit({ key: "description", value: v })}
        onCancel={() => setTextEdit(null)}
        onSave={(v) => save.mutate({ topic: { description: v.trim() } })}
      />

      <ListEditorCard
        title="Synonyms"
        values={topic.synonyms ?? []}
        note="Alternative names for the intervention or condition. Widens the search — the search stage reads these too."
        placeholder="One synonym per line"
        editing={editing === "synonyms"}
        draft={draft}
        isPending={save.isPending && editing === "synonyms"}
        onStartEdit={() => {
          setEditing("synonyms");
          setDraft((topic.synonyms ?? []).join("\n"));
        }}
        onCancel={() => setEditing(null)}
        onDraftChange={setDraft}
        onSave={(lines) => save.mutate({ topic: { synonyms: lines } })}
      />

      <ListEditorCard
        title="Not about (exclusions)"
        values={topic.not_about ?? []}
        note="Topics that look similar but are out of scope. Screening uses these to reject papers — the most common thing to revise once you see what the search returns."
        placeholder="One exclusion per line"
        editing={editing === "not_about"}
        draft={draft}
        isPending={save.isPending && editing === "not_about"}
        onStartEdit={() => {
          setEditing("not_about");
          setDraft((topic.not_about ?? []).join("\n"));
        }}
        onCancel={() => setEditing(null)}
        onDraftChange={setDraft}
        onSave={(lines) => save.mutate({ topic: { not_about: lines } })}
      />

      {/* Changing this adds or removes whole pipeline steps, so it warns. */}
      <div className="rounded-lg border border-border bg-card p-3">
        <label className="flex items-start gap-2.5">
          <input
            type="checkbox"
            checked={includeObs}
            disabled={save.isPending}
            onChange={(e) =>
              save.mutate({ include_observational: e.target.checked })
            }
            className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
          />
          <span className="min-w-0">
            <span className="text-sm font-medium">
              Include observational studies
            </span>
            <span className="mt-0.5 block text-xs text-muted-foreground">
              Off means RCTs only. This adds or removes the observational
              extraction step, so turning it off after extraction has run leaves
              those results in place but stops refreshing them.
            </span>
          </span>
        </label>
        {!includeObs && (
          <p className="mt-2 flex items-start gap-1.5 text-xs text-amber-600">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            RCT-only: the observational extraction step is skipped for this
            review.
          </p>
        )}
      </div>
    </div>
  );
}

/** A free-text config field with an inline edit toggle. Used for the research
 *  question and the description — both prose, both editable at any time. Only
 *  one is open at a time (the parent keys `editing` on a shared discriminator). */
function TextCard({
  title,
  hint,
  value,
  editing,
  draft,
  pending,
  onStart,
  onChange,
  onCancel,
  onSave,
}: {
  title: string;
  hint: string;
  value: string;
  editing: boolean;
  draft: string;
  pending: boolean;
  onStart: () => void;
  onChange: (v: string) => void;
  onCancel: () => void;
  onSave: (v: string) => void;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-medium">{title}</div>
        {!editing && (
          <button
            type="button"
            onClick={onStart}
            className="text-xs text-blue-600 hover:text-blue-700"
          >
            Edit
          </button>
        )}
      </div>
      <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>
      {!editing ? (
        <p className="mt-2 whitespace-pre-wrap text-sm">
          {value || <span className="text-muted-foreground">Not set.</span>}
        </p>
      ) : (
        <div className="mt-2">
          <textarea
            value={draft}
            onChange={(e) => onChange(e.target.value)}
            rows={3}
            className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <div className="mt-1.5 flex gap-2">
            <button
              type="button"
              disabled={pending}
              onClick={() => onSave(draft)}
              className="rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-60"
            >
              {pending ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
