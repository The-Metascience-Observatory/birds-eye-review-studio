"use client";

/** The one button. Replaces ~23 inline button/Link class strings that had
 *  drifted on padding, font weight, and disabled styling.
 *
 *  Polymorphic by `href`: with one it renders a Next `<Link>` (so nav buttons
 *  and action buttons share a look), otherwise a `<button>`. Variants match the
 *  three that already existed in the wild — primary (filled), secondary
 *  (outline), ghost (text-only). */

import Link from "next/link";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost";
type Size = "sm" | "md";

const BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors disabled:opacity-50 disabled:pointer-events-none";

const VARIANT: Record<Variant, string> = {
  primary: "bg-primary text-primary-foreground hover:opacity-90",
  secondary: "border border-border hover:bg-accent",
  ghost: "text-muted-foreground hover:text-foreground hover:bg-accent",
};

const SIZE: Record<Size, string> = {
  sm: "px-2.5 py-1 text-xs",
  md: "px-3 py-1.5 text-sm",
};

function classes(variant: Variant, size: Size, className?: string): string {
  return cn(BASE, VARIANT[variant], SIZE[size], className);
}

type CommonProps = {
  variant?: Variant;
  size?: Size;
  className?: string;
  children: ReactNode;
};

/** `href` present → a Link; absent → a button. */
export function Button(
  props:
    | (CommonProps & { href: string } & Omit<ComponentPropsWithoutRef<typeof Link>, "className" | "href">)
    | (CommonProps & { href?: undefined } & Omit<ComponentPropsWithoutRef<"button">, "className">),
) {
  const { variant = "primary", size = "md", className, children, ...rest } = props;
  const cls = classes(variant, size, className);
  if ("href" in props && props.href != null) {
    const { href, ...linkRest } = rest as { href: string };
    return (
      <Link href={href} className={cls} {...linkRest}>
        {children}
      </Link>
    );
  }
  return (
    <button className={cls} {...(rest as ComponentPropsWithoutRef<"button">)}>
      {children}
    </button>
  );
}
