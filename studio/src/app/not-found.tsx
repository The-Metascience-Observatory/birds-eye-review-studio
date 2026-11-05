import Link from "next/link";

import { BirdGlyph } from "@/components/landing/LandingPage";
import styles from "@/components/landing/landing.module.css";

/** App-wide 404, in the landing page's design language: the bird circles over a
 *  paper that screened out. Rendered for any unmatched route and for
 *  `notFound()` calls. A server component on purpose — it needs no auth state,
 *  so it stays static and renders instantly; the two links cover both kinds of
 *  visitor (the dashboard link simply bounces through login when signed out). */
export default function NotFound() {
  return (
    <div className={`${styles.landing} flex min-h-[70vh] items-center`}>
      <main className="mx-auto max-w-xl px-6 py-16 text-center">
        {/* The bird hovering over an excluded paper — this page screened out. */}
        <svg
          viewBox="0 0 200 150"
          className="mx-auto mb-6 w-44"
          role="img"
          aria-label="A bird hovers over a paper marked excluded."
        >
          <g transform="translate(100,44)">
            <g className={styles.birdBob}>
              <BirdGlyph scale={0.8} />
            </g>
          </g>
          <g transform="translate(88,88) rotate(-8)">
            <rect className={styles.pCard} width="26" height="34" rx="3" style={{ stroke: "var(--exc)" }} />
            <rect className={styles.pLine} x="5" y="10" width="14" height="3" rx="1.5" />
            <rect className={styles.pLine} x="5" y="17" width="11" height="3" rx="1.5" />
            <rect className={styles.pLine} x="5" y="24" width="15" height="3" rx="1.5" />
            <circle cx="20" cy="7" r="3" fill="var(--exc)" />
          </g>
        </svg>

        <p className={`${styles.mono} mb-3 text-[11px] uppercase tracking-[0.14em] text-primary`}>
          HTTP 404 · not indexed
        </p>
        <h1 className={`${styles.serif} mb-3 text-3xl font-semibold sm:text-4xl`}>
          Nothing found at this address.
        </h1>
        <p className="mx-auto mb-8 max-w-[44ch] text-muted-foreground">
          We searched everywhere a page could be and came up empty. The link may
          be stale, or the review it pointed at may have moved.
        </p>
        <div className="flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/"
            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:opacity-90"
          >
            Back to home
          </Link>
          <Link
            href="/dashboard"
            className="rounded-md border border-border px-4 py-2 text-sm font-semibold hover:border-primary hover:text-primary"
          >
            Open the studio
          </Link>
        </div>
        <p className={`${styles.mono} mt-8 text-xs text-muted-foreground`}>
          excluded — reason: no such route
        </p>
      </main>
    </div>
  );
}
