"use client";

/** The one empty-state. Unifies the scattered "No X yet — <hint>" messages that
 *  differed in wording and styling (bare <p> vs bordered card vs centered cell).
 *  `bordered` picks the card treatment (page-level) vs. the inline treatment
 *  (inside a table/section). */

import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export function EmptyState({
  children,
  bordered = false,
  className,
}: {
  children: ReactNode;
  bordered?: boolean;
  className?: string;
}) {
  return (
    <p
      className={cn(
        "text-center text-sm text-muted-foreground",
        bordered ? "rounded-lg border border-border bg-card p-8" : "py-8",
        className,
      )}
    >
      {children}
    </p>
  );
}
