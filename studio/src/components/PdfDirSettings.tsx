"use client";

/** Account-level PDF download directory. Blank = each review's default
 *  (`projects/<slug>/pdfs`). The server normalizes the path (including
 *  translating Windows `C:\...` paths for WSL), rejects system directories,
 *  and may return non-fatal advice in `pdf_dir_warning`. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "@/lib/toast";

import { getUserSettings, updateUserSettings } from "@/lib/api";
import type { UserSettingsIn } from "@/lib/types";

export function PdfDirSettings() {
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: ["user-settings"],
    queryFn: getUserSettings,
    staleTime: 5 * 60 * 1000,
  });

  const [dir, setDir] = useState("");
  const [dirty, setDirty] = useState(false);

  // Seed from the server once it arrives, unless the user is mid-edit.
  useEffect(() => {
    if (settings.data && !dirty) setDir(settings.data.pdf_dir ?? "");
  }, [settings.data, dirty]);

  const save = useMutation({
    mutationFn: (patch: UserSettingsIn) => updateUserSettings(patch),
    onSuccess: (data) => {
      toast.success(data.pdf_dir ? "PDF folder saved" : "Using the default PDF folder");
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["user-settings"] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not save the PDF folder"),
  });

  const warning = !dirty ? settings.data?.pdf_dir_warning : null;

  if (settings.isLoading) {
    return (
      <section className="mt-6 rounded-lg border border-border bg-card p-4">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={14} className="animate-spin" /> Loading…
        </p>
      </section>
    );
  }

  return (
    <section className="mt-6 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold">PDF download folder</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Where full-text PDFs are saved. Leave blank to keep them inside each
        review&rsquo;s own project folder. On Windows you can paste a path like{" "}
        <code className="rounded bg-muted px-1 py-0.5">C:\Users\you\PDFs</code>{" "}
        &mdash; it&rsquo;s converted for WSL automatically.
      </p>

      <div className="mt-3">
        <input
          type="text"
          spellCheck={false}
          value={dir}
          onChange={(e) => {
            setDir(e.target.value);
            setDirty(true);
          }}
          placeholder="(default: inside each review's project folder)"
          className="w-full rounded border border-border bg-background px-2 py-1.5 font-mono text-sm placeholder:font-sans placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-primary"
        />
        {settings.data?.pdf_dir && !dirty && (
          <p className="mt-1 text-[11px] text-muted-foreground">
            Saved as{" "}
            <code className="rounded bg-muted px-1 py-0.5">{settings.data.pdf_dir}</code>
          </p>
        )}
      </div>

      {warning && (
        <p className="mt-3 flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-[11px] text-amber-700 dark:text-amber-400">
          <AlertTriangle size={13} className="mt-px shrink-0" />
          <span>{warning}</span>
        </p>
      )}

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={() => save.mutate({ pdf_dir: dir.trim() })}
          disabled={save.isPending || !dirty}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {save.isPending && <Loader2 size={13} className="animate-spin" />}
          Save
        </button>
        {dirty && dir.trim() === "" && (
          <span className="text-xs text-muted-foreground">
            Saving blank restores the default.
          </span>
        )}
      </div>
    </section>
  );
}
