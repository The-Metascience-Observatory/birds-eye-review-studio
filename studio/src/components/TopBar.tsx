"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Bird, LogOut, Settings, Shield, Wallet } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { useIsAdmin } from "@/hooks/useMe";
import { ClaudeSessionBanner } from "@/components/ClaudeSessionBanner";

/** App shell top bar: brand, nav, and sign-out. The balance chip is a
 *  placeholder stub — real billing lands in a later phase. */
export function TopBar() {
  const { isAuthenticated, ready, signOut } = useAuth();
  const isAdmin = useIsAdmin();
  const router = useRouter();

  function handleSignOut() {
    signOut();
    router.push("/login");
  }

  return (
    <header className="border-b border-border bg-card">
      <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
        {/* The wordmark always goes to the marketing home page — "/" renders it
            for signed-in visitors too (with "Open studio" CTAs). */}
        <Link href="/" className="flex items-center gap-2 font-semibold">
          <Bird className="h-5 w-5 text-primary" aria-hidden />
          <span>Bird&apos;s Eye Review Studio</span>
        </Link>

        <nav className="flex items-center gap-3 text-sm">
          {ready && isAuthenticated ? (
            <>
              <Link
                href="/dashboard"
                className="text-muted-foreground hover:text-foreground"
              >
                My reviews
              </Link>
              {isAdmin && (
                <Link
                  href="/admin/publish"
                  className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
                  title="Publish queue"
                >
                  <Shield className="h-4 w-4" aria-hidden />
                </Link>
              )}
              <Link
                href="/settings"
                className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
                title="Preferences"
              >
                <Settings className="h-4 w-4" aria-hidden />
              </Link>
              {/* TODO(phase-billing): live balance from /billing/balance. */}
              <span className="flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">
                <Wallet className="h-3.5 w-3.5" aria-hidden />
                &mdash;
              </span>
              <button
                onClick={handleSignOut}
                className="flex items-center gap-1 rounded-md px-2 py-1 text-muted-foreground hover:text-foreground"
              >
                <LogOut className="h-4 w-4" aria-hidden />
                Sign out
              </button>
            </>
          ) : (
            <>
              <Link
                href="/login"
                className="text-muted-foreground hover:text-foreground"
              >
                Log in
              </Link>
              <Link
                href="/signup"
                className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground hover:opacity-90"
              >
                Sign up
              </Link>
            </>
          )}
        </nav>
      </div>
      {/* App-wide: warns when AI steps would bill someone else's Claude account. */}
      <ClaudeSessionBanner />
    </header>
  );
}
