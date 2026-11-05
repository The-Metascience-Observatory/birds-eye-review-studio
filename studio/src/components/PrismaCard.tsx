"use client";

/** The PRISMA funnel, fetched live and rendered through the shared PrismaFlow.
 *
 *  A thin studio wrapper: owns the query (and polls while a job runs, since the
 *  funnel deepens as screening progresses); the diagram itself is the package
 *  component the published dashboards use, so the wizard's "funnel so far" and
 *  the final dashboard's PRISMA can't disagree.
 */

import { useQuery } from "@tanstack/react-query";
import { PrismaFlow } from "@bird/dashboard-ui";

import { Banner } from "@/components/ui/Banner";
import { SkeletonCard } from "@/components/ui/Skeleton";
import { getPrisma } from "@/lib/api";

export function PrismaCard({
  reviewId,
  running = false,
  title = "Screening funnel",
}: {
  reviewId: string;
  /** Poll while a job is touching this review, so the funnel deepens live. */
  running?: boolean;
  title?: string;
}) {
  const prisma = useQuery({
    queryKey: ["prisma", reviewId],
    queryFn: () => getPrisma(reviewId),
    refetchInterval: running ? 3000 : false,
    retry: false,
  });

  if (prisma.isLoading) {
    return <SkeletonCard />;
  }
  if (prisma.isError || !prisma.data) {
    return <Banner tone="info">The screening funnel isn&apos;t available yet.</Banner>;
  }

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      <PrismaFlow data={prisma.data} />
    </div>
  );
}
