"use client";

/** The one callout banner. Replaces the amber/red/muted note patterns that were
 *  re-declared ~13× (StageShell, AgentRail, BackgroundStageBar, and the
 *  ExportStage/MetaStage/CurateStage/SetupStage info-notes — one of which had
 *  drifted to `bg-muted` instead of `bg-muted/40`).
 *
 *  `tone="info"` is the muted explanatory note; `warning`/`error` are the
 *  amber/red alert styles, theme-safe in both light and dark. */

import { AlertTriangle, Info, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

type Tone = "info" | "warning" | "error";

const TONE: Record<Tone, { cls: string; Icon: typeof Info }> = {
  info: {
    cls: "border-border bg-muted/40 text-muted-foreground",
    Icon: Info,
  },
  warning: {
    cls: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    Icon: AlertTriangle,
  },
  error: {
    cls: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400",
    Icon: XCircle,
  },
};

export function Banner({
  tone = "info",
  icon,
  children,
  className,
}: {
  tone?: Tone;
  /** Override the tone's default icon (e.g. a Lock for a blocked stage). */
  icon?: typeof Info;
  children: ReactNode;
  className?: string;
}) {
  const { cls, Icon: DefaultIcon } = TONE[tone];
  const Icon = icon ?? DefaultIcon;
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-md border px-3 py-2 text-sm",
        cls,
        className,
      )}
    >
      <Icon size={15} className="mt-0.5 shrink-0" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}
