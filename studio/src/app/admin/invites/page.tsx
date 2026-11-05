"use client";

/** Access codes: who may hold an account on this instance.
 *
 *  This is the operator's door policy. A code is minted for ONE person, admits
 *  ONE account, and can be withdrawn individually — which is the whole reason it
 *  replaced a single shared signup code that had no revocation and no record of
 *  who used it.
 *
 *  Admin-only. The server enforces 403; this page also guards the route so a
 *  non-admin sees an explanation rather than a wall of failed requests.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, KeyRound, Loader2, Ban } from "lucide-react";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "@/lib/toast";

import { TopBar } from "@/components/TopBar";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { SkeletonCard } from "@/components/ui/Skeleton";
import { createInvite, getInvites, revokeInvite } from "@/lib/api";
import { loginUrlForHere, useAuth } from "@/lib/auth";
import { useIsAdmin, useMe } from "@/hooks/useMe";
import type { InviteOut } from "@/lib/types";

/** Tint by what the operator can still do with the row, not by prettiness:
 *  pending is actionable (someone is holding an unused code), redeemed is the
 *  steady state, revoked/expired are inert. */
const STATE_STYLE: Record<string, string> = {
  pending: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  redeemed: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  revoked: "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200",
  expired: "bg-muted text-muted-foreground",
};

function fmt(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function AdminInvitesPage() {
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();
  const me = useMe();
  const isAdmin = useIsAdmin();
  const qc = useQueryClient();

  const [label, setLabel] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  // The raw code from the most recent mint. Held in component state and NEVER
  // refetched, because the server cannot produce it again — only its hash is
  // stored. Losing this panel means revoking the code and minting another.
  const [freshCode, setFreshCode] = useState<{ code: string; label: string } | null>(
    null,
  );

  useEffect(() => {
    if (ready && !isAuthenticated) router.replace(loginUrlForHere());
  }, [ready, isAuthenticated, router]);

  const invites = useQuery({
    queryKey: ["invites"],
    queryFn: getInvites,
    enabled: isAdmin,
  });

  const mint = useMutation({
    mutationFn: () =>
      createInvite({
        label: label.trim(),
        note: null,
        expires_in_days: expiresDays ? Number(expiresDays) : null,
      }),
    onSuccess: (res) => {
      setFreshCode({ code: res.code, label: res.invite.label });
      setLabel("");
      setExpiresDays("");
      qc.invalidateQueries({ queryKey: ["invites"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => revokeInvite(id),
    onSuccess: () => {
      toast.success("Access withdrawn. Their session ends on the next request.");
      qc.invalidateQueries({ queryKey: ["invites"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Code copied.");
    } catch {
      toast.error("Couldn't copy — select the code and copy it by hand.");
    }
  }

  if (!ready || me.isLoading) {
    return (
      <div>
        <TopBar />
        <main className="mx-auto max-w-4xl px-4 py-10">
          <SkeletonCard />
        </main>
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div>
        <TopBar />
        <main className="mx-auto max-w-4xl px-4 py-10">
          <h1 className="text-2xl font-bold">Admin</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            This page is for administrators. If you think you should have access,
            ask an existing admin to grant it.
          </p>
        </main>
      </div>
    );
  }

  return (
    <div>
      <TopBar />
      <main className="mx-auto max-w-4xl px-4 py-10">
        <h1 className="text-2xl font-bold">Access codes</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Each code admits one person, once. Revoking a code also ends the
          session of the account that redeemed it.
        </p>

        {/* Shown once, right after minting. */}
        {freshCode && (
          <section className="mt-6 rounded-lg border border-amber-400 bg-amber-50 p-4 dark:bg-amber-950/30">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <KeyRound className="h-4 w-4" />
              Code for {freshCode.label}
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              This is the only time it can be shown — only its hash is stored.
              Copy it now; if you lose it, revoke this code and mint another.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <code className="flex-1 select-all break-all rounded border border-border bg-background px-3 py-2 font-mono text-sm">
                {freshCode.code}
              </code>
              <Button variant="secondary" onClick={() => copy(freshCode.code)}>
                <Copy className="h-4 w-4" />
                Copy
              </Button>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="mt-2"
              onClick={() => setFreshCode(null)}
            >
              I&apos;ve saved it
            </Button>
          </section>
        )}

        <section className="mt-6 rounded-lg border border-border p-4">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Issue a new code
          </h2>
          <form
            className="mt-3 flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (label.trim()) mint.mutate();
            }}
          >
            <label className="flex-1 min-w-[16rem] text-sm">
              <span className="text-muted-foreground">Who is it for?</span>
              <Input
                className="mt-1"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="Alice Chen — Stanford"
                required
              />
            </label>
            <label className="text-sm">
              <span className="text-muted-foreground">Expires in (days)</span>
              <Input
                className="mt-1 w-36"
                type="number"
                min={1}
                value={expiresDays}
                onChange={(e) => setExpiresDays(e.target.value)}
                placeholder="never"
              />
            </label>
            <Button type="submit" disabled={!label.trim() || mint.isPending}>
              {mint.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <KeyRound className="h-4 w-4" />
              )}
              Mint code
            </Button>
          </form>
        </section>

        <section className="mt-8">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Issued codes
          </h2>
          <div className="mt-2 overflow-x-auto rounded-lg border border-border">
            {invites.isLoading && <SkeletonCard />}
            {invites.data?.length === 0 && (
              <EmptyState>No access codes yet.</EmptyState>
            )}
            {!!invites.data?.length && (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-muted-foreground">
                    <th className="px-3 py-2 font-medium">For</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Account</th>
                    <th className="px-3 py-2 font-medium">Issued</th>
                    <th className="px-3 py-2 font-medium">Expires</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {invites.data.map((inv: InviteOut) => (
                    <tr key={inv.id} className="border-b border-border last:border-0">
                      <td className="px-3 py-2">{inv.label}</td>
                      <td className="px-3 py-2">
                        <span
                          className={`rounded px-2 py-0.5 text-xs font-medium ${
                            STATE_STYLE[inv.state] ?? "bg-muted"
                          }`}
                        >
                          {inv.state}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">
                        {inv.redeemed_by_email ?? "—"}
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">
                        {fmt(inv.created_at)}
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">
                        {inv.expires_at ? fmt(inv.expires_at) : "never"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {!inv.revoked_at && (
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={revoke.isPending}
                            onClick={() => revoke.mutate(inv.id)}
                          >
                            <Ban className="h-4 w-4" />
                            Revoke
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
