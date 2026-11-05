"use client";

/** The default body for any stage without a bespoke form.
 *
 *  Drives itself entirely off `StageDef.config_keys`, which the server already
 *  declares for 10 of the 14 stages. That is what lets every stage have a
 *  working, honest page on day one — bespoke bodies then replace this one at a
 *  time, rather than the wizard being unusable until all 14 are hand-built.
 *
 *  Only list-of-string config values get an editor here; anything else is shown
 *  read-only rather than rendered through a control that would misrepresent it.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "@/lib/toast";

import { ListEditorCard } from "@/components/ListEditorCard";
import { useReviewConfig } from "@/hooks/useReview";
import { patchReviewConfig } from "@/lib/api";
import type { StageOut } from "@/lib/types";

/** Read a dotted path ("models.classification") out of the config. */
function at(cfg: Record<string, unknown> | undefined, path: string): unknown {
  let node: unknown = cfg;
  for (const part of path.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

/** Build the nested patch object for a dotted path. PATCH merges server-side,
 *  so sending only this branch cannot clobber a concurrent edit elsewhere. */
function patchFor(path: string, value: unknown): Record<string, unknown> {
  const parts = path.split(".");
  return parts.reduceRight<unknown>(
    (acc, part) => ({ [part]: acc }),
    value,
  ) as Record<string, unknown>;
}

export function GenericStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const config = useReviewConfig(reviewId);

  const save = useMutation({
    mutationFn: (args: { path: string; lines: string[] }) =>
      patchReviewConfig(reviewId, patchFor(args.path, args.lines)),
    onSuccess: () => {
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      // Editing a stage's own keys can make its approval stale.
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Save failed"),
  });

  const cfg = config.data?.config_json;
  const keys = stage.config_keys ?? [];

  if (!keys.length) {
    return (
      <p className="text-sm text-muted-foreground">
        {stage.step_ids.length > 0
          ? "This stage has no settings — run it when you are ready."
          : "Nothing to configure here."}
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {keys.map((key) => {
        const value = at(cfg, key);
        const label = key
          .split(".")
          .pop()!
          .replace(/_/g, " ")
          .replace(/^\w/, (c) => c.toUpperCase());

        if (Array.isArray(value) || value === undefined || value === null) {
          const values = Array.isArray(value) ? value.map(String) : [];
          return (
            <ListEditorCard
              key={key}
              title={label}
              values={values}
              note={key.includes(".") ? key : undefined}
              editing={editing === key}
              draft={draft}
              isPending={save.isPending && editing === key}
              onStartEdit={() => {
                setEditing(key);
                setDraft(values.join("\n"));
              }}
              onCancel={() => setEditing(null)}
              onDraftChange={setDraft}
              onSave={(lines) => save.mutate({ path: key, lines })}
            />
          );
        }

        // Scalars and nested objects: show honestly rather than through a
        // control that would misrepresent the shape.
        return (
          <div key={key} className="rounded-lg border border-border bg-card p-3">
            <div className="text-sm font-medium">{label}</div>
            <pre className="mt-1 overflow-x-auto text-xs text-muted-foreground">
              {JSON.stringify(value, null, 2)}
            </pre>
            <p className="mt-1 text-xs text-muted-foreground">
              Edit this on the settings page — it is not a simple list.
            </p>
          </div>
        );
      })}
    </div>
  );
}
