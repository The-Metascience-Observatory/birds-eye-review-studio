"use client";

/** The one text input. Replaces the ~10 identical
 *  `mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm`
 *  fields across the auth and new-review pages. */

import type { ComponentPropsWithoutRef } from "react";

import { cn } from "@/lib/cn";

export function Input({
  className,
  ...props
}: ComponentPropsWithoutRef<"input">) {
  return (
    <input
      className={cn(
        "w-full rounded-md border border-border bg-background px-3 py-2 text-sm",
        "placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-primary",
        className,
      )}
      {...props}
    />
  );
}
