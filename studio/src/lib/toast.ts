/** Toasts, with errors and warnings that DO NOT auto-dismiss and CAN be copied.
 *
 *  Import this instead of `sonner` directly:
 *      import { toast } from "@/lib/toast";
 *
 *  Why a wrapper: a failed screen or a rejected run is often the only place the
 *  server's explanation is shown. A message that disappears after four seconds —
 *  while the user is reading the table, or away from the screen — is the same as
 *  no message at all. Errors and warnings therefore stay until the user
 *  dismisses them (a click anywhere on the toast, or the ✕ — see providers.tsx).
 *
 *  They also carry a **Copy** button. Getting the text out by hand is
 *  surprisingly hostile otherwise: sonner dismisses a toast when you drag across
 *  it past the swipe threshold, so selecting a long message can delete the very
 *  thing you were selecting. One click is reliable; manual selection still works
 *  as a fallback (providers.tsx ignores the click that completes a selection).
 *
 *  sonner 1.7 has no per-type duration, so persistence can't be configured on
 *  `<Toaster>`; it has to be set per call. Successes keep the global 6s.
 *
 *  The re-exported surface is API-compatible with sonner's `toast`, so existing
 *  call sites work unchanged after switching the import.
 */
import { toast as sonner } from "sonner";

type ToastOptions = Parameters<typeof sonner.error>[1];
type ToastMessage = Parameters<typeof sonner.error>[0];
/** `copy: false` skips the Copy button (short auth failures, etc.). */
type StudioToastOptions = ToastOptions & { copy?: boolean };

/** The plain-text form of a toast, for the clipboard.
 *
 *  Only string-ish content is copyable: a ReactNode message would need
 *  rendering to text, and no call site passes one today.
 */
function plainText(message: ToastMessage, opts?: ToastOptions): string {
  const parts: string[] = [];
  const push = (v: unknown) => {
    if (typeof v === "string" || typeof v === "number") {
      const s = String(v).trim();
      if (s) parts.push(s);
    }
  };
  push(message);
  push(opts?.description);
  return parts.join("\n\n");
}

/** Copy `text`, returning whether it worked.
 *
 *  `navigator.clipboard` exists ONLY in a secure context. The studio is
 *  routinely opened over plain http on a LAN address (http://<lan-ip>:3002), where
 *  it is `undefined` — so the deprecated `execCommand` path is not a legacy
 *  nicety here, it is the path that actually runs.
 */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission denied or blocked — fall through to the textarea path.
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    // Off-screen but focusable; `position: fixed` avoids scrolling the page.
    ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** Never auto-dismiss, and offer a one-click Copy, unless the caller overrides. */
function persistent(message: ToastMessage, opts?: StudioToastOptions): ToastOptions {
  const { copy: withCopy = true, ...rest } = opts ?? {};
  const base = { duration: Infinity, ...rest } as NonNullable<ToastOptions>;
  const text = plainText(message, rest);
  // Nothing copyable, caller already owns the action slot, or they opted out.
  if (!withCopy || !text || base.action) return base;

  return {
    ...base,
    action: {
      label: "Copy",
      onClick: (event) => {
        // sonner dismisses the toast after an action fires unless the event is
        // defaultPrevented. Copying must NOT close the message being copied.
        event.preventDefault();
        void writeClipboard(text).then((ok) =>
          ok
            ? sonner.success("Copied to clipboard", { duration: 2000 })
            : sonner.message("Press Ctrl+C to copy the selected text", {
                duration: 4000,
              }),
        );
      },
    },
  };
}

export const toast = Object.assign(
  // Bare toast(...) — neutral, keeps the default duration.
  (message: Parameters<typeof sonner>[0], opts?: ToastOptions) =>
    sonner(message, opts),
  sonner,
  {
    /** Stays until dismissed, with a Copy button (pass `{ copy: false }` to skip). */
    error: (message: ToastMessage, opts?: StudioToastOptions) =>
      sonner.error(message, persistent(message, opts)),

    /** Stays until dismissed, with a Copy button (pass `{ copy: false }` to skip). */
    warning: (message: ToastMessage, opts?: StudioToastOptions) =>
      sonner.warning(message, persistent(message, opts)),
  },
);
