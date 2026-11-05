"use client";

/** Warns when AI steps would run under SOMEONE ELSE's Claude account.
 *
 *  This server shares one `claude` CLI login, so whoever holds the lease pays
 *  for every AI step run on it. Silent mis-billing is the failure this exists to
 *  prevent — so the warning is app-wide, not buried in Settings. */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { getLlmSession } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function ClaudeSessionBanner() {
  const { isAuthenticated, ready } = useAuth();
  const q = useQuery({
    queryKey: ["llm-session"],
    queryFn: getLlmSession,
    enabled: ready && isAuthenticated,
    refetchInterval: 30_000,
    // A failure here must never block the app — the banner just doesn't show.
    retry: false,
  });

  const s = q.data;
  if (!s) return null;
  // OpenRouter users use their own key; the shared CLI is irrelevant to them.
  if (s.provider === "openrouter") return null;
  // Only speak up when someone ELSE holds a live lease.
  if (!s.held || s.held_by_me || s.expired) return null;

  return (
    <div className="border-b border-amber-500/40 bg-amber-500/10">
      <p className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-2 text-xs text-amber-800 dark:text-amber-300">
        <AlertTriangle size={13} className="shrink-0" />
        <span className="min-w-0">
          AI steps are currently reserved by <strong>{s.holder_email}</strong>
          {s.claude_account && <> (account {s.claude_account})</>}. Runs you
          start will be refused until they release it.{" "}
          <Link href="/settings" className="underline hover:no-underline">
            Details
          </Link>
        </span>
      </p>
    </div>
  );
}
