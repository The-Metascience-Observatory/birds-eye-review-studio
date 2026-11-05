/** Pipeline constants the UI has to know, mirrored by hand.
 *
 *  The studio cannot import the Python pipeline, so anything it must agree with
 *  gets copied here — the same arrangement as `workspace.ARTICLE_TYPES`, which
 *  duplicates `constants.ARTICLE_TYPES` and is kept honest by a test. Each
 *  constant below names the guard that fails when the copy drifts.
 */

/** Article types whose full text is downloaded when `pdf_types` is unset.
 *
 *  Mirrors `constants.PDF_ELIGIBLE_TYPES` (= the primary-study types plus
 *  the two legacy labels). download_pdfs.py falls back to this list, so the
 *  Screen stage shows it as the effective default when the review has made no
 *  explicit choice.
 *
 *  Guard: `test_classification_config.py::test_studio_pdf_types_default_mirrors_the_pipeline`.
 */
export const DEFAULT_PDF_TYPES = [
  "Original Research",
  "Clinical Trial",
  "Prospective Cohort Study",
  "Retrospective Cohort Study",
  "Other Observational Study",
  "Case-Control Study",
  "Cross-Sectional Study",
  "Survey Study",
  "Qualitative Study",
  "Mixed Methods Study",
  "Observational/Cohort Study",
  "Case Report/Series",
] as const;
