/* ── Evidence table (live, progressive) + per-paper detail ────────────────── */

import { ApiError, authFetch, doiQuery, request } from "./client";

export interface EvidenceRow {
  doi: string;
  pmid?: string;
  title?: string;
  authors?: string;
  journal?: string;
  year?: string | number | null;
  article_type?: string;
  trial_type?: string;
  design_type?: string;
  n?: number | null;
  rob?: string;
  verdict?: string;
  primary_name?: string | null;
  follow_up_weeks?: number | null;
  relevance?: string;
  eligible?: boolean;
  // Which source the background enrichment used for this row's biblio, or
  // "not_found" if it tried every source and none resolved. Absent ⇒ enrichment
  // hasn't attempted this DOI yet — the signal the dock uses to decide whether
  // more enrichment work actually remains (vs. re-firing on permanent misses).
  metadata_source?: string;
  // Publisher/OA link resolved by enrichment. Declared (not left to the index
  // signature) so the missing-PDFs page's manual-download links are visible to
  // the type checker and the contract-drift test.
  url?: string | null;
  // The authoritative screening verdict the server computes (evidence._screen_state):
  //  - "included"     going to extraction (full-text screen passed it, extracted,
  //                   or manually force-included)
  //  - "excluded"     dropped by the pipeline or a manual exclude
  //  - "pending"      screened IN on the abstract, awaiting the background
  //                   full-text eligibility screen's verdict
  //  - "screened_out" didn't pass relevance / full-text screen, not force-included
  // The pill, row tint, and default sort all key on this.
  screen_state?: "included" | "excluded" | "pending" | "screened_out";
  // Back-compat bool == (screen_state === "included").
  included?: boolean;
  // True when the user force-included this paper (user_inclusions.json) — it goes
  // to extraction even if the full-text screen said ineligible.
  user_included?: boolean;
  // The study organism: an abstract-level guess, refined by the full-text screen.
  // Non-human papers are screened out by default (unless the review includes
  // animal studies). Absent until screening has run for this paper.
  organism?: "human" | "mouse" | "other_animal" | "other";
  extracted?: boolean;
  has_fulltext?: boolean;
  /** How the PDF was converted to text (`merged-grobid-docling`, `grobid-only`,
   *  …). `conversion_degraded` marks incomplete or table-free conversions;
   *  `tables_captured` is
   *  measured from the text itself. Absent for pre-provenance conversions. */
  conversion_mode?: string;
  conversion_degraded?: boolean;
  conversion_status?: string;
  conversion_requires_review?: boolean;
  conversion_warnings?: string[];
  tables_captured?: boolean;
  /** Publisher full text fetched alongside the PDF. `has_xml` reports
   *  availability; completeness and the selected extraction source can differ.
   *  `structured_source` is "xml" | "html". */
  has_xml?: boolean;
  structured_source?: string;
  /** Supplementary/supporting-information files are available for this paper. */
  has_supplement?: boolean;
  is_excluded?: boolean;
  exclusion_reason?: string | null;
  pico?: {
    population: string | null;
    intervention: string[];
    comparator: string[];
    outcome: string[];
  };
  [k: string]: unknown;
}

/** One arm's row in `sample_sizes.per_arm` / `adverse_events.per_arm` /
 *  `baseline_demographics.per_arm` — typed loosely (the pass-2 schema is
 *  config-shaped), naming only the keys the detail panel renders. */
export interface ExtractionArmDetail {
  arm_id?: string | null;
  n_randomized?: number | null;
  n_randomized_quote?: string | null;
  n_enrolled?: number | null;
  n_enrolled_quote?: string | null;
  n_completed?: number | null;
  n_analyzed_primary?: number | null;
  analysis_population?: string | null;
  n_lost_to_followup?: number | null;
  n_withdrawn?: number | null;
  withdrawal_reasons?: string[] | null;
  // adverse_events.per_arm
  n_any_ae?: number | null;
  n_serious_ae?: number | null;
  n_withdrawn_due_to_ae?: number | null;
  n_deaths?: number | null;
  notable_aes?: string[] | null;
  // baseline_demographics.per_arm / .overall
  age_mean?: number | null;
  age_sd?: number | null;
  percent_female?: number | null;
  bmi_mean?: number | null;
  comorbidities?: string[] | null;
  [k: string]: unknown;
}

export interface ExtractionOutcome {
  outcome_id?: string | null;
  name?: string | null;
  symptom_domain?: string | null;
  measurement_instrument?: string | null;
  is_primary?: boolean | null;
  higher_is_better?: boolean | null;
  timepoints?: {
    timepoint_id?: string | null;
    label?: string | null;
    weeks_from_randomization?: number | null;
  }[];
  arm_results?: {
    arm_id?: string | null;
    timepoint_id?: string | null;
    n?: number | null;
    mean?: number | null;
    sd?: number | null;
    events?: number | null;
    total?: number | null;
    result_quote?: string | null;
    [k: string]: unknown;
  }[];
  between_group_effects?: {
    comparison?: string | null;
    timepoint_id?: string | null;
    effect_measure?: string | null;
    effect_value?: number | null;
    ci_95_low?: number | null;
    ci_95_high?: number | null;
    p_value?: number | null;
    adjusted?: boolean | null;
    effect_quote?: string | null;
    [k: string]: unknown;
  }[];
  [k: string]: unknown;
}

/** A full pass-2 extraction record, fetched on demand for the detail panel.
 *  Loosely typed: only the parts the panel renders are named, and every one is
 *  optional — lighter extraction tiers simply omit blocks. */
export interface PaperExtraction {
  paper_id?: string;
  study_design?: {
    design_type?: string | null;
    blinding?: string | null;
    setting?: string | null;
    countries?: string[] | null;
    arms?: {
      arm_id?: string | null;
      label?: string | null;
      type?: string | null;
      intervention_name?: string | null;
      dose?: string | null;
      route?: string | null;
      frequency?: string | null;
      duration_value?: number | null;
      duration_unit?: string | null;
      [k: string]: unknown;
    }[];
    [k: string]: unknown;
  };
  sample_sizes?: {
    n_screened?: number | null;
    n_eligible?: number | null;
    n_randomized_total?: number | null;
    n_enrolled_total?: number | null;
    per_arm?: ExtractionArmDetail[];
    [k: string]: unknown;
  };
  baseline_demographics?: {
    per_arm?: ExtractionArmDetail[];
    overall?: ExtractionArmDetail | null;
    [k: string]: unknown;
  };
  outcomes?: ExtractionOutcome[];
  adverse_events?: {
    reporting_quality?: string | null;
    per_arm?: ExtractionArmDetail[];
    [k: string]: unknown;
  };
  follow_up?: {
    treatment_duration_weeks?: number | null;
    total_duration_weeks?: number | null;
    post_treatment_followup_weeks?: number | null;
    [k: string]: unknown;
  };
  risk_of_bias?: {
    tool?: string | null;
    overall_judgment?: string | null;
    domain_judgments?: string | null;
    key_concern?: string | null;
    [k: string]: unknown;
  };
  subgroup_analyses?: Record<string, unknown>[];
  /** Answers to the review's `custom_extraction_questions`, in question order.
   *  Only present on papers extracted while questions were configured. */
  custom_answers?: {
    question?: string | null;
    answer?: string | null;
    [k: string]: unknown;
  }[] | null;
  /** The model's own account of the extraction — notably what it could NOT see
   *  ("the numeric tables were not included in the provided text"). This is
   *  what diagnosed the table-loss bug, so it belongs in front of the user. */
  extraction_metadata?: {
    extractor_notes?: string | null;
    [k: string]: unknown;
  };
  _provenance?: {
    n_quoted?: number;
    n_exact?: number;
    n_fuzzy?: number;
    n_absent?: number;
    traceable_rate?: number | null;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export interface EvidenceOut {
  rows: EvidenceRow[];
  stage_reached: string;
  counts: Record<string, number>;
}

export function getEvidence(reviewId: string): Promise<EvidenceOut> {
  return request<EvidenceOut>(`/reviews/${reviewId}/evidence`);
}

/** One paper's abstract, fetched on demand for the detail panel (kept out of
 *  the evidence rows because it's large). Null when there's no abstract. */
export function getPaperAbstract(
  reviewId: string,
  doi: string,
): Promise<{ abstract: string | null }> {
  return request(`/reviews/${reviewId}/evidence/abstract${doiQuery(doi)}`);
}

/** The full extraction record(s) for one paper (usually one; multi-intervention
 *  papers can carry several). 404 → empty list, matching "not extracted yet". */
export async function getPaperExtraction(
  reviewId: string,
  doi: string,
): Promise<{ records: PaperExtraction[] }> {
  try {
    return await request(`/reviews/${reviewId}/evidence/extraction${doiQuery(doi)}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { records: [] };
    throw e;
  }
}

/** Fetch the downloaded PDF for a paper and return a blob object URL, or null
 *  when none exists (404). The PDF endpoint is auth-gated with a Bearer header,
 *  which a plain `<a href>` navigation can't send — so we fetch with the token
 *  and hand back an object URL instead. Ownership is the caller's: the in-page
 *  PDF pane revokes it on close/replace; the new-tab fallback deliberately
 *  leaves it for the new tab to own. `signal` lets a caller that has moved on
 *  to another paper abandon the download. */
export async function getPaperPdfObjectUrl(
  reviewId: string,
  doi: string,
  signal?: AbortSignal,
): Promise<string | null> {
  const res = await authFetch(
    `/reviews/${reviewId}/evidence/pdf${doiQuery(doi)}`,
    { signal, errorPrefix: "Could not load the PDF" },
    [404],
  );
  if (res.status === 404) return null;
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

/** One paper's full author names, fetched on demand for the detail panel. Returns
 *  the stored names as-is when they're already full; when they're initials-only
 *  the server expands them from the DOI (and caches the result). */
export function getPaperAuthors(
  reviewId: string,
  doi: string,
): Promise<{ authors: string | null; expanded: boolean }> {
  return request(`/reviews/${reviewId}/evidence/authors${doiQuery(doi)}`);
}

/** Upload a hand-fetched PDF for one paper. Multipart, so the browser sets the
 *  Content-Type (with its boundary) — `request` would force JSON. The server
 *  stores it under the canonical filename and enqueues the convert job. */
export async function uploadPaperPdf(
  reviewId: string,
  doi: string,
  file: File,
): Promise<{ stored: string; convert_job_id: string }> {
  const form = new FormData();
  form.append("doi", doi);
  form.append("file", file);
  const res = await authFetch(`/reviews/${reviewId}/evidence/upload-pdf`, {
    method: "POST",
    body: form,
    errorPrefix: "Upload failed",
  });
  return res.json();
}

/** Manually exclude papers from downstream processing. Already-extracted rows
 *  stay; the exclusion greys the row now, and the server re-runs staging in the
 *  background (`restage_job_id`) so the verdict reaches extraction. */
export function excludePapers(
  reviewId: string,
  dois: string[],
  reason = "user_excluded",
): Promise<{ excluded: number; restage_job_id?: string | null }> {
  return request(`/reviews/${reviewId}/evidence/exclude`, {
    method: "POST",
    body: { dois, reason },
  });
}

/** Force-include papers, overriding the automatic gate. The server re-runs
 *  staging in the background so the paper actually reaches extraction. */
export function includePapers(
  reviewId: string,
  dois: string[],
): Promise<{ included: number; restage_job_id?: string | null }> {
  return request(`/reviews/${reviewId}/evidence/include`, {
    method: "POST",
    body: { dois, reason: "" },
  });
}
