/** Small display formatters shared across the review UI. */

/** Strip inline JATS/HTML tags from a metadata string and collapse whitespace.
 *
 *  Titles arriving from Crossref/OpenAlex/etc. can embed markup like
 *  `Adenosine A<sub>1</sub> Receptors`. We render titles as plain text, so the
 *  tags would otherwise show literally. Newly fetched data is cleaned in the
 *  pipeline (`steps/fetch_abstracts.py` `_strip_jats`); this is the display-time guard
 *  for rows already on disk. */
export function stripTags(s: string | null | undefined): string {
  if (!s) return "";
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

/** Snake_case → Title Case for display of enum-ish values (design/RoB/verdict),
 *  with "Rct" special-cased to "RCT". */
export function titleCase(s: string): string {
  return (s || "")
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase())
    .replace(/\bRct\b/gi, "RCT");
}

/** Recase SHOUTY-CAPS name tokens some publishers deposit in Crossref
 *  ("Ji Young KIM" → "Ji Young Kim"). Newly fetched metadata is cleaned in the
 *  pipeline (`metadata_fetcher._fix_author_caps`); this is the display-time
 *  guard for rows already on disk. Only all-caps runs of 3+ letters are
 *  recased, so initials ("JK", "A.H.") and Roman-numeral suffixes (III, IV)
 *  keep their capitals, and mixed-case names pass through untouched. */
export function fixAuthorCaps(name: string): string {
  return name.replace(/\p{Lu}{3,}/gu, (run) =>
    /^[IVX]+$/.test(run) ? run : run[0] + run.slice(1).toLowerCase(),
  );
}

/** `raw` as an absolute http(s) URL, or undefined for anything else.
 *
 *  Use for EVERY URL that came from outside the app (enrichment metadata,
 *  publisher links, LLM output) before it reaches an `href`. React still
 *  renders `javascript:` URLs (it only warns), and the CSP allows inline
 *  script, so a crafted metadata `url` would run in the studio's origin — with
 *  the session token in localStorage. Parsing (rather than a prefix regex)
 *  also rejects whitespace/control-character smuggling like "java\tscript:". */
export function safeHttpUrl(raw: unknown): string | undefined {
  if (typeof raw !== "string" || !raw.trim()) return undefined;
  let u: URL;
  try {
    u = new URL(raw.trim());
  } catch {
    return undefined;
  }
  return u.protocol === "http:" || u.protocol === "https:" ? u.href : undefined;
}

/** Build a doi.org URL from a DOI stored in the on-disk filename encoding.
 *
 *  DOIs are persisted with their `/` written as `--` so they can be filenames
 *  (`10.1684--epd.2012.0534`). A link must reverse that first, or it resolves to
 *  a non-existent DOI. Mirrors the pipeline's `doi.replace("--", "/")`. */
export function doiUrl(doi: string | null | undefined): string | null {
  if (!doi) return null;
  // PMID-only papers are keyed as `pmid_<n>` (the pipeline's stem for records
  // with no DOI). Those must resolve to PubMed, not a bogus doi.org/pmid_… link.
  const pmid = /^pmid[_:]?(\d+)$/i.exec(doi.trim());
  if (pmid) return `https://pubmed.ncbi.nlm.nih.gov/${pmid[1]}/`;
  const canonical = doi.replace(/--/g, "/");
  return `https://doi.org/${encodeURI(canonical)}`;
}

/** Build a Metascience Observatory Explorer URL for a paper's DOI, e.g.
 *  `https://explore.metascienceobservatory.org/doi/10.1001/jamanetworkopen.2024.51217`.
 *  Same `--`→`/` canonicalization as {@link doiUrl}. */
export function explorerUrl(doi: string | null | undefined): string | null {
  if (!doi) return null;
  const canonical = doi.replace(/--/g, "/");
  return `https://explore.metascienceobservatory.org/doi/${encodeURI(canonical)}`;
}

/** Seconds -> "45s" / "1m 07s" / "1h 02m". */
export function fmtDur(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}m ${String(rem).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}
