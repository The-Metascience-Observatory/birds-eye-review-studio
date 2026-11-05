"use client";

import { Check, Loader2, Pencil, RotateCcw, X } from "lucide-react";

/** A card that edits one list of strings as newline-separated text.
 *
 * Editing is controlled by the parent so only one card in a group is open at a
 * time (pass the same `editing` discriminator you use for the others).
 */
export function ListEditorCard({
  title,
  values,
  note,
  placeholder,
  editing,
  draft,
  isPending,
  onStartEdit,
  onCancel,
  onDraftChange,
  onSave,
  onReset,
}: {
  title: string;
  values: string[];
  /** Inline caveat shown under the title — use for non-obvious effects. */
  note?: string;
  placeholder?: string;
  editing: boolean;
  draft: string;
  isPending?: boolean;
  onStartEdit: () => void;
  onCancel: () => void;
  onDraftChange: (text: string) => void;
  onSave: (lines: string[]) => void;
  /** Omit to hide the "Reset to defaults" action. */
  onReset?: () => void;
}) {
  const save = () =>
    onSave(
      draft
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean),
    );

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 text-sm font-medium">
          {title}{" "}
          <span className="text-muted-foreground">({values.length})</span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {editing ? (
            <>
              <button
                onClick={save}
                disabled={isPending}
                className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-green-700 hover:bg-green-50 disabled:opacity-60"
              >
                {isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Check className="h-3.5 w-3.5" />
                )}
                Save
              </button>
              <button
                onClick={onCancel}
                className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
                Cancel
              </button>
            </>
          ) : (
            <>
              {onReset && (
                <button
                  onClick={onReset}
                  disabled={isPending}
                  title="Restore the standard list"
                  className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-60"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  Reset
                </button>
              )}
              <button
                onClick={onStartEdit}
                className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
              >
                <Pencil className="h-3.5 w-3.5" />
                Edit
              </button>
            </>
          )}
        </div>
      </div>

      {note && (
        <p className="mt-1 text-[11px] text-muted-foreground">{note}</p>
      )}

      {editing ? (
        <>
          <textarea
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            rows={Math.max(3, Math.min(20, values.length + 2))}
            spellCheck={false}
            placeholder={placeholder}
            className="mt-2 w-full resize-y rounded-md border border-border bg-background p-2 font-mono text-xs leading-relaxed"
          />
          <p className="mt-1 text-[11px] text-muted-foreground">
            One entry per line.
          </p>
        </>
      ) : values.length ? (
        <ul className="mt-1 space-y-1">
          {values.map((v, i) => (
            <li
              key={i}
              className="break-words font-mono text-xs text-muted-foreground"
            >
              {v}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-xs text-muted-foreground">
          Not set — the pipeline default applies.
        </p>
      )}
    </div>
  );
}
