"use client";

import { LandingPage } from "@/components/landing/LandingPage";
import { useAuth } from "@/lib/auth";

/** The marketing landing page — shown to everyone, signed in or not, so the
 *  logo/home link always works. A signed-in visitor gets "Open studio" CTAs
 *  instead of Log in / Request access (decided after `ready` so the brief
 *  pre-hydration window doesn't flash the wrong buttons). */
export default function HomePage() {
  const { isAuthenticated, ready } = useAuth();

  return <LandingPage authenticated={ready && isAuthenticated} />;
}
