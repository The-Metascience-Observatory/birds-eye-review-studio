"use client";

/** User preferences: client-only view toggles (localStorage, see lib/prefs.ts)
 *  PLUS account-level settings that do hit the server (AI provider, PDF
 *  download folder). */

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { AiProviderSettings } from "@/components/AiProviderSettings";
import { ClaudeSessionSettings } from "@/components/ClaudeSessionSettings";
import { PdfDirSettings } from "@/components/PdfDirSettings";
import { TopBar } from "@/components/TopBar";
import { loginUrlForHere, useAuth } from "@/lib/auth";
import { usePref } from "@/lib/prefs";

export default function SettingsPage() {
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();
  const [debug, setDebug] = usePref("debugMode");
  const [autoPdf, setAutoPdf] = usePref("autoOpenPdf");

  useEffect(() => {
    if (ready && !isAuthenticated) router.replace(loginUrlForHere());
  }, [ready, isAuthenticated, router]);

  return (
    <div>
      <TopBar />
      <main className="mx-auto max-w-2xl px-4 py-10">
        <h1 className="text-2xl font-bold">Preferences</h1>

        <section className="mt-6 space-y-4 rounded-lg border border-border bg-card p-4">
          <label className="flex items-start gap-3">
            <input
              type="checkbox"
              checked={debug}
              onChange={(e) => setDebug(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
            />
            <span className="min-w-0">
              <span className="text-sm font-medium">Debug mode</span>
              <span className="mt-0.5 block text-xs text-muted-foreground">
                Show the raw job terminal (the live log console) on the review
                overview and stage pages. Off by default — most work is legible
                from the progress bars alone.
              </span>
            </span>
          </label>

          <label className="flex items-start gap-3">
            <input
              type="checkbox"
              checked={autoPdf}
              onChange={(e) => setAutoPdf(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
            />
            <span className="min-w-0">
              <span className="text-sm font-medium">
                Show the PDF automatically in paper detail
              </span>
              <span className="mt-0.5 block text-xs text-muted-foreground">
                When you open a paper&apos;s detail view, its downloaded PDF
                opens alongside it right away (when one exists) instead of
                waiting for a click on &ldquo;Show downloaded PDF&rdquo;.
                Desktop-width windows only.
              </span>
            </span>
          </label>
        </section>

        <AiProviderSettings />
        <ClaudeSessionSettings />
        <PdfDirSettings />
      </main>
    </div>
  );
}
