"use client";

import { useEffect, useState } from "react";

/**
 * True when the viewport matches the given media query (default: ≤640px,
 * Tailwind's `sm` breakpoint). Starts false on the server and first client
 * render so there is no hydration mismatch; flips in an effect after mount.
 */
export function useIsMobile(query = "(max-width: 640px)") {
  const [isMobile, setIsMobile] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const update = () => setIsMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [query]);
  return isMobile;
}
