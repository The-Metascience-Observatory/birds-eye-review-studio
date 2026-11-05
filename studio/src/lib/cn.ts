/** Join class names, dropping falsy values. A dependency-free `clsx` — enough
 *  for the ui/ primitives to merge their variant classes with a passthrough
 *  `className`. Later classes win only by Tailwind's own source order, so a
 *  primitive puts its defaults first and the caller's `className` last. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
