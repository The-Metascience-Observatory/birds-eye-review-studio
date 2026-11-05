"use client";

/** The paper-detail drawer's author line: full names (expanded on demand when
 *  the stored ones are initials-only), truncated behind a "+N more" toggle. */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getPaperAuthors } from "@/lib/api";
import { fixAuthorCaps } from "@/lib/format";

/** Long author lists truncate at this many names behind a "+N more" toggle. */
const MAX_AUTHORS = 8;

/** A bare-initial token like "J", "J.", "JK", or "AH" (mirrors the server's
 *  `_is_initial_token`), used to decide whether a stored author name needs
 *  expanding. Real short surnames ("Li", "He") have a lowercase letter, so they
 *  don't match. */
function isInitialToken(token: string): boolean {
  const stripped = token.replace(/\./g, "");
  return stripped.length > 0 && stripped.length <= 3 && /^[A-Z]+$/.test(stripped);
}

/** True if any author in a "; "-joined string has an initials-only name, in
 *  either "J. Smith" or "Smith J." order — the trigger for a full-name lookup. */
function authorsLookAbbreviated(authors?: string): boolean {
  if (!authors || !authors.trim()) return true;
  return authors
    .split(";")
    .map((n) => n.trim())
    .filter(Boolean)
    .some((name) =>
      name
        .replace(/,/g, " ")
        .split(/\s+/)
        .filter(Boolean)
        .some(isInitialToken),
    );
}

/** Split a "; "-joined author string into display-ready names (caps fixed). */
function splitAuthors(authors: string): string[] {
  return authors
    .split(";")
    .map((n) => fixAuthorCaps(n.trim()))
    .filter(Boolean);
}

export function AuthorsBlock({
  reviewId,
  doi,
  storedAuthors,
}: {
  reviewId: string;
  doi: string;
  /** The row's best stored value — full for screened papers. */
  storedAuthors?: string;
}) {
  const [showAll, setShowAll] = useState(false);

  // Only fetch an expansion when the stored names look initials-only, so most
  // opens make no extra call. While loading, show what the row has.
  const needsExpansion = !!doi && authorsLookAbbreviated(storedAuthors);
  const authorsQ = useQuery({
    queryKey: ["paper-authors", reviewId, doi],
    queryFn: () => getPaperAuthors(reviewId, doi),
    enabled: needsExpansion,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const authors = authorsQ.data?.authors ?? storedAuthors ?? null;
  const names = authors ? splitAuthors(authors) : [];
  if (names.length === 0) return null;

  const shown = showAll ? names : names.slice(0, MAX_AUTHORS);
  const hiddenCount = names.length - shown.length;

  return (
    <p className="mt-1.5 text-xs leading-relaxed text-foreground/70">
      {shown.join(", ")}
      {hiddenCount > 0 && (
        <>
          {", "}
          <button
            type="button"
            onClick={() => setShowAll(true)}
            className="font-medium text-blue-600 hover:underline"
          >
            +{hiddenCount} more
          </button>
        </>
      )}
      {showAll && names.length > MAX_AUTHORS && (
        <>
          {" "}
          <button
            type="button"
            onClick={() => setShowAll(false)}
            className="font-medium text-blue-600 hover:underline"
          >
            Show fewer
          </button>
        </>
      )}
    </p>
  );
}
