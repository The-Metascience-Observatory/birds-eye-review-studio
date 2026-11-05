"use client";

/** Loading skeleton. `SkeletonCard` is the single card-shaped pulse used inline
 *  (was copy-pasted in admin/publish, MetaStage, PrismaCard). */

import { cn } from "@/lib/cn";

export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "h-40 animate-pulse rounded-lg border border-border bg-muted",
        className,
      )}
    />
  );
}
