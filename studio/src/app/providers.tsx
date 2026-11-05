"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Toaster, toast } from "sonner";

/** App-wide client providers: react-query cache + toast host.
 *
 *  ERRORS AND WARNINGS NEVER AUTO-DISMISS (`duration: Infinity`, set per call in
 *  lib/toast.ts). A failed screen or a rejected run is often the only place the
 *  server's explanation appears; a message that vanishes after four seconds —
 *  while the user is reading something else, or away from the screen — is the
 *  same as no message at all. They stay until explicitly closed. Successes still
 *  auto-dismiss: they carry no information the user needs to act on.
 *
 *  EVERY toast — errors and warnings included — dismisses on a click anywhere on
 *  it (an explicit product decision; it previously exempted errors/warnings so
 *  they could be drag-selected). sonner exposes no per-toast id in the DOM, so a
 *  click dismisses all visible ones — predictable, and there is rarely more than
 *  one. Two guards keep the click cheap, not destructive:
 *
 *  - The buttons own their behaviour: Copy keeps the toast (it copies the very
 *    message you'd otherwise lose), ✕ closes it.
 *  - A click is ignored while text is selected, so the mouseup that finishes a
 *    drag-select never deletes the message being copied. Copy-by-hand therefore
 *    still works; the Copy button (lib/toast.ts) stays the reliable path.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            refetchOnWindowFocus: false,
            staleTime: 10_000,
          },
        },
      }),
  );

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const target = e.target as Element | null;
      const toastEl = target?.closest?.("[data-sonner-toast]");
      if (!toastEl) return;
      // The buttons own their behaviour (Copy keeps the toast; ✕ closes it).
      if (target?.closest("[data-action],[data-cancel],[data-close-button]"))
        return;
      // Don't let the click that completes a text selection eat the message.
      if (!window.getSelection()?.isCollapsed) return;
      toast.dismiss();
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  return (
    <QueryClientProvider client={client}>
      {children}
      <Toaster
        richColors
        closeButton
        position="top-right"
        // Successes fade after 6s. Errors/warnings are made permanent at the
        // call site by lib/toast.ts (sonner 1.7 has no per-type duration).
        //
        // Everything is click-to-dismiss, so everything gets the pointer cursor.
        // Errors/warnings keep select-text so drag-selecting the message by hand
        // still works (the click guard in the handler above protects it).
        toastOptions={{
          classNames: {
            toast: "cursor-pointer",
            error: "select-text",
            warning: "select-text",
          },
          duration: 6000,
        }}
      />
    </QueryClientProvider>
  );
}
