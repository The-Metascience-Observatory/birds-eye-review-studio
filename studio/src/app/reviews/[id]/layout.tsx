"use client";

import { useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { loginUrlForHere, useAuth } from "@/lib/auth";
import { EvidenceDock } from "@/components/EvidenceDock";
import { TopBar } from "@/components/TopBar";

/**
 * Shared shell for every page under /reviews/[id]: the auth guard (previously
 * copy-pasted into each page) and the app header. Children render the page body
 * inside their own <main>.
 */
export default function ReviewLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();

  useEffect(() => {
    if (ready && !isAuthenticated) router.replace(loginUrlForHere());
  }, [ready, isAuthenticated, router]);

  return (
    <div>
      <TopBar />
      {children}
      {/* Docked beneath every stage page: the live evidence table, hidden until
          the search produces papers. */}
      <EvidenceDock />
    </div>
  );
}
