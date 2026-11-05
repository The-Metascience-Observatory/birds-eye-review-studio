# Architecture

How the pipeline, the paper store, the studio and its job engine fit together.
The statistical contract (estimate selection, pooling identity, design classes,
GRADE, PRISMA) is in [scientific-validation.md](scientific-validation.md); this
document does not repeat it.

## Repository layout

```
run_pipeline.py        CLI driver over ber_core/pipeline.py
steps/                 one script per pipeline step (run by path from a checkout, not packaged)
ber_core/              the shared library: every step, script and the studio server import it
prompts/               LLM prompt templates; prompts/<slug>/ holds per-project overrides
configs/               example.yaml (annotated); review configs live here, git-ignored
scripts/               standalone tools; maintenance/ holds repair tools
tests/                 pipeline test suite
server/                FastAPI backend + arq job engine for the studio
studio/                Next.js 15 frontend
packages/dashboard-ui/ React dashboard components shared by the studio and the public website
projects/<slug>/       CLI run artifacts (git-ignored)
```

Steps are scripts rather than modules: each puts the repo root on `sys.path`,
takes `--config`, and is resumable. Everything they share lives in `ber_core`,
imported as `from ber_core import utils`.

### `ber_core` module map

| Area | Modules |
|---|---|
| Pipeline definition and state | `pipeline` (the canonical step sequence: the single source of truth), `projects` (project paths, pipeline state derived from disk), `execution` (output contracts: `verify_produces`, freshness), `defaults` (shared config defaults), `stage_eligible` (route pass-1-eligible papers into pass-2 buckets) |
| Papers and storage | `paper_store` (the per-paper store and only path resolver), `storage` (atomic writes, per-review paths), `identity` (DOI/PMID normalization, slugs), `migrate_to_paper_store` (legacy layout migration), `supplement_import` |
| Full text | `conversion` (conversion-quality facts, `TABLE_BEARING_MODES`), `docling_guard`, `html_convert` (publisher HTML → Markdown), `extraction_inputs` (bounded, source-labelled pass-2 inputs), `conversion_benchmark` |
| Search and metadata | `search` (per-query coverage ledger), `metadata_fetcher` (Crossref/OpenAlex/… metadata), `local_sciscinet` (optional local metadata tier), `pregate` (keyword pre-gate before any LLM call) |
| LLM access | `claude_cli` (one wrapper for every provider: sandboxing, credential scoping, auth latch), `codex_backend` (Codex CLI transport), `environment` (credential variables, `.env.local` loading), `usage_guard` (usage-limit stop), `batch_classify` (shared scaffolding for batch classifiers), `llm_search` (query drafting) |
| Evidence and statistics | `evidence` (identity, eligibility, overrides, exclusion rules), `scientific` (estimate selection and design classes), `meta_analysis` (pooling engine), `grading` (GRADE, risk of bias, poolability), `quote_verification` (check extracted quotes against the source text), `prisma` (PRISMA counts), `constants` (status sentinels, vocabularies), `utils` (shared helpers: config loading, JSONL/CSV I/O, paper discovery) |
| Outputs | `bundles` (versioned, atomically replaced dashboard bundles), `dashboard_summary` (bundle aggregates), `synthesize_review` (narrative review document) |
| INSPECT-SR (step 19) | `inspect_sr`, `inspect_sources`, `inspect_stats`, `inspect_scope`, `inspect_corpus`, `inspect_visuals`, `inspect_report`, `inspect_dashboard` |

## The pipeline

### Canonical steps

`pipeline.canonical_steps(project, ...)` returns an ordered list of `Step`s
(`id`, `num`, `label`, `script`, `args`, `produces`). `run_pipeline.py` and the
studio's runner both build their work from it. The step table is in the
[README](../README.md#pipeline-steps); `run_pipeline.py --dry-run` prints the live
sequence for a config.

Each step has a stable **id** (persisted by the studio; never renamed) and a
**number**. The numbers are historical selectors, not run order: `classify.py`
runs as step 3 (`quick`) and step 8 (`screen`), the prerequisites 5–7 and 10–11
run after step 9, and several steps share number 16. `run_pipeline.py --list`
prints the run order. The CLI selects by id or number (`pipeline.select_run`);
code should select by id (`pipeline.select_step_ids`).

Metadata enrichment runs **after** screening, for screened papers only; a
pre-filter enrichment of the whole corpus was dropped as hours of API calls over
mostly irrelevant papers. The export backfills bibliography from the local cache.

### Prerequisites before pass 2

`pipeline.execution_steps(project, selected)` inserts, before the first pass-2
step that was selected:

- `acquisition_steps`: `steps/download_pdfs.py`, `steps/convert_pdfs.py`,
  `steps/install_structured.py` (full text into the paper store);
- `eligibility_steps`: `steps/extract_pass1.py` (full-text eligibility) and
  `steps/stage_eligible.py --execute --clean` (route eligible and force-included papers
  into `eligible_rct/` / `eligible_observational/`).

With `staging: manual` the eligibility steps are omitted and the `eligible_*`
buckets are left exactly as curated (`--clean` would otherwise wipe them).
Pass 2 binds `--input <bucket>` explicitly and fails when the bucket is missing;
there is deliberately no fallback to "the whole corpus".

Selecting only later steps (e.g. `--only 13_build_bundle`) inserts nothing, and a
prerequisite can be selected on its own (`--only 6_convert_pdfs`). In the studio, the
same prerequisites also run as background jobs (below), so the wizard never waits
on them.

### Output verification and resumability

Each step declares `produces`. After a step exits 0, `run_pipeline.py` checks the
declared outputs exist and are non-empty (unless the step allows empty output) and
stops otherwise; it warns when a step updated none of them
(`ber_core.execution.verify_produces` / `freshness_warning`).

Every LLM step skips already-processed records (by DOI for CSV steps, by
`paper_id` for JSONL), uses a thread pool (`--workers`), and appends results as it
goes, so a run can be interrupted and resumed. Records with `_status: no_text` are
retried on later runs. A credential rejection or a usage-guard stop latches,
cancels the queued work, keeps the results already written, and exits non-zero
from the main thread (`claude_cli.raise_if_auth_failed`: 2 for credentials, 3 for
usage).

### Config-declared steps

`extra_steps:` in a config adds topic-specific scripts at step 16 with ids
`extra:<id>`; `script` resolves relative to the config file, and `--config` is
always passed first. `prompts_dir:` points prompt overrides at a directory beside
the config (exported to the scripts as `BER_PROMPTS_DIR`). This is how a review
kept outside this repository ships its own derivations.

### Identifiers

- **Canonical DOI** — `ber_core.identity.canonical_doi` strips `doi.org` URLs and
  `doi:` prefixes, URL-decodes, decodes both filename encodings, drops trailing
  punctuation, lowercases and validates. `join_key` adds `pmid_<n>` ids for
  records without a DOI. Use these to join across files.
- **Filename form** — `utils.doi_to_filename` replaces `/` with `--`
  (`10.1001/jama.1` → `10.1001--jama.1`); `utils.filename_to_doi` is the inverse.
  Rows in the studio are keyed by `norm_doi` / `norm_paper_id`.

### Extraction

Pass 1 (`steps/extract_pass1.py`) is a cheap full-text eligibility screen; pass 2
(`steps/extract_pass2.py`) is the full extraction. RCTs and observational studies use
different schemas, so they are written to separate files:

| | RCT (`--design-type rct`) | Observational |
|---|---|---|
| Output | `trial_extractions_pass2.jsonl` | `observational_extractions.jsonl` |
| Sample size | `n_randomized_total` | `n_enrolled_total` |
| Risk of bias | RoB 2 | ROBINS-I |

Text is read by `utils.read_best_text` in the order body.md → publisher XML →
PDF (PyMuPDF), rejecting garbled or paywall-boilerplate text. Pass 2 prefers XML
when `fulltext.xml_first` is set and retries on the next source when an
extraction has no outcome numbers. Extracted fields carry `*_source` markers
(`verbatim | calculated | inferred | not_reported`).

Every record carries `_status`. `ok` is success; anything in
`constants.FAILED_STATUS` (`no_text`, `parse_error`, `error`, `no_json`,
`truncated`, `policy_skip`, `refused`, `ineligible`, `low_quality_text`, `failed`)
is never pooled, published or counted as included.

### Export and the dashboard bundle

`steps/export_dashboard.py` is the only place RCT and observational records are
merged. It unions both files, drops failed records, applies delivery and manual
exclusions and duplicate publications once, routes by `topic.indication_focus`
(`treatment` | `prevention` | `all`; the other kind goes to a
`<kind>_trials.jsonl` side feed), validates every record against the dashboard
contract, and — with `--meta-analysis` — pools via
`meta_analysis.build_meta_analysis` (the same function `scripts/pool_meta_analysis.py`
uses). The bundle is built in a staging directory and swapped into
`projects/<slug>/dashboard/` atomically (`ber_core.bundles`); bundles carry a
content fingerprint and the scientific rules version.

A shrink guard diffs the new feed against the existing one and refuses to drop
previously published studies or metrics without `--allow-shrink`. Inside a
pipeline run the flag is always passed, because the local bundle is a preview;
publishing re-checks the guard and needs explicit approval to shrink.

`steps/generate_dashboard.py` then resolves which panels the data supports and writes
`dashboard.manifest.json`.

## The paper store

Every artifact for one paper lives in one folder, filed under the article type the
abstract screen assigned:

```
projects/<slug>/papers/            (or dirs.base_pdf_dir)
  Clinical_Trial/10.1000--jama.2021.1234/
      10.1000--jama.2021.1234.pdf      the PDF
      10.1000--jama.2021.1234.xml      publisher JATS, if any
      body.md  abstract.md  provenance.json  references.json
      supplementary/                   filed by steps/install_structured.py
  _unclassified/                       no (valid) article type yet
  _incoming/                           download staging
```

- **`paper_store` resolves every path.** `build_index()` / `paper_dir()` find a
  paper by normalized DOI regardless of its type folder; `ensure_paper_dir()`
  places one. Never construct `papers/<type>/<stem>` to look something up: the
  article type can change.
- **One root.** Every entry point accepts the config (`dirs.base_pdf_dir`); a
  configured store is recorded in `<project>/.paper-store.json`.
- **Re-screening re-files.** `steps/classify.py quick` ends with `paper_store.refile()`,
  which moves folders whose type changed, merges a second folder for the same DOI
  into its existing home, and re-points the `eligible_*` symlinks. Refiling takes
  the store lock exclusively; downloads and conversions hold it shared.
- **`eligible_*` buckets** are flat directories of symlinks into the store
  (target computed by `paper_store.bucket_link_target`).
- **Legacy layouts still resolve.** `utils.discover_papers()` reads the typed
  store, a nested `converted/<stem>/` layout and flat directories.
  `steps/download_pdfs.py` and `steps/convert_pdfs.py` migrate a legacy project on first
  use (`--no-auto-migrate` to opt out); `scripts/maintenance/migrate_to_paper_store.py` does it
  explicitly and is a dry run by default. Migration moves by `rename()`, so
  hard-linked corpora keep their links; differing name collisions are kept as
  `.dup` and reported, never deleted.

## Full-text conversion

`steps/convert_pdfs.py` drives the sibling `pdf4llm` package once per type bucket, so
`body.md` lands beside its PDF. The default `--profile content` uses docling
(sections, **tables**, figures) without GROBID; `--profile bibliographic` merges
GROBID's abstract and references with docling's body. Without docling, pdf4llm
degrades to text with no tables while still reporting success, which silently
loses table-only outcomes. Guardrails:

- `ber_core.docling_guard` probes docling before converting and exits non-zero rather
  than degrading. `--fix-docling` (or `BER_FIX_DOCLING=1`) attempts the usual
  repair (reinstall numpy/pandas/pyarrow/docling) and re-execs at most once;
  `--allow-degraded` is the deliberate escape hatch.
- `ber_core.conversion.TABLE_BEARING_MODES` is the one list of `extraction_mode`
  values that carry tables; every convert run prints its mode counts.
- Tables are serialized as HTML (`<table>` with `rowspan`/`colspan`), not
  Markdown pipe tables, which cannot express merged headers.
  `html_convert.count_tables()` counts both forms; `provenance.json` records
  `table_format`.
- Evidence rows expose `conversion_mode`, `conversion_degraded` and
  `tables_captured`; `scripts/maintenance/redo_degraded_conversions.py` re-converts and re-extracts
  affected papers (dry run unless `--execute`).
- `steps/convert_pdfs.py --html-only` converts papers whose only full text is
  publisher HTML (and that have neither XML nor a PDF) to `body.md`.
- Comparing converters on a frozen sample, outside the live store:
  [conversion-benchmark.md](conversion-benchmark.md).

## The studio

### Server

FastAPI with async SQLAlchemy (SQLite by default, Postgres supported) and an
arq/Redis job queue. The server never runs pipeline logic in process: it runs the
step scripts as subprocesses (`services/runner.py`), with each review's
workspace (`DATA_ROOT/reviews/<id>/`, containing `config.yaml` and
`projects/<slug>/`) passed through `BER_PROJECTS_DIR`. Scripts run on the base
Python interpreter (or `PIPELINE_PYTHON`), not the server's venv.
`services/pipeline_bridge.py` is the main importer of the pipeline.

Config lives both in the database (`ReviewConfig.config_json`, what the UI reads)
and on disk (`config.yaml`, what the pipeline reads); every write goes through
`services/configuration.py`, which validates it and re-renders the file. There is
no Alembic: startup creates missing tables, so new state goes in new tables.

### Job engine

Two arq queues, two worker processes (`server/app/worker.py`):

| Settings | Queue | Runs |
|---|---|---|
| `WorkerSettings` | `ber:default`, `max_jobs=1` | everything that calls an LLM: pipeline steps, the full-text eligibility screen, synthesis |
| `PdfWorkerSettings` | `ber:pdf`, `max_jobs=1` | non-LLM background work: PDF download + conversion, metadata enrichment |

LLM work is serialized because it may run on the host's shared CLI login.
Background kinds (`metadata`, `pdf`, `fulltext_screen`) are exempt from the
one-job-per-review guard (`services/jobs.py`). A job whose workspace is busy, or
whose shared CLI is leased to someone else, is deferred and retried
(`WORKSPACE_BUSY_DEFER_S`, `LLM_LEASE_DEFER_S`) instead of occupying the worker.
Failed jobs are never retried automatically.

Job rows are committed before dispatch. A reconciler in the API process
(`services/reconcile.py`, every 15 s) re-dispatches queued rows Redis lost, fails
rows whose worker died, and — for reviews where the user has started work —
enqueues background jobs for papers that need metadata, full text or a full-text
verdict. No open browser tab is needed.

Job logs stream over SSE (`GET /jobs/{id}/logs`) and are scrubbed of secrets and
absolute server paths (`services/redact.py`).

### Wizard stages

`server/app/services/stages.py` is the stage registry: frozen `StageDef`s grouped
into phases (Define · Search · Screen · Review · Extract · Meta-analysis · View
dashboard). Every canonical step belongs to exactly one stage (`extra:` steps
belong to Extract); `phase` is presentation only; `hidden` stages are skipped by
the walkthrough; `requires_approval` makes a stage a gate. `resolve_stages`
filters each stage to the review's actual steps, and a stage whose steps all
vanish is skipped. Slugs are permanent URLs (`/reviews/<id>/stages/<slug>`).

The screening flow is: **Screen** (`classify`: the abstract screen, the trial
screen and enrichment) → **Review** (`triage`: a human looks) → the hidden
full-text eligibility screen (runs as the `fulltext_screen` background job as
full text arrives) → **Extract** (pass 2 and `extra:` steps).

### The evidence row

`services/evidence.py::build_evidence` merges every on-disk artifact for a review
into one DOI-keyed row per paper (search → abstract screen → trial screen →
pass 1 → pass 2), each a tier; a blank later value never erases a filled one.
`_screen_state` derives one verdict per row — `included | pending | excluded |
screened_out` — which the pill, row tint, sort order and PRISMA counts all share.
`has_fulltext` means body.md, publisher markup or a recorded conversion failure,
not merely a folder.

Manual include/exclude write `user_inclusions.json` / `user_exclusions.json`
(kept disjoint), the same files `stage_eligible.py`, the export and the
meta-analysis honour. A manual include is a force-include that reaches extraction
even when pass 1 said ineligible. An unreadable decisions file is an error, never
"no decisions".

Organism (`human | mouse | other_animal | other`) is labelled by the abstract
screen and refined by pass 1. Non-human papers are excluded unless the config
sets `include_animal_studies: true`; the policy is applied deterministically
after the model, so toggling it re-colours an already screened corpus.

### Frontend

The browser never calls the API cross-origin. `studio/src/lib/api/` calls the
same-origin path `/api`, which `studio/next.config.mjs` rewrites to
`BER_API_ORIGIN` (default `http://127.0.0.1:8000`). CORS is therefore not
exercised, the API can bind loopback, and one access-control layer can cover the
whole app. `NEXT_PUBLIC_API_BASE` is an escape hatch that opts back into
cross-origin calls (and CORS). `next.config.mjs` also sets the CSP and security
headers. `studio/src/lib/types.ts` mirrors `server/app/schemas.py`
(`server/tests/test_contract_drift.py` checks it).

### Publishing

Running the pipeline never touches a public site. Publishing is two-party: the
review owner requests it, an admin approves it, and `services/publish_deploy.py`
copies the bundle into `WEBSITE_DIR/data/birds_eye_reviews/<route>/` by staging
and atomically swapping the directory, after re-running the shrink guard.
`WEBSITE_DIR` is unset by default, in which case the instance cannot publish.

## Auth and access control

- **Sessions**: argon2 password hashes and stateless HS256 JWTs (`security.py`),
  sent as a Bearer header (no cookies, so no CSRF surface). Tokens must carry
  `exp` and `sub`.
- **Ownership**: every review- or job-scoped route depends on `deps.owned_review`
  or `deps.owned_job`; admin routes use `deps.get_current_admin`. Responses carry
  no absolute filesystem paths, and other users' emails and review ids are masked
  for non-admins.
- **Registration**: per-person invite codes (`invite_codes` table,
  `services/invites.py`), minted at `/admin/invites` or with
  `server/scripts/manage_access.py`. `SIGNUP_CODE` is a legacy shared-code
  fallback. Under `PUBLIC_MODE`, signup fails closed.
- **Revocation is immediate**: `deps.get_current_user` loads the user on every
  request and rechecks their invite and the `user_disabled` table
  (`manage_access.py disable|enable`, or the admin endpoints).
- **Custom PDF folders** (`dirs.base_pdf_dir`, the only user-supplied path the
  server writes to) are validated at write time and at read time
  (`services/pdf_dir.py`) and are disabled entirely under `PUBLIC_MODE`.
- **Operator-only config keys**: the API refuses `extra_steps`, `prompts_dir` and
  every `dirs.*` key except `base_pdf_dir`. Workspace configs have `extra_steps`
  stripped before the pipeline sees them unless `ALLOW_CONFIG_EXTRA_STEPS` is set
  (refused under `PUBLIC_MODE`).

## LLM providers and credentials

Five providers (`server/app/schemas.py::LLM_PROVIDERS`):

| Provider | Whose login | Billing |
|---|---|---|
| `codex_cli` (default) | the host's `codex login` | the operator (shared) |
| `claude_cli` | the host's `claude` login | the operator (shared) |
| `claude_token` | the user's `claude setup-token` OAuth token | the user |
| `anthropic_key` | the user's Anthropic API key | the user |
| `openrouter` | the user's OpenRouter key | the user |

A review's own provider wins, else the owner's account setting, else `codex_cli`
(`services/llm_prefs.py`). User credentials are stored Fernet-encrypted in the
`user_credentials` table (key derived from `APP_SECRET`) and verified with a tiny
model call when saved. Host-shared providers go through a single-occupancy lease
(`services/llm_lease.py`); `services/llm_access.py` applies the lease and
`PUBLIC_REQUIRE_BYO_LLM` to the review **owner's** effective provider, both at
the endpoint and when a job starts.

**Credential injection is exclusive, not additive.** Claude Code resolves its
credential by precedence: `CLAUDE_CODE_USE_*` > `ANTHROPIC_AUTH_TOKEN` >
`ANTHROPIC_API_KEY` > `apiKeyHelper` > `CLAUDE_CODE_OAUTH_TOKEN` > profiles >
the host login. A user's token sits below the API-key variables, and the workers
inherit the server's environment. So `llm_prefs.credential_env` yields every
variable in `ber_core.environment.MANAGED_VARS`, with `None` meaning "must be
unset", and both the subprocess environment (`runner.build_step_env`) and
in-process calls (`llm_prefs.provider_env`, which scopes the provider through
`claude_cli.llm_context` context variables rather than `os.environ`) clear
before they set. Never put `ANTHROPIC_API_KEY` in the repo-root `.env.local` of
a studio host.

Every Claude CLI child runs in a private empty directory with no tools, no MCP
servers and no user settings, with the prompt on stdin; the PDF-reading path gets
`Read` alone, confined to the directories it names. Codex calls are stateless and
text-only. A rejected credential latches and stops the run rather than being
recorded as a per-paper failure.
