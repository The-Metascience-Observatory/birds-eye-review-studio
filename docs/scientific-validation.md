# Scientific validation contract

`ber_core.scientific` owns estimate validation, comparison selection,
compatibility keys, study identity and result interpretation. Extraction
attaches its findings without changing the extracted evidence.
`meta_analysis.build_meta_analysis`, `scripts/pool_meta_analysis.py`,
feasibility, dashboard projection and narrative synthesis consume this contract.
Domain names are navigation categories, never pool keys.

The current rules version is `ber_core.scientific.RULES_VERSION` (`2026-10-03.1`
at the time of writing); increment it in Python and the shared dashboard
TypeScript constant when scientific rules change. Bundle fingerprints include
this version. Older bundles remain readable as historical artifacts but require
rebuilding before interpreting pooled results. Narrative synthesis also checks
the source fingerprint. No database schema migration is required.

## Evidence and estimands

A selection identifies both arm IDs, outcome/construct, units, population,
assessment window, design, analysis population, endpoint/change/model basis,
effect measure, uncertainty and source location. Findings retain field paths,
source quotes where available, extraction version and rule version. Quote
matching and numerical consistency alone do not verify table-column attribution.

Selection prefers explicit primary author analyses, then adjusted author
contrasts, then stable extraction order. It never selects the smallest SE.
Model-calculated estimates must be reproduced from verified raw summaries. Raw
means require participant denominators and explicit SD/ordinary-SEM/mean-CI
semantics. Adjusted means and model SEs cannot reconstruct raw SDs. Crossover
and cluster estimates require verified design-adjusted comparison variance.
Source-uncertain or invalid fields remain unchanged and withheld.

Blocking source findings apply to their recorded field scope: an outcome-level
finding withholds its estimates, and a field-level finding prevents use of that
result. Unused assessments and sibling outcomes remain independent. These source
holds also prevent benefit claims in the result summary. Warning findings remain
nonblocking.

A quoted mean marked as a percentage cannot be pooled under non-percentage
outcome units. Such a conflict is retained and flagged for source review; the
validator does not convert or relabel the value.

Default MD pooling requires common outcome units. SMDs require an explicit
common construct and compatible standardization; endpoint and change SMDs remain
separate. No binary-to-SMD or hazard-to-odds approximation is performed.
Protocol mappings can be supplied in `meta_analysis.outcome_mappings`, keyed by
the exact outcome name, with `concept`, `unit`, `population`, `time_window` and
optionally `smd_construct`. Mappings describe verified compatibility; changing a
unit label does not convert numerical values.

## Design classes

`ber_core.scientific.design_label` is the single definition of randomized versus
not; the pool keys, GRADE's starting level (`grading.design_class`), the
export's `is_rct` flag, the dashboard's RCT/observational totals and pass-2
bucket routing (`stage_eligible._route`) all call it. Fine classes are `rct`,
`cluster_rct`, `crossover`, `observational` and `unknown`; the first three are
randomized (`RANDOMIZED_CLASSES`). An explicit denial of randomization
("non-randomized", "quasi-experimental", "quasi-randomized", "single-arm",
"uncontrolled") always means non-randomized, even if a randomization field is
filled. A label that names the setting rather than the allocation
("human-challenge", "clinical trial", "pilot") or no label at all is randomized
only when the record documents a randomization method or a real
allocation-concealment mechanism ("unclear" is not evidence); otherwise a
setting label is non-randomized and a missing label is `unknown`, which blocks
pooling. "cluster" without a randomization word is `unknown`; cluster and
crossover estimates still require verified design-adjusted variance. Without an
observational extraction pass, a non-randomized *trial* is staged into the RCT
bucket (the RCT prompt records its design type) but is still classified
non-randomized downstream.

## Pooling identity

A comparison's treatment identity is, in order: the protocol's
`synthesis_intervention`, the canonical name from
`steps/canonicalize_interventions.py` (`intervention_canonical`), the verbatim
`intervention_name`, then the arm label (`scientific.treatment_identity`).
`meta_analysis.attach_canonical_interventions` stamps the canonical name on
every intervention arm before pooling in both the export and
`scripts/pool_meta_analysis.py`, so naming variants ("Azelastine 0.1%",
"azelastine nasal spray") pool together. Non-placebo comparators resolve in the
same order (canonical names are stamped on intervention arms only). **Existing
reviews must be re-exported (`steps/export_dashboard.py --meta-analysis`) to
pick this up**; a bundle built earlier keeps verbatim-name groups.

All explicit intervention/comparator pairs are preserved. Equivalent arms that
would duplicate a control within one pool are withheld until a deliberate
combined-arm analysis or distinct regimen definition is available. Verified
registration/report links establish study identity; author/year/sample-size
matches only generate review flags. Descriptive dashboard charts choose an
explicitly primary report, otherwise stable report ID; all reports remain in the
table.

## Results and methods

Result labels use the specified primary assessment, otherwise the final
identifiable on-treatment assessment. Acute post-dose observations at zero
elapsed weeks remain eligible. P-value operators are preserved; under the strict
`p < .05` convention, `p ≤ .05` alone is unresolved at the boundary. Confidence
intervals use zero for differences and one for ratios. Missing or conflicting
information is “Insufficient extracted data”; a nonsignificant test is “No
statistically significant difference”, not evidence of equivalence. Unknown
clinical direction does not imply benefit.

Unconfigured pools use the existing dashboard defaults: random effects, DL
heterogeneity, Wald intervals, minimum two independent studies. Explicit
supported settings remain in the output. Pools below ten studies also report
ordinary HKSJ and variance-floored HKSJ sensitivity. Ordinary HKSJ may collapse
under zero residual heterogeneity; its floored variant can be very conservative.
Method-dependent interval conclusions are flagged. Native outcome direction is
preserved.

Subgroup analyses pool each moderator level separately. Trials whose record
lacks the moderator are pooled and reported as an `(unknown)` group
(`in_between_test: false`, `n_unknown`) but are not a level of the Q-between
test of subgroup differences; with fewer than two recorded levels no test is
reported.

## Certainty (GRADE) and risk of bias

Every pooled group carries `rob_summary` (counts of studies at `low` / `some`
concerns / `high` / `unknown` risk of bias, read per study by
`grading.parse_rob` from `overall_judgment`, else the worst `domain_judgments`
entry; ROBINS-I `serious`/`critical` count as high and `moderate` as some
concerns), `total_n` (analysed n, else randomized/enrolled n, only when every
study has one) and `certainty` = `{grade, start, downgrades: [{domain,
reason}]}` from `grading.grade_certainty`. Randomized classes start High and
everything else Low. One level is removed for risk of bias when at least half
the rated studies are high risk (or at least half have some concerns or high
risk); one for I² ≥ 50% and two for I² ≥ 75%; one for imprecision when the
interval crosses the null (1 for ratios, 0 otherwise) or the optimal information
size is not met (fewer than three studies or under 400 participants); and one
for publication bias when Egger's test is significant (p < .10) with at least
ten studies. Indirectness is not assessed automatically. The rating is
deterministic and descriptive; it does not replace a reviewer's GRADE judgement.
Each trial row also carries its own `rob`. Both the export
(`steps/export_dashboard.py --meta-analysis`) and
`scripts/pool_meta_analysis.py` produce the same fields, and narrative synthesis
reports them in its certainty section.

`meta_analysis.json` contains every selected/withheld comparison and validation
finding. The eager summary includes a bounded preview of withheld reasons;
Studio loads the full audit separately. The shared forest adapter only displays
groups stamped with the current rule version.

## PRISMA and rebuilding

PRISMA uses normalized identifier sets and mutually exclusive dispositions for
screening/full-text exclusions, pending stages, duplicate reports and reconciled
included reports. Manual decisions take precedence; overlapping include/exclude
decisions are flagged and pending. Raw search identifiers must reconcile before
a duplicate-removal count is stated. Historical extractions without
corresponding eligibility decisions remain visible as discrepancies, not
invented funnel entries.

Builds stage a complete directory and atomically exchange it with the live
bundle on Linux; a portable rollback path protects the old directory elsewhere.
Corrections require an artifact backup, a source-linked field change log and
unresolved findings. Rebuilding does not publish a review.

Extraction keeps each table together with its caption and notes. For oversized
XML tables, it first removes redundant cell alignment and explicit spans of one.
An XML tree comparison verifies that text, cell order, other attributes and
nontrivial spans are unchanged. The input manifest records both block hashes and
sizes; the original source file is preserved. Tables that still exceed the input
budget remain explicitly marked as omitted and require recovery or review.

## Record set and indication routing

Extraction records whose `_status` is in `constants.FAILED_STATUS` (`no_text`,
`parse_error`, `error`, `no_json`, `truncated`, `policy_skip`, `refused`,
`ineligible`, `low_quality_text`, `failed`; `utils.FAILED_EXTRACTION_STATUSES`
is the same object) are never pooled, published or counted as included. The
export and `scripts/pool_meta_analysis.py` pool the same set: pass-2 records
first, then observational records not already present; delivery and manual
(`user_exclusions.json`) exclusions applied once; duplicate publications dropped
only when their canonical report is present (`utils.load_duplicate_dois` /
`drop_duplicate_records`); then indication routing.

`steps/classify_intervention_indication.py` labels each record `treatment`,
`prevention`, `both` or `exclude` with a topic-generic prompt
(`prompts/classify_study_indication.txt`; a project may override it under
`prompts/<slug>/`) and the condition definition stored under
`topic.condition_field`. The export keeps the feed on `topic.indication_focus`:
`treatment` (default, the historical behaviour) moves prevention studies to
`prevention_trials.jsonl`; `prevention` moves treatment studies to
`treatment_trials.jsonl`; `all` keeps both. `both` studies stay in the feed and
also appear in the side feed; `exclude` records are dropped; unclassified
records are kept. Reviews whose question includes prevention should set
`indication_focus` explicitly.

## Regression coverage

`tests/test_scientific_validation.py` covers two failure patterns seen in real
trial reports (the "kim" and "hodis" fixtures: an earlier-timepoint p-value
attached to the primary result, and study-wide counts or adjusted SEs taken as
SDs),
four-arm/shared-control selection, incompatible outcomes and standardizations,
acute/crossover/cluster data, incomplete primary outcomes, study/report
identity, manual/legacy PRISMA reconciliation, native selection parity and
independent statistical reference calculations. `tests/test_design_classes.py`
pins the design vocabulary table and that every consumer agrees;
`tests/test_meta_analysis.py` covers GRADE downgrades, risk-of-bias parsing, the
canonical pooling identity and the `(unknown)` subgroup;
`tests/test_batch_classifiers.py` covers the classifiers' atomic resume,
consolidation caching, config vocabulary and indication routing.
`server/tests/test_scientific_contract.py` checks Studio response provenance and
version handling. Existing summary, feasibility, meta, PRISMA, bundle and
contract suites cover their consumers.
