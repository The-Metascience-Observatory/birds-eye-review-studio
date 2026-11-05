"use client";

/** "+ Add chart" — the writable version of PanelSkipList's "panels you could
 *  add". Lists every shell-renderable panel not currently on the dashboard,
 *  with its one-line "why"; entries that cannot render are disabled with the
 *  stated reason (missing data, unsupported pooling) rather than hidden —
 *  a silently absent option is the failure mode this project exists to remove.
 */

import { Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export interface AddChartOption {
  key: string;
  label: string;
  why: string;
  /** Present → the entry is disabled, showing this reason. */
  disabledReason?: string;
}

export function AddChartPicker({
  options,
  onAdd,
}: {
  options: AddChartOption[];
  onAdd: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click — a popover that only closes via its own button
  // fights the rest of the edit toolbar.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm font-medium hover:bg-accent"
      >
        <Plus size={14} />
        Add chart
      </button>
      {open && (
        <div className="absolute left-0 z-20 mt-1 max-h-96 w-80 overflow-y-auto rounded-md border border-border bg-card p-1.5 shadow-lg">
          {options.length === 0 && (
            <p className="px-2 py-2 text-sm text-muted-foreground">
              Every available chart is already on the dashboard.
            </p>
          )}
          {options.map((o) => (
            <button
              key={o.key}
              type="button"
              disabled={!!o.disabledReason}
              onClick={() => {
                onAdd(o.key);
                setOpen(false);
              }}
              className="block w-full rounded px-2 py-1.5 text-left hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
            >
              <span className="block text-sm font-medium">{o.label}</span>
              <span className="block text-xs text-muted-foreground">
                {o.disabledReason ?? o.why}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
