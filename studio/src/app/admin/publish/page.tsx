"use client";

/** The admin publish queue: pending requests to approve/reject, and everything
 *  currently live. Admin-only — the server enforces 403; this page also guards
 *  the route so a non-admin sees a clear message instead of empty 403s.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Globe, Loader2, X } from "lucide-react";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { toast } from "@/lib/toast";

import { TopBar } from "@/components/TopBar";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { SkeletonCard } from "@/components/ui/Skeleton";
import {
  approvePublishRequest,
  getPublished,
  getPublishRequests,
  getPublishPreflight,
  rejectPublishRequest,
} from "@/lib/api";
import { loginUrlForHere, useAuth } from "@/lib/auth";
import { useIsAdmin, useMe } from "@/hooks/useMe";
import type { PublishRequestOut } from "@/lib/types";

export default function AdminPublishPage() {
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();
  const me = useMe();
  const isAdmin = useIsAdmin();

  useEffect(() => {
    if (ready && !isAuthenticated) router.replace(loginUrlForHere());
  }, [ready, isAuthenticated, router]);

  const requests = useQuery({
    queryKey: ["publish-requests", "pending"],
    queryFn: () => getPublishRequests("pending"),
    enabled: isAdmin,
  });
  const published = useQuery({
    queryKey: ["published"],
    queryFn: () => getPublished(),
    enabled: isAdmin,
  });

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
        <h1 className="text-2xl font-bold">Publish queue</h1>

        <section className="mt-6">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Pending requests
          </h2>
          <div className="mt-2 space-y-3">
            {requests.data?.length === 0 && (
              <EmptyState bordered>No pending requests.</EmptyState>
            )}
            {requests.data?.map((req) => (
              <RequestRow key={req.id} req={req} />
            ))}
          </div>
        </section>

        <section className="mt-8">
          <h2 className="text-sm font-semibold text-muted-foreground">
            Currently live
          </h2>
          <div className="mt-2 overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Route</th>
                  <th className="px-3 py-2 font-medium">Trials</th>
                  <th className="px-3 py-2 font-medium">Source</th>
                  <th className="px-3 py-2 font-medium">Published</th>
                </tr>
              </thead>
              <tbody>
                {published.data?.map((p) => (
                  <tr key={p.route} className="border-b border-border/50">
                    <td className="px-3 py-2 font-medium">/{p.route}</td>
                    <td className="px-3 py-2 tabular-nums text-muted-foreground">
                      {p.n_trials ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{p.source}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {p.published_at?.slice(0, 10) ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </div>
  );
}

function RequestRow({ req }: { req: PublishRequestOut }) {
  const qc = useQueryClient();

  // Preflight for THIS review + route, so the admin sees the diff before acting.
  const pf = useQuery({
    queryKey: ["publish-preflight", req.review_id, req.requested_route],
    queryFn: () => getPublishPreflight(req.review_id, req.requested_route),
    retry: false,
  });

  const done = () => {
    qc.invalidateQueries({ queryKey: ["publish-requests"] });
    qc.invalidateQueries({ queryKey: ["published"] });
  };

  const approve = useMutation({
    mutationFn: () => {
      if (!pf.data) throw new Error("Preflight not loaded");
      return approvePublishRequest(req.id, pf.data.confirm_token, {
        allow_shrink: pf.data.shrink_detected,
      });
    },
    onSuccess: (r) => {
      toast.success(`Published to /${r.route}.`);
      done();
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Approve failed"),
  });

  const reject = useMutation({
    mutationFn: () => rejectPublishRequest(req.id, "Rejected from the queue."),
    onSuccess: () => {
      toast.success("Request rejected.");
      done();
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Reject failed"),
  });

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <span className="font-medium">/{req.requested_route}</span>
          <span className="ml-2 text-xs text-muted-foreground">
            requested {req.created_at.slice(0, 10)}
          </span>
        </div>
        <div className="flex gap-2">
          <Button
            type="button"
            onClick={() => approve.mutate()}
            disabled={!pf.data || !pf.data.route_available || approve.isPending}
          >
            {approve.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Check size={14} />
            )}
            Approve
          </Button>
          <Button
            variant="secondary"
            type="button"
            onClick={() => reject.mutate()}
            disabled={reject.isPending}
          >
            <X size={14} />
            Reject
          </Button>
        </div>
      </div>

      {pf.isLoading && (
        <p className="mt-2 text-sm text-muted-foreground">Loading preflight…</p>
      )}
      {pf.data && (
        <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
          {!pf.data.route_available && (
            <span className="text-red-600">
              Route taken{pf.data.route_taken_by ? ` by ${pf.data.route_taken_by}` : ""}
            </span>
          )}
          <span className="flex items-center gap-1">
            <Globe size={13} />
            {pf.data.first_publish ? "First publish" : "Update"} ·{" "}
            {pf.data.n_trials ?? "—"} trials
          </span>
          {pf.data.feasibility_overall && (
            <span>{pf.data.feasibility_overall.replace(/_/g, " ")}</span>
          )}
          {pf.data.shrink_detected && (
            <span className="text-amber-600">shrinks the corpus</span>
          )}
          {pf.data.diff && (
            <span>
              +{pf.data.diff.added_ids.length} / −{pf.data.diff.removed_ids.length}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
