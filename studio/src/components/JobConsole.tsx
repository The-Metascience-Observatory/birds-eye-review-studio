"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp } from "lucide-react";
import type { JobOut } from "@/lib/types";

/** How many lines the collapsed / expanded terminal keeps on screen. */
const COLLAPSED_LINES = 3;
const EXPANDED_LINES = 500;

/**
 * Fake-terminal panel streaming a job's stdout/stderr.
 *
 * Lines arrive over SSE via `useJob`; this component only renders them and
 * owns the collapse toggle + pinned-to-bottom autoscroll.
 */
export function JobConsole({
  job,
  jobActive,
  logLines,
  stuckQueued,
  stepLabel,
}: {
  job: JobOut;
  jobActive: boolean;
  logLines: string[];
  stuckQueued?: boolean;
  stepLabel?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);

  // Auto-scroll to the newest output, but only while the user is already
  // pinned to the bottom (so scrolling up to read doesn't yank them).
  const termRef = useRef<HTMLPreElement>(null);
  const pinnedRef = useRef(true);
  useEffect(() => {
    const el = termRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [logLines]);
  const onScroll = () => {
    const el = termRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const all = logLines.length
    ? logLines
    : jobActive
      ? ["Waiting for output…"]
      : ["(no output)"];
  const shown = expanded ? all.slice(-EXPANDED_LINES) : all.slice(-COLLAPSED_LINES);

  return (
    <div>
      {stuckQueued && (
        <p className="mb-2 flex items-center gap-1.5 text-sm text-amber-600">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          Job is queued but not starting — is the worker running?
          (<code>./dev.sh status</code>)
        </p>
      )}

      <div className="overflow-hidden rounded-md border border-black/20 bg-[#0b0f19]">
        <div className="flex items-center justify-between border-b border-white/10 px-3 py-1.5">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-red-500/70" />
            <span className="h-2.5 w-2.5 rounded-full bg-yellow-500/70" />
            <span className="h-2.5 w-2.5 rounded-full bg-green-500/70" />
            <span className="ml-2 truncate font-mono text-[11px] text-white/50">
              terminal{stepLabel ? ` · ${stepLabel}` : ""}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {jobActive ? (
              <span className="flex items-center gap-1.5 text-[11px] text-green-400">
                <span className="h-2 w-2 animate-pulse rounded-full bg-green-400" />
                live
              </span>
            ) : (
              <span
                className={`text-[11px] ${
                  job.state === "failed"
                    ? "text-red-400"
                    : job.state === "succeeded"
                      ? "text-green-400"
                      : "text-white/50"
                }`}
              >
                {job.state}
              </span>
            )}
            <button
              onClick={() => setExpanded((v) => !v)}
              className="flex items-center gap-1 text-[11px] text-white/60 hover:text-white"
            >
              {expanded ? (
                <>
                  <ChevronUp className="h-3.5 w-3.5" aria-hidden /> Show less
                </>
              ) : (
                <>
                  <ChevronDown className="h-3.5 w-3.5" aria-hidden /> Show more
                </>
              )}
            </button>
          </div>
        </div>
        <pre
          ref={termRef}
          onScroll={onScroll}
          className={`overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-[11.5px] leading-relaxed text-green-200/90 ${
            expanded ? "max-h-96" : "max-h-[4.5rem]"
          }`}
        >
          {shown.join("\n")}
        </pre>
      </div>
    </div>
  );
}
