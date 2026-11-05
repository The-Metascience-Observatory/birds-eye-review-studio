"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { listReviews } from "@/lib/api";
import { loginUrlForHere, useAuth } from "@/lib/auth";
import { TopBar } from "@/components/TopBar";
import type { ReviewOut } from "@/lib/types";

export default function DashboardPage() {
  const { isAuthenticated, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (ready && !isAuthenticated) router.replace(loginUrlForHere());
  }, [ready, isAuthenticated, router]);

  const { data, isLoading, isError, error } = useQuery<ReviewOut[]>({
    queryKey: ["reviews"],
    queryFn: listReviews,
    enabled: ready && isAuthenticated,
  });

  return (
    <div>
      <TopBar />
      <main className="mx-auto max-w-5xl px-4 py-10">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">My reviews</h1>
          <Link
            href="/reviews/new"
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
          >
            <Plus className="h-4 w-4" aria-hidden />
            New review
          </Link>
        </div>

        <div className="mt-6">
          {isLoading && (
            <p className="text-sm text-muted-foreground">Loading…</p>
          )}
          {isError && (
            <p className="text-sm text-red-600">
              {(error as Error)?.message ?? "Failed to load reviews"}
            </p>
          )}
          {data && data.length === 0 && (
            <div className="rounded-lg border border-border bg-card p-8 text-center">
              <p className="text-muted-foreground">
                No reviews yet. Create your first one.
              </p>
            </div>
          )}
          {data && data.length > 0 && (
            <ul className="divide-y divide-border rounded-lg border border-border bg-card">
              {data.map((r) => (
                <li key={r.id}>
                  <Link
                    href={`/reviews/${r.id}`}
                    className="flex items-center justify-between px-4 py-3 hover:bg-muted"
                  >
                    <div>
                      <div className="font-medium">{r.title}</div>
                      <div className="text-sm text-muted-foreground">
                        {r.topic_name}
                      </div>
                    </div>
                    <span className="rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">
                      {r.status}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  );
}
