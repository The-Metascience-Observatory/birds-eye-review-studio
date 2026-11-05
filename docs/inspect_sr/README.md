# INSPECT-SR systematic review tooling

`steps/systematic_review.py` creates a source-linked systematic-review draft
through the Codex CLI (run `--help` for the default model and the `--model`
choices). The selected model is recorded in the manifest and in the assessment
cache fingerprints. It reads the current clinical-trial screen, full-text
decisions, paper store and extractions. It does not re-screen or overwrite those
inputs. Optionally pass the published website bundle so every displayed report
is reconciled, including reports absent from today's screen-positive set.

```bash
python3 scripts/download_inspect_sr.py      # fetches the guidance into docs/inspect_sr/upstream/
python3 steps/systematic_review.py --config configs/config_my_topic.yaml \
  --dashboard <path to a published dashboard bundle>   # optional
```

For other topics, supply their configuration. `systematic_review: {enabled: true}`
adds this as canonical step **19** after the dashboard. It belongs to the Studio
`report` stage. The existing lightweight synthesis button remains available;
run the canonical pipeline for the full INSPECT-SR workflow. Studio runs must
select the `codex_cli` provider. Other providers fail explicitly instead of
silently using the host's Codex login.

Install the package's dependencies (including `jsonschema`). PDF visual review
also requires PyMuPDF (`pip install PyMuPDF`). Codex must be installed and logged
in. Model calls are serial by default. `--workers 6 --batch-size 1` enables bounded
parallel report assessment and narrative generation, as with the pipeline's
existing extraction workers. The two assessments of a report remain separate.

## Reproducible inputs and sources

`--prepare-only` builds the corpus and retrieves sources without using the model.
`--offline` uses cached sources and records misses as unavailable.
`--refresh-sources` refreshes notices, registrations and metadata; changed evidence
invalidates the affected model assessments. `--retraction-watch PATH` uses a local
CSV. Otherwise the current public Crossref/Retraction Watch CSV is downloaded
when absent. No API key is needed for that dataset. PubPeer lookups need a PubPeer
developer key in `PUBPEER_DEVKEY`; without one they are recorded as unavailable. Linked public discussion comments are downloaded and
included as attributed commentary for assessment. Unavailable discussions remain
explicitly unavailable; comment presence alone cannot establish a concern.

`scripts/download_inspect_sr.py` fetches the guidance introduction and all 21
checks from a pinned upstream commit into `upstream/` (not committed);
`upstream/manifest.json` records the commit and file hashes. The guidance is
CC BY 4.0 (© Jack Wilkinson); see `upstream/license.qmd` and `THIRD_PARTY.md`.
Cite Wilkinson et al., DOI 10.1101/2025.09.03.25334905, and guidance version 1.1.2.

## Execution and review

The default is two independent **machine** assessments of RCT reports; these do
not substitute for independent human reviewers. Non-randomized reports receive
descriptive extraction and bias limitations separately, since INSPECT-SR was
developed for RCTs. All 21 check responses, four domain judgments and an overall
judgment are required for each RCT. There is no check-count score.

A separate source-grounded scope audit distinguishes treatment of an established
condition from acute-infection prevention, different conditions, protocols and
companion reports. A conflict with the initial assessment holds that report out
of effectiveness synthesis pending adjudication, while retaining it in the audit.
This extra machine check is not human adjudication.

PDF pages are attached to the model when available (up to `--visual-pages 12`).
Long PDFs are sampled for figure/results pages and marked partial; incomplete
visual coverage cannot clear the figure check. This is visual screening, not
specialist image forensics. Full-text truncation, missing registry history,
unconfirmed author identities and unavailable sources are explicit limitations.

Every Yes/No response and clinical finding needs an exact source passage. Terra
selects a labeled passage; the program inserts its original text, verifies the
source, and checks that numbers in findings occur in the associated passage.
Unknown labels, unsupported numbers and other invalid output are retried.
Failures stop the run with resumable progress; they never become successful
negative assessments. Independent differences are retained for adjudication.
After retries, a clinical finding with unverifiable quotations or numbers may be
withheld while the rest of its assessment is retained. Every remaining field must
still pass validation. Omitted claims and reasons are recorded separately in
`withheld_findings.csv` and the adjudication queue; they are not synthesis evidence.
Invalid eligibility or INSPECT-SR checks still fail the report. Account quota
exhaustion stops the batch with cached progress so it can resume after reset.
Serious concerns are separated pending resolution, and no/some-concern analysis
sets support a sensitivity review. No stale dashboard pooling is reused.

Retraction screening covers every design, including observational reports.
Exact DOI matches are withheld pending notice verification. A publisher-deposited
retraction-notice title linked to the original DOI confirms publication status;
these reports remain in the audit and are excluded from effectiveness synthesis.
Retraction Watch-supplied Crossref links alone are not counted as independent
publisher confirmation. Reasons for retraction still require substantive review.

Add `--resume-snapshot` to continue the exact frozen corpus while background
pipeline work changes live inputs. Without it, re-running snapshots current data
and only reuses assessments whose evidence is unchanged. Cache keys cover source packets, guidance,
model, reviewer, schema, prompt rules and configuration. `--limit 3 --reviewers 1`
creates a separate smoke-test directory and cannot replace the full manuscript.

## Outputs

Outputs live in `projects/<slug>/systematic_review/`:

- `systematic_review.md`: intervention-specific manuscript and methods.
- `related_evidence.md`: prevention, other-condition and companion reports presented separately.
- `evidence_appendix.md`: all reports, findings, check comments and quotations.
- `study_table.csv`, `inspect_sr_checks.csv`: reusable structured assessment tables.
- `selection_audit.json`, `manifest.json`: provenance, selection and dashboard parity.
- `input_snapshots/`, `corpus/`, `source_cache/`, `images/`: frozen inputs and evidence.
- `prompts/`, `raw_responses/`, `assessments/`, `assessment_history/`: exact calls and validated results.
- `decisions.json`, `adjudication_queue.csv`, `possible_trial_overlap.json`:
  disagreements, missing evidence and candidate duplicate-trial groups.
- `scope_audit.json`, `scope_audits/`: treatment-scope checks and their source passages.
- `withheld_findings.csv`: unaccepted clinical claims and source-validation errors.
- `sensitivity_sets.json`, `sensitivity_by_intervention.csv`: primary and restricted analysis sets.
- `correspondence_drafts.md`: unsent clarification requests.
- `run_status.json`: prepared, assessing, failed, or automated draft complete.
- `inspect_sr.json`: portable dashboard decisions and analysis-set memberships.

A dashboard that consumes `inspect_sr.json` can offer an **INSPECT-SR review
filter** (the public long-COVID dashboard does). It defaults to all reports and can select the provisional treatment synthesis, the RCT
primary or no-concerns sensitivity set, reports held pending resolution, or
excluded/out-of-scope reports. Charts and tables use the same selection. Reasons
remain accessible on each report, and the selection is saved in shareable URLs.
The treatment-synthesis set is broader than the RCT primary set; it may include
nonrandomized reports and unresolved differences. Unknown assessments never
pass an analysis-set filter. Screening, treatment scope, trustworthiness and
retraction status remain separate, with provisional concerns clearly labelled.

To update a dashboard's snapshot from a completed run without calling the model
again:

```bash
python3 scripts/export_inspect_dashboard.py \
  --review-dir projects/<slug>/systematic_review \
  --output <dashboard checkout>/data/birds_eye_reviews/<slug>/inspect_sr.json
```

This exports decision metadata, not source texts or credentials. It preserves
the original assessment date and records a hash of the decisions. Exporting
does not deploy the website. Existing dashboard outcome values are not
revalidated or re-extracted by selecting a filter.

After success, `data/review_document.md` is updated for the existing Studio viewer.
No document is published and no correspondence is sent automatically.

For human decisions, pass `--adjudications PATH`, a JSON object keyed by paper ID.
Each value needs `reviewer`, `reason`, `reviewed_at`, `input_fingerprint` (from the
first machine assessment), and a complete `assessment` following the schema in
`ber_core.inspect_sr`. Stale decisions are rejected. The raw machine assessments
are always retained, including earlier source fingerprints in `assessment_history/`.

Use `--supplemental-evidence PATH` to supply evidence obtained during human review.
The JSON is keyed first by paper ID and then by a stable source ID. Each source
needs `kind`, `text`, `reviewer`, and `reviewed_at`. For example:

```json
{
  "10.1000/example": {
    "registry_original": {
      "kind": "registry_history",
      "text": "Original record and amendment comparison, with dates, URLs and exact quotations...",
      "reviewer": "Reviewer's name",
      "reviewed_at": "2026-09-19"
    }
  }
}
```

Other useful kinds include `figure_review`, `publisher_notice`, `author_identity`,
and `statistical_verification`. Attach findings and their supporting evidence;
an assertion that a check was completed is insufficient. New evidence changes the
assessment fingerprint, triggers reassessment, and invalidates stale adjudications.
`ber_core.inspect_stats` provides conservative integer-moment and rounding-aware
pooled-t-test checks for appropriately verified input data; these diagnostics
never determine a trustworthiness judgment by themselves.

An automated draft may still require original registry histories, publisher and
citation searches, independent full-text/data verification, expert statistical
and image assessment, author correspondence, outcome-level RoB 2/ROBINS-I and
GRADE, study-level deduplication, and an updated comprehensive literature search.
These are not represented as completed simply because the script exits zero.

An adjudication that resolves a retraction flag also needs `notice_resolution`:
an object containing `status` (`retracted` or `not_retracted`), `reason`, and
`evidence` (the same source/quote format as the assessment). This permits documented
reinstatement or correction of an erroneous match without silently bypassing a flag.
