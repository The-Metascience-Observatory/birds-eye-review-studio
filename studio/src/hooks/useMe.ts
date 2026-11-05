"use client";

/** The signed-in user, fetched from `/auth/me` and cached. Separate from
 *  `useAuth` (which owns only the token) because role is server truth: the
 *  client must never decide "is admin" from anything it can edit locally. */

import { useQuery } from "@tanstack/react-query";

import { me } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { UserOut } from "@/lib/types";

export function useMe() {
  const { isAuthenticated, ready } = useAuth();
  return useQuery<UserOut>({
    queryKey: ["me"],
    queryFn: me,
    enabled: ready && isAuthenticated,
    staleTime: 5 * 60 * 1000, // role rarely changes within a session
  });
}

/** True only once `/auth/me` has confirmed an admin role. Undefined-safe:
 *  returns false while loading, so an admin-only control never flashes for a
 *  regular user. */
export function useIsAdmin(): boolean {
  const { data } = useMe();
  return data?.role === "admin";
}
