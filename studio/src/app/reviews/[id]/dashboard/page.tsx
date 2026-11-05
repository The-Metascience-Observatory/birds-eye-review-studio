"use client";

/** Full-width dashboard view.
 *
 *  The dashboard's primary home is now INLINE on the export stage page
 *  (/stages/view-dashboard), where it auto-builds on first visit. This route remains
 *  for bookmarks and as the full-width view (the inline one shares the row with
 *  the agent rail). All the fetching/state logic lives in DashboardView, shared
 *  with the export stage so the two cannot drift. */

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { DashboardView } from "@/components/DashboardView";

export default function DashboardPreviewPage() {
  const { id } = useParams<{ id: string }>();

  return (
    <main className="mx-auto max-w-7xl px-4 py-8">
      {/* Outside DashboardView so it survives the loading/error states too. */}
      <BackLink id={id} />
      <DashboardView
        reviewId={id}
        header={({ summary, manifest, state }) => (
          <header className="mb-6">
            <div className="mt-2 flex flex-wrap items-baseline justify-between gap-3">
              <h1 className="text-2xl font-bold">
                {manifest?.title ?? summary.slug}
              </h1>
              <p className="text-sm text-muted-foreground">
                Local preview · built{" "}
                {state.built_at
                  ? new Date(state.built_at).toLocaleString()
                  : "—"}{" "}
                · not published
              </p>
            </div>
          </header>
        )}
      />
    </main>
  );
}

function BackLink({ id }: { id: string }) {
  return (
    <Link
      href={`/reviews/${id}/stages/view-dashboard`}
      className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
    >
      <ArrowLeft size={14} />
      Back to the pipeline
    </Link>
  );
}
