"use client";

/** The publish controls on the export stage — preflight-driven.
 *
 *  Publishing is two-party: any owner can PROPOSE a route (creating a request an
 *  admin later approves); an admin can additionally approve their own request in
 *  one step. Either way the same preflight packet is shown first — route
 *  availability, the diff vs. the live site, the shrink warning, and the honesty
 *  numbers — so nobody publishes blind.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Globe, Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "@/lib/toast";

import { Button } from "@/components/ui/Button";
import {
  approvePublishRequest,
  getPublishPreflight,
  requestPublish,
} from "@/lib/api";
import { useIsAdmin } from "@/hooks/useMe";

/** Default route suggestion: the slug hyphenated, which is how the site names
 *  routes. The user can edit it before proposing. */
function defaultRoute(slug: string | undefined): string {
  return (slug ?? "").replace(/_/g, "-");
}

export function PublishPanel({
  reviewId,
  slug,
  built,
}: {
  reviewId: string;
  slug: string | undefined;
  built: boolean;
}) {
  const isAdmin = useIsAdmin();
  const qc = useQueryClient();
  const route = defaultRoute(slug);

  // Preflight is a manual, on-demand fetch (it reads the bundle + diffs the live
  // site) — not something to poll. Enabled once the user has entered a route.
  const preflight = useQuery({
    queryKey: ["publish-preflight", reviewId, route],
    queryFn: () => getPublishPreflight(reviewId, route),
    enabled: false, // fired by the "Check" button
    retry: false,
  });
  const pf = preflight.data;

  const propose = useMutation({
    mutationFn: () => requestPublish(reviewId, route),
    onSuccess: () => {
      toast.success("Publish request submitted for admin review.");
      qc.invalidateQueries({ queryKey: ["publish-requests"] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not submit"),
  });

  const publishNow = useMutation({
    // Admin one-step: propose, then approve with the token preflight returned.
    mutationFn: async () => {
      if (!pf) throw new Error("Run preflight first.");
      const req = await requestPublish(reviewId, route);
      return approvePublishRequest(req.id, pf.confirm_token, {
        route,
        allow_shrink: pf.shrink_detected,
      });
    },
    onSuccess: (r) => {
      toast.success(`Published to /${r.route}.`);
      qc.invalidateQueries({ queryKey: ["dashboard-state", reviewId] });
      qc.invalidateQueries({ queryKey: ["publish-requests"] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Publish failed"),
  });

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Globe size={15} />
        Publish to the public site
      </h3>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">
        Request publication at this topic’s route. An administrator reviews and
        approves before anything goes live.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="text-sm text-muted-foreground">
          Route <span className="text-muted-foreground/60">/birds-eye-reviews/</span>
        </label>
        <input
          value={route}
          readOnly
          aria-label="Canonical publication route"
          placeholder="my-topic"
          className="w-56 rounded border border-border bg-background px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        />
        <Button
          variant="secondary"
          type="button"
          onClick={() => preflight.refetch()}
          disabled={!built || !route.trim() || preflight.isFetching}
          title={built ? undefined : "Build the dashboard first"}
        >
          {preflight.isFetching ? <Loader2 size={14} className="animate-spin" /> : null}
          Check
        </Button>
      </div>

      {pf && <Preflight pf={pf} />}

      {pf && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            variant="secondary"
            type="button"
            onClick={() => propose.mutate()}
            disabled={!pf.route_available || propose.isPending}
          >
            {propose.isPending ? <Loader2 size={14} className="animate-spin" /> : null}
            Request publication
          </Button>
          {isAdmin && (
            <Button
              type="button"
              onClick={() => publishNow.mutate()}
              disabled={!pf.route_available || publishNow.isPending}
            >
              {publishNow.isPending ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Globe size={15} />
              )}
              Publish now (admin)
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function Preflight({
  pf,
}: {
  pf: import("@/lib/types").PublishPreflightOut;
}) {
  return (
    <div className="mt-3 space-y-2 rounded-md border border-border bg-muted/30 p-3 text-sm">
      {!pf.route_available && (
        <p className="flex items-center gap-1.5 text-red-600">
          <AlertTriangle size={14} />
          Route already taken{pf.route_taken_by ? ` by ${pf.route_taken_by}` : ""}.
        </p>
      )}
      {pf.stale && (
        <p className="flex items-center gap-1.5 text-amber-600">
          <AlertTriangle size={14} />
          {pf.stale_reason ?? "The built dashboard is out of date."}
        </p>
      )}
      {pf.feasibility_changed && (
        <p className="flex items-start gap-1.5 text-amber-600">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            The feasibility verdict changed since the live version (
            {pf.live_feasibility_overall?.replace(/_/g, " ")} →{" "}
            {pf.feasibility_overall?.replace(/_/g, " ")}). Publishing updates what
            the public dashboard claims — review before proceeding.
          </span>
        </p>
      )}
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-muted-foreground">
        <span>
          {pf.first_publish ? "First publish" : "Update"} ·{" "}
          <strong className="tabular-nums text-foreground">
            {pf.n_trials ?? "—"}
          </strong>{" "}
          trials
        </span>
        {pf.feasibility_overall && (
          <span>Feasibility: {pf.feasibility_overall.replace(/_/g, " ")}</span>
        )}
        {pf.panels_skipped.length > 0 && (
          <span>{pf.panels_skipped.length} panel(s) skipped</span>
        )}
      </div>
      {pf.diff && (
        <p className="text-muted-foreground">
          {pf.diff.added_ids.length} added, {pf.diff.removed_ids.length} removed
          {pf.shrink_detected && (
            <span className="ml-1 text-amber-600">
              — this shrinks the corpus; an admin must confirm.
            </span>
          )}
        </p>
      )}
      {pf.route_available && !pf.shrink_detected && (
        <p className="flex items-center gap-1.5 text-green-600">
          <Check size={14} />
          Ready to publish.
        </p>
      )}
    </div>
  );
}
