# Validate conversion against numerical evidence

`scripts/benchmark_conversion.py` runs an isolated RCT-first benchmark. It never
converts into the paper store, downloads models, or labels existing extractions
as gold. LLM extraction requires explicit `extract --execute`; its default is a
prepared dry run. The initial target is 20 development reports
and 40 held-out reports. Related reports and supplements stay in one trial family.

## Inventory and verify the cohort

Run from the repository root:

```bash
python3 scripts/benchmark_conversion.py inventory \
  --project projects/<slug> --config configs/config_<topic>.yaml \
  --pilot <pilot-selection>.json \
  --output projects/_validation/pdf_conversion/inventory.json
python3 scripts/benchmark_conversion.py templates \
  --inventory projects/_validation/pdf_conversion/inventory.json \
  --output-dir projects/_validation/pdf_conversion/templates
python3 scripts/benchmark_conversion.py preflight \
  --output projects/_validation/pdf_conversion/preflight.json
```

Inventory nominations come from `eligible_rct` and the canonical `paper_store`
index, respecting `dirs.base_pdf_dir`. This is **not RCT verification**. Each
candidate starts with `is_rct: null`, unverified identity/family, and no gold.
The 20/40 proposal is deterministic but remains unusable as a verified benchmark.
Existing pilot RCT candidates and known inspected cases (pass each with
`--sentinel DOI`) are development sentinels. `--seed` fixes the split.
Observational pilot labels do not nominate observational papers into the benchmark.

Review PDFs, registry IDs, related reports, and supplements. Fill each candidate's
`verification`: `is_rct`, `identity_verified`, `family_verified`, `trial_family`,
`verified_by`, and `evidence_refs`. Identity verification covers the versions of
every pinned source, including publisher XML/HTML and supplements. Inventory
records those native files, supplementary Markdown/text/HTML, and conversion
provenance; importer bookkeeping is excluded. Add missing supplements as hashed source
artifacts; document acquisition gaps. A record claiming to be an RCT must have one
unambiguous main PDF. Whole-journal PDFs, protocols, corrections, wrong versions,
and mismatched articles require identity resolution rather than parser blame.

```bash
python3 scripts/benchmark_conversion.py split \
  --inventory projects/_validation/pdf_conversion/inventory.json \
  --output projects/_validation/pdf_conversion/cohort.json
```

The split excludes unverified/non-RCT candidates. All members of a verified trial
family stay together; a family containing a development sentinel stays in development.
Whole families can exceed the 20/40 targets. When fewer than 60 verified reports
exist, the split uses approximately one-third development and two-thirds holdout.
If sentinels consume the available pool, obtain fresh holdout reports; do not move
inspected sentinels into holdout. Inspect source/layout coverage before freezing:
scans, merged headers, long/multipage tables, footnotes, and numerical supplements.
The current deterministic splitter does not infer those strata automatically.

## Create an independently reviewed reference

The annotation templates intentionally contain no facts, reviewer approvals, or
positive source-coverage claims. They are not generated from existing LLM output.
Each independent reviewer creates a separate JSON file containing:

```json
{
  "paper_id": "10.1234/example",
  "reviewed_by": "reviewer-a",
  "status": "independent_review_complete",
  "source_coverage_complete": true,
  "sources": [{"path": "/absolute/source.pdf", "sha256": "...", "role": "main_pdf"}],
  "facts": []
}
```

`facts` must be populated from the source, including outcomes that all converters
miss. The adjudicated gold retains the template's fields and sets
`status: adjudicated`, `source_coverage_complete: true`, `conflicts_resolved: true`,
and `adjudicated_by`. Its `reviewers` contains two distinct reviewers, each with
`id`, `independent: true`, `source_reviewed: true`, and an `annotation` artifact
with `path` and `sha256`. These are explicit human attestations; the CLI cannot
establish independence by itself. Preserve both independent annotations even if
they agree. Set the candidate's `gold_path` to the adjudicated JSON.

Each fact contains every field below. Use `null` for an absent numerical field and
explicit `none`/`not reported` for applicable text fields. Use a fixed vocabulary
shared by annotation and extraction; the scorer does not guess synonymous labels.

```json
{
  "population": "long covid", "subgroup": "all", "arm": "intervention",
  "outcome": "FEV1 %VCMAX", "timepoint": "6 months", "n": null, "denominator": null,
  "statistic": "median", "value": "76.26", "units": "percent",
  "uncertainty_type": "IQR", "uncertainty_value": null,
  "uncertainty_lower": "73.33", "uncertainty_upper": "80.95", "uncertainty_level": null,
  "adjustment": "unadjusted", "primary": true,
  "evidence": {"source_sha256": "...", "locator": "page 11, table 3, FEV1 row, month 6"}
}
```

The numbers above illustrate the tuple shape, not adjudicated facts or an asserted
sample size/primary designation. Preserve distinctions between means, medians,
within-arm percentage changes, between-arm differences, SD, SE, IQR, and CI. The
cacao study's 51%/22.6% reductions must not become raw arm means. The observed FEV1
interval is an IQR; truncating 80.95 to 80.9 must fail exact scoring.

## Pin and run converters

Edit generated `adapters.json`. Snapshot copies existing artifacts and has **no
conversion latency**. Docling FAST/ACCURATE use the sibling `pdf4llm` content API,
disable alternate-parser fallbacks and bibliographic network enrichment, and share
the same OCR policy. Pin the installed package versions, converter source files,
and all model weights/configuration files using `{path, sha256}` artifacts.
Recreate these pins after intentional converter changes. No package upgrade occurs.

The PyMuPDF4LLM adapter uses `to_markdown` with explicit options. External Marker
and MinerU adapters use an operator-supplied `argv` array with `{input}` and
`{output}` placeholders, exact package/model/source pins, and a configuration
record. Implement a small wrapper for the installed release; no upstream CLI flags
are guessed. Wrappers must write `body.md` and `provenance.json` in the output
directory. Provenance declares `conversion_status` as `complete`, `partial`,
`degraded`, or `failed`. Missing/unknown status requires review. Additional
`document.json`/`tables.json` artifacts are retained when present.

An optional `environment` string-to-string map is frozen, validated, and applied
to both preflight and conversion. Docling templates set `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`, preventing a moving model revision from replacing the
hashed local weights during a run. Pin the complete local model assets first.

Generate a runnable evaluation contract from the review config:

```bash
python3 scripts/benchmark_conversion.py evaluation \
  --config configs/config_<topic>.yaml --model <exact-model-name> \
  --provider codex_cli --packaging legacy --input-budget 80000 \
  --output projects/_validation/pdf_conversion/evaluation-legacy.json
```

Choose the exact available model to test; model aliases are rejected. This
resolves the actual pass-2 system/user prompts without an LLM call, enables the
same full numerical extraction fields (`subgroup_analyses`, not `subgroup`), and
hashes the resolved prompt, code, options, model, and budget. A cloned config
clears source directories and extra pipeline steps. The default
`--source-policy pdf_only` fixes source selection to PDF-derived Markdown.
A converter-only development smoke can explicitly use model
`none-converter-only` and a hashed protocol; it provides no extraction score.

```bash
python3 scripts/benchmark_conversion.py freeze \
  --cohort projects/_validation/pdf_conversion/cohort.json \
  --adapters projects/_validation/pdf_conversion/templates/adapters.json \
  --adapter current-snapshot --adapter docling-fast --adapter docling-accurate \
  --evaluation projects/_validation/pdf_conversion/evaluation-legacy.json \
  --split dev --output projects/_validation/pdf_conversion/frozen-dev.json
python3 scripts/benchmark_conversion.py run \
  --frozen projects/_validation/pdf_conversion/frozen-dev.json \
  --output-dir projects/_validation/pdf_conversion/run-dev-001
```

GPU adapters require CUDA to be visible in the selected Python subprocess. A
sandbox can hide the host GPU; rerun preflight in the intended worker environment.
The tool refuses silent CPU fallback. Every converter/report gets its own output
directory and a supervised Linux process tree. A child-subreaper launcher keeps
detached and orphan workers owned; deadlines stop and kill descendants even when
they create new sessions. PID birth times guard against signalling recycled PIDs.
The non-Linux fallback only supervises the original process group. Source/model/
code drift stops execution; run directories and frozen files are not overwritten.
Historical snapshots reproduce their prior acceptance with an explicit
`historical_unknown` status when old provenance lacks a completeness field.

These runs measure **cold isolated document latency**, not warm batch throughput.
Report cold startup separately. A dedicated persistent-worker load experiment is
needed before claiming production warm latency, concurrency throughput, or a GPU
speedup. Artifact-copy times must never enter converter rankings. Conversion
provenance retains device and stage metrics when the adapter supplies them.

`--split holdout` refuses to freeze unless every selected report has verified
identity/family and two independent annotations with complete adjudicated gold.
Run only development finalists. Do not tune parsers, prompts, flags, or routing
after examining holdout results; changed choices need fresh held-out reports.

## Seal predictions, then score

Prepare three paired repetitions against the real pass-2 workflow:

```bash
python3 scripts/benchmark_conversion.py extract \
  --frozen projects/_validation/pdf_conversion/frozen-dev.json \
  --run-results projects/_validation/pdf_conversion/run-dev-001/results.json \
  --output-dir projects/_validation/pdf_conversion/extraction-dry-run
```

The default stages inputs and writes exact commands without LLM calls. Review
the plan, then use a **new** output directory and add `--execute` to run. Each
adapter/repetition gets a fresh `BER_PROJECTS_DIR`, config, prompt, raw output,
and log. For the default PDF-only experiment, only body Markdown and conversion provenance are copied: no XML, PDF,
or supplementary files can silently substitute for the candidate conversion.
Pass 2 receives `--no-xml-first`, `--no-retry-on-empty`, a fixed input budget and
packaging mode, and one worker. Input hashes/lengths, command, wall time, costs,
tokens, and raw output hashes are retained. Each executed job also writes
`scientific-comparisons.json`, using BER's existing scientific validators and
comparison selection on compatible raw records. It retains semantic identities,
estimates, analysis-scale variances, eligibility, and withheld reasons for paired
inspection. These derived calculations are not adjudicated gold.
The three repetitions remain separate;
do not combine them into an artificially larger sample of independent reports.

Each executed job produces a pending `mapping-template.json` with raw numerical
JSON pointers. Explicitly map the raw pass-2 schema to the outcome contract before
scoring; no parser guesses that percentages are means or IQRs are CIs. For each
non-null numerical field provide `raw_fields`, e.g.
`{"value": "/outcomes/0/arm_results/0/median", "uncertainty_lower": "/outcomes/0/arm_results/0/iqr_low"}`.
Provide source hashes/locators and `mapping_verified_by`; unresolved mappings
remain `review_required: true`. Every mapped number must exactly match its raw
JSON-pointer value. Raw record hashes must agree. This semantic mapping is done
before looking at gold, then sealed along with the flags.

```bash
python3 scripts/benchmark_conversion.py map \
  --frozen projects/_validation/pdf_conversion/frozen-dev.json \
  --mapping completed-mapping.json --output predictions-docling-fast.json
```

Existing scientific validators are only appropriate for their own compatible
schema, not these flat outcome tuples. The explicit mapping layer avoids treating
raw extraction outputs as adjudicated source truth.

Prediction JSON has `adapter` and a `papers` list. Each paper has `paper_id`,
`review_required` (boolean), `flags` (list), and `facts`. Facts use the numerical
contract above plus explicit `review_required`. Account for every report,
including failed conversions and reports with no extracted facts. Flags and
abstentions must be produced before looking at reference labels.

```bash
python3 scripts/benchmark_conversion.py seal \
  --frozen projects/_validation/pdf_conversion/frozen-dev.json \
  --run-results projects/_validation/pdf_conversion/run-dev-001/results.json \
  --predictions predictions-docling-fast.json --output sealed-docling-fast.json
python3 scripts/benchmark_conversion.py score \
  --frozen projects/_validation/pdf_conversion/frozen-dev.json \
  --sealed sealed-docling-fast.json --baseline scored-baseline.json \
  --output scored-docling-fast.json
```

Development-only `--dev-gold-map` accepts a `paper_id: gold_path` JSON mapping when
gold was not available at development freeze. Holdout gold is always frozen before
running. Score the snapshot first without `--baseline`, then compare candidates
against that score. Scores must share identical sources, gold, and extraction
configuration. A seal detects subsequent edits; it is an audit record, not a
cryptographic proof that an operator never viewed gold.

Scoring preserves exact decimals (51 and 51.0 agree), inequality signs, statistic
types, uncertainty, units, denominators, arm/timepoint/population, and source-file
attribution. It does not perform unit conversions or percentage rescaling.
Locators are required for inspection; their prose is not graded as exact text.
Duplicate predictions count against precision. Missing facts count against recall.
Wrong arm/time/statistic/uncertainty labels are diagnosed separately. Unsupported
automatically accepted facts count as critical errors.

The numerical gate requires zero unflagged critical errors, positive automatically accepted
primary coverage, and no automatic primary-recall regression against baseline.
Flagging everything cannot pass. Report raw precision/recall, automatic precision/
recall, flagged reports/facts, and the observed sample size separately. Zero errors
on a small correlated sample do not prove zero production risk. Prefer correct
primary completeness, then lower review burden, then latency. A speedup is a target,
not a reason to accept numerical errors. Passing this non-regression gate alone
does not justify promotion: require improved primary coverage or demonstrated
operational benefit, the full development/holdout sweep, and independent gold.
The current automated scorer measures extraction tuples; it does not separately
score every parser table cell/span against a rendered table reference. Audit table
structure and omitted rows directly until an independently annotated cell-level
reference is available.

Assess table-aware packaging and supplement inclusion as a separate experiment
with its own frozen configuration. Broaden to observational reports only after
RCT validation. Unit tests use fake converters and temporary files:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p xdist tests/test_conversion_benchmark.py
```

## Separate production source-routing experiment

After the PDF-only converter comparison, create a distinct evaluation contract:

```bash
python3 scripts/benchmark_conversion.py evaluation \
  --config configs/config_<topic>.yaml --model <exact-model-name> \
  --provider codex_cli --source-policy production --packaging table-aware \
  --output projects/_validation/pdf_conversion/evaluation-production.json
```

Freeze this independently using an inventory that pins native sources. It is
tagged `production_workflow`; converter-only experiments are tagged
`pdf_converter`. Their scores cannot serve as each other's direct baseline
because their frozen source policies and extraction configurations differ.

Production `extract` copies verified, hashed main XML and canonical supplementary
inputs alongside candidate Markdown into isolated paper folders. It uses
`--xml-first` and still disables source retry and external acquisition. An XML
source can be staged even when the candidate PDF conversion failed. Main PDFs
are not copied into the extraction workspace, so whole-PDF fallback remains
disabled. Multiple main XML candidates require explicit selection/verification.
Supplement subdirectories are preserved, avoiding collisions between e.g.
`supplementary/a/body.md` and `supplementary/b/body.md`. Traversal, mismatched
relative paths, outside-directory files, collisions, and source hash drift stop
the run. No identity is inferred automatically from filenames.

Native publisher HTML is pinned and staged for audit, but the current
`read_best_text` reader does not select raw HTML. The staging manifest records
that limitation; preconvert matching HTML through the supported converter before
claiming native HTML coverage. Use table-aware packaging for supplement inclusion;
the legacy packaging control intentionally omits supplements.

Raw pass-2 records and derived scientific reports record the actual selected
source. Scoring remains conservative: a candidate conversion's explicit review
flag is retained even when production selected complete XML instead. Inspect
the source-specific results separately; that unused-PDF warning can understate
production automatic acceptance and is not evidence of an XML extraction error.

## Rescuing selected pages with MinerU

`scripts/mineru_standard_adapter.py` wraps a version-pinned MinerU install that
lives in its own virtual environment (MinerU is not a dependency of this repo).
Point it at that runtime with `--runtime-root` or `BER_MINERU_RUNTIME_ROOT`, and
run it with the runtime's own interpreter from the repository root:

```bash
<mineru-runtime>/venv/bin/python scripts/mineru_standard_adapter.py INPUT.pdf \
  projects/_validation/pdf_conversion/rescue/NEW_FOLDER \
  --runtime-root <mineru-runtime> --pages 10-12
```

Omit `--pages` to process the complete PDF. The wrapper verifies its locked local
models and runtime, refuses existing candidate artifacts, retains native output,
table HTML and captions/footnotes, and avoids external LLMs. A page-only run is
explicitly partial and marked as requiring review. It never replaces the original
conversion or silently substitutes differing numerical values.
