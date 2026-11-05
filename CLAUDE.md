# CLAUDE.md

Orientation for coding agents. `README.md` is the user-facing entry point (with a
folder map), `docs/architecture.md` the full design (with a `ber_core` module
map), `DEVELOPMENT.md` the dev workflow. This file is the dense version plus the
invariants and traps that bite when editing. Bare module names below
(`utils`, `pipeline`, `paper_store`, …) live in `ber_core/`; studio modules are
under `server/app/`.

## What this is

An AI-assisted systematic-review tool: search 100K+ medical papers, screen them,
extract structured data, pool it, build a dashboard bundle. Two front doors share
ONE pipeline:

- **CLI pipeline** — the step scripts in `steps/`, driven by
  `run_pipeline.py --config …`.
- **Studio** — FastAPI (`server/`) + Next.js (`studio/`) web app driving the same
  pipeline as a wizard, with a job queue, a live evidence table and per-stage
  approval.

## Layout (where things go)

- `steps/` — one script per pipeline step, run by path (`python steps/x.py`),
  never imported as a package. Each starts by putting the repo root on
  `sys.path` and resolves `.env.local` / `projects/` from the repo root
  (`BASE_DIR = parents[1]`). Tests load them with `spec_from_file_location`.
- `ber_core/` — everything shared: imported as `from ber_core import utils` /
  `from ber_core.utils import …`. Never add a root-level module or a re-export
  shim (see the credential trap below: two copies of `claude_cli` = two
  credential states).
- `scripts/` — standalone tools (not in a run); `scripts/maintenance/` repair
  tools are dry-run by default.
- `configs/` — `example.yaml` (annotated); review configs and per-review
  prompt overrides (`prompts/<slug>/`) are git-ignored and stay local. Keep
  topic-specific code out of the repo too: a review's own scripts belong in its
  `extra_steps:` beside its config.
- `server/app/services/` is the heart of the studio (stages, evidence, runner,
  jobs, reconcile, pipeline_bridge, llm_*); `server/app/routers/` the API.
- `studio/src/lib/api/` is the typed client; `packages/dashboard-ui/` is shared
  with the public website (a separate repo that only consumes the bundle).
- Git-ignored: `projects/`, `server/_data/` (per-review workspaces), `.env.local`.

## The pipeline (essentials)

- `pipeline.canonical_steps` → ordered `Step(id, num, script, args, produces)`.
  `Step.script` is a bare filename in `steps/`, resolved by
  `pipeline.script_path` (`extra_steps` carry absolute paths). Ids are stable
  and persisted (`Job.step_id`) — they keep their historical numeric prefixes
  (`9_pass2_rct`, `13_build_bundle`); never rename one. `Step.num` is a gappy,
  shared legacy selector (3 and 8 are both `classify.py`; 16 is the taxonomy
  steps + `extra:`), NOT run order. Select programmatically by id
  (`select_step_ids`); the CLI uses `pipeline.select_run` (ids or numbers).
- `pipeline.execution_steps` inserts prerequisites before the first selected
  pass-2 step: 5 download, 6 convert, 7 install-structured, then 10 pass 1 +
  11 `stage_eligible --clean` (omitted when config `staging: manual`, which
  protects hand-curated buckets). Pass 2 binds `--input <bucket>` and hard-errors
  if it's missing — never reintroduce a whole-corpus fallback.
- `run_pipeline.py` stops on non-zero exit AND when a step exits 0 without its
  `produces` (`ber_core.execution.verify_produces`). `--list` prints the run order.
- `run_pipeline.py` reads `--no-observational` / `--meta-analysis` from FLAGS
  (falling back to the config); the studio (`pipeline_bridge.canonical_steps_for`)
  reads `include_observational` / `generate_meta_analysis` from the CONFIG.
- **`extra_steps:`** (config) adds topic scripts at step 16 as `extra:<id>`;
  `script` is relative to the config file; `--config` is passed first. The
  studio's Extract stage claims the `extra:` prefix. **`prompts_dir:`** points
  prompt overrides beside the config (`BER_PROMPTS_DIR`).
- `topic.indication_focus: treatment|prevention|all` routes the export; the other
  kind goes to `<kind>_trials.jsonl`.
- Statistics: `docs/scientific-validation.md`. Pooling is `meta_analysis`;
  GRADE / risk of bias / poolability is `grading`. One design-class definition
  (`scientific.design_label`), one failed-status set (`constants.FAILED_STATUS`),
  one DOI normalizer (`identity.canonical_doi` / `join_key`). Don't add parallel
  copies.

## The studio architecture

**Server** (`server/app/`): FastAPI + async SQLAlchemy (SQLite dev, `server/dev.db`)
+ arq/Redis. It never runs pipeline logic in process — it shells out to the
step scripts via `services/runner.py` (`run_subprocess`), passing each review's
workspace through `BER_PROJECTS_DIR` and `BER_MANAGED_RUN=1` (managed children
skip `.env.local` and ignore user-supplied `dirs.*` inputs).
`services/pipeline_bridge.py` is the main importer of the pipeline
(`canonical_steps_for`, `build_project`, `preflight`, `paper_index`); services
reach `ber_core` through `services/_pipeline_path.py`.

**Two arq queues / two worker processes** (`app/worker.py`):
- `WorkerSettings` → `ber:default`, `max_jobs=1`: all LLM work (pipeline steps,
  the `fulltext_screen` eligibility job, synthesis). Serialized because it may run
  on one shared host CLI login.
- `PdfWorkerSettings` → `ber:pdf`, separate process: non-LLM `pdf`
  (download + convert) and `metadata` jobs, concurrent with LLM work.

Background kinds (`metadata`, `pdf`, `fulltext_screen`) are exempt from the
one-job-per-review guard (`services/jobs.py::active_job`). Enqueue through the one
`jobs.enqueue(kind, …)`. A held workspace or lease DEFERS the job (arq Retry,
`WORKSPACE_BUSY_DEFER_S` / `LLM_LEASE_DEFER_S`); failed jobs are never
auto-retried. `services/reconcile.py` (a loop in the API process, every 15 s)
re-dispatches lost queued rows, fails orphaned ones, and enqueues background work
for reviews with a `review_automation` row (enrolled when the user first starts a
job). The browser does NOT drive background work.

**Frontend** (`studio/src/`): `lib/api/` is the typed client (one module per
domain, all through `client.ts`; import from `@/lib/api`); `lib/types.ts` mirrors
`server/app/schemas.py` (guarded by `server/tests/test_contract_drift.py` —
update both together). Evidence table + dock: `components/EvidenceTable.tsx`,
`components/EvidenceDock.tsx`; paper drawer pieces in `components/paper-detail/`.

**The browser never calls the API cross-origin.** `/api` is rewritten by
`studio/next.config.mjs` to `BER_API_ORIGIN` (`127.0.0.1:8000`). So CORS is not
exercised, the API binds loopback, and one Cloudflare Access app covers
everything. `NEXT_PUBLIC_API_BASE` is an escape hatch that opts back into
cross-origin (and CORS). `next.config.mjs` also sets the CSP.

## Auth & access control

- **Sessions**: argon2 + stateless HS256 JWT (`security.py`), Bearer only (token
  in `localStorage`, no cookies → no CSRF). JWTs must carry `exp` and `sub`.
- **Ownership**: shared dependencies `deps.owned_review` / `deps.owned_job`;
  admin routes use `deps.get_current_admin`. Responses carry no absolute paths;
  other users' emails/review ids are masked for non-admins.
- **Registration**: per-person codes in `invite_codes` (`services/invites.py`,
  `/admin/invites`, `server/scripts/manage_access.py mint|list|revoke|make-admin`).
  `settings.SIGNUP_CODE` is the legacy shared fallback.
- **Revocation is immediate** despite stateless JWTs: `deps.get_current_user`
  loads the user every request and rechecks their invite AND the `user_disabled`
  table (`manage_access.py disable|enable`, admin endpoints). Users with no
  invite row (operator, seeded admins, test fixtures) are unaffected by the
  invite check.
- **`PUBLIC_MODE`** (`config.py`) = "strangers can reach this": drops `/docs` +
  `/openapi.json`, masks error text (`errors.public_detail`), password floor 12,
  refuses `BER_ENGINE_FAKE_STEPS`, signup fails closed, applies rate-limit/TTL
  defaults, `TRUST_PROXY_HEADERS` on, refuses host LLM logins for non-admins
  (`PUBLIC_REQUIRE_BYO_LLM`), disables custom PDF dirs, refuses
  `ALLOW_CONFIG_EXTRA_STEPS`, and refuses to boot with weak `JWT_SECRET` / empty
  `APP_SECRET`. A non-loopback `BIND_HOST` with the placeholder secret also
  refuses to boot. Pair with `./dev.sh up --public`; see `docs/hosting.md`.
  `server/tests/conftest.py` pins it (and limits, signup gate) to local values.
- **Operator-only config keys**: `services/configuration.py` (THE config write
  path) refuses `extra_steps`, `prompts_dir` and every `dirs.*` except
  `base_pdf_dir` from the API. Workspace configs get `extra_steps` stripped unless
  `ALLOW_CONFIG_EXTRA_STEPS`.
- **`dirs.base_pdf_dir`** is the only user-supplied path the server writes to.
  `pdf_dir.validate_pdf_dir` at write time (`configuration._validate_config`, PUT
  and PATCH) and `pdf_dir.resolve_for_workspace` at read time (degrades rather
  than raising). Rejects system roots, credential dot-dirs, `DATA_ROOT`,
  `REPO_ROOT`, existing projects, other users' homes. Disabled under `PUBLIC_MODE`.
- **Job logs** are scrubbed of secrets and absolute paths (`services/redact.py`).
- **In-process LLM calls must use `llm_prefs.provider_env`**, never a bare
  `os.environ[...] = key`. It scopes provider + credential in contextvars via
  `claude_cli.llm_context`, applied to the child env at spawn
  (`claude_cli._child_env`). `os.environ` is shared across requests and copied
  into every subprocess by `runner.build_step_env`; mutating it leaked one
  owner's credential to the next (agent turns interleave across `await`s).

## LLM credentials (read before touching provider or env code)

Five providers (`schemas.LLM_PROVIDERS`): `codex_cli` (DEFAULT — host's
`codex login`) and `claude_cli` (host's `claude` login) are host-shared, leased,
billed to the operator (`llm_prefs.HOST_SHARED_PROVIDERS`); `claude_token`
(`claude setup-token` OAuth token), `anthropic_key`, `openrouter` are BYO, billed
to the user, exempt from the lease (`llm_prefs.BYO_PROVIDERS`). Precedence: the
review's `llm_default_provider` (unless `inherit`) > the owner's
`User.llm_provider` > `codex_cli`. `services/llm_access.py` applies the lease and
`PUBLIC_REQUIRE_BYO_LLM` to the review OWNER's provider, at the endpoint and at
job start.

Per-user secrets (incl. the OpenRouter key) live in **`user_credentials`**,
Fernet-encrypted via `crypto.py`, verified by a tiny model call on save
(`llm_lease.verify_credential`; `claude auth status` alone says loggedIn for any
plausible token). The legacy `User.openrouter_key_encrypted` column is only read
as a fallback.

**The trap — credential injection is EXCLUSIVE, not additive.** Claude Code's
precedence: `CLAUDE_CODE_USE_*` (1) > `ANTHROPIC_AUTH_TOKEN` (2) >
`ANTHROPIC_API_KEY` (3) > `apiKeyHelper` (4) > `CLAUDE_CODE_OAUTH_TOKEN` (5) >
profiles (6) > host `/login` (7). A user token is rank 5. `dev.sh` exports the
repo-root `.env.local` into the workers and `build_step_env` copies `os.environ`,
so one `ANTHROPIC_API_KEY` there would silently outrank every user token and bill
the operator. Hence `llm_prefs.credential_env` returns ALL of
`environment.MANAGED_VARS` with `None` = must be unset, and both `provider_env`
and `build_step_env` clear before they set; `environment.enforce_credentials`
re-applies it at the final spawn. `server/tests/test_user_credentials.py` asserts
this against the CHILD env and pins that interleaved callers can't see each
other's credential — don't weaken it.

**Sandbox.** Every `claude` child runs in a private empty dir
(`claude_cli.sandbox_dir`), `--tools ""` (the `read_files` path gets `Read` only,
`--restricted` to its `--add-dir`s, fails closed if the CLI can't confine it),
`--strict-mcp-config`, `--setting-sources ""`, prompt on stdin. Codex calls
(`codex_backend.py`) are stateless and text-only; `haiku/sonnet/opus` map to the
Codex model.

**A dead credential stops the run.** Per-paper workers catch `Exception`, so a
raise would become "this paper failed" for every paper. `claude_cli._latch_auth_failure`
latches, `run_halted()` stops workers, and `raise_if_auth_failed()` (end of
`classify.py` / `extract_pass1.py` / `extract_pass2.py` `main()`) exits 2
(credential) or 3 (usage-guard stop). Auth/usage phrases are matched only in
stderr / error envelopes, never in the model's answer.

## The wizard / stage model (read before touching stages)

`server/app/services/stages.py` is the registry: frozen `StageDef`s grouped into
`PHASES` (Define · Search · Screen · Review · Extract · Meta-analysis · View
dashboard). Invariants (tested in `test_stages.py`):

- **Every canonical step belongs to exactly one stage** (`extra:` ids → `extract`
  via `step_id_prefixes`).
- **`phase` is presentation-only** — gating never reads it. Phases partition the
  stages, in order.
- `hidden=True` → not in the walkthrough (`abstracts`, `extract1`);
  `autorun=True` → client may start it without a click; `requires_approval=False`
  → not a gate (a hidden gate would block everything downstream with no UI).
- `resolve_stages(workspace)` filters `step_ids` to the review's actual steps; a
  stage whose steps all vanish is `skipped`.

Slugs are permanent URLs (`/reviews/<id>/stages/<slug>`) — changing one means
updating `config_schema.py` stage tuples, tests and the client `BODIES` map.
Current slugs: `setup, search, abstracts, classify, triage, extract1, extract,
meta, view-dashboard, report`.

### Screening → extraction flow

`classify` ("Screen": `classify.py quick` + `screen` + enrich) → `triage`
("Review": human looks) → background full-text eligibility (`extract1`, hidden,
step-less; `fulltext_screen` job runs `extract_pass1.py` + `stage_eligible.py`
as full text arrives) → `extract` (pass 2 + `extra:`). PDF fetch/convert and
eligibility are background jobs, not run steps — the wizard never gates on them.

- The abstract screen is one cheap-model pass producing relevance, article type,
  keep/drop, certainty and **organism** (`human | mouse | other_animal | other`;
  pass 1 refines it and wins). Non-human papers are excluded unless
  `include_animal_studies: true` (Screen page checkbox); the policy is applied
  deterministically after the model, so toggling it recolours without a re-run.
- Organism is not in the pass-2 schema; the export JOINS the pass-1 verdict
  (`steps/export_dashboard.py::_load_organism`). Reviews screened before organism
  existed show blank/"—" until re-screened.
- PRISMA "Included in synthesis" counts pass-2 files ONLY.

## The paper store (read before touching any file path)

```
projects/<slug>/papers/            (or dirs.base_pdf_dir)
  Clinical_Trial/10.1000--jama.2021.1234/
      <stem>.pdf  <stem>.xml  body.md  abstract.md  provenance.json  references.json
      supplementary/                   filed by install_structured.py
  _unclassified/                       no (valid) article type yet; parse_error lands here
  _incoming/                           download staging (skipped by build_index)
```

- **`paper_store` is the only path resolver.** FIND with `build_index()` /
  `paper_dir()` (keyed by normalized DOI, type-folder agnostic); PLACE with
  `ensure_paper_dir()` / `stem_for()`. Never construct `papers/<type>/<stem>` to
  look something up: `article_type` is mutable. The server goes through
  `pipeline_bridge.paper_index()`.
- **Always pass the config/root** (`config=` / `root=`, i.e. `dirs.base_pdf_dir`)
  to paper_store entry points; without it the store resolves to
  `<project>/papers` and `eligible_*` links dangle. A configured store is recorded
  in `<project>/.paper-store.json`.
- **Locking**: `refile()` takes `store_lock(exclusive=True)` and skips (retry next
  run) while a download/convert holds it shared.
- **One home per DOI**: `build_index` prefers the folder with body.md/XML and
  warns on duplicates; `refile` merges a second folder into the existing home.
- **Conversion happens in place**; a failure writes `conversion_failed.marker`.
- **Re-screening re-files**: `classify.py quick` ends with `paper_store.refile()`
  (moves changed-type folders, re-points `eligible_*` symlinks). Idempotent.
- **`has_fulltext` ≠ folder exists** — it means body.md, publisher markup, or a
  recorded conversion failure (`services/evidence.py::_mark_fulltext`).
- **Legacy layouts still resolve**: `utils.discover_papers()` reads `typed`,
  `nested` (`converted/<stem>/`) and `flat`. `download_pdfs.py` / `convert_pdfs.py`
  auto-migrate on first touch (`--no-auto-migrate`);
  `scripts/maintenance/migrate_to_paper_store.py` is dry-run by default, moves by
  `rename()` (hardlinks survive), keeps differing collisions as `<name>.dup`,
  never deletes unrecognized files. Trap: legacy converted dirs named
  `pdfs__<stem>` are not valid stems and are left behind — strip the prefix first.
- `eligible_*/<stem>` are usually SYMLINKS into the store (older runs copied —
  handle both); link targets come from `paper_store.bucket_link_target` (depth
  varies by bucket), never hardcoded.

**The evidence row** (`services/evidence.py::build_evidence`, cached on its input
files) is a progressive DOI-keyed merge of every artifact (search → abstract
screen → trial screen → pass 1 → pass 2); a blank later tier never erases a filled
one. `_screen_state` derives ONE verdict (`included | pending | excluded |
screened_out`) shared by pill, tint, sort and PRISMA. Default sort
included→pending→screened_out→excluded, but each row's position is frozen on first
sight so a toggle recolours in place. Manual include (FORCE-include, reaches
extraction even if pass 1 said ineligible) / exclude write `user_inclusions.json` /
`user_exclusions.json` (kept disjoint; honoured by stage_eligible, export, meta).
A corrupt decisions file RAISES (`CorruptDecisionFile` / `CorruptVerdictFile`,
409 on write) — never read as "no decisions".

## Running and testing (details in DEVELOPMENT.md)

- `bash dev.sh up|status|logs <svc>|down`. Don't restart mid-job (it kills
  workers). `--lan` binds 0.0.0.0 (refused with the placeholder `JWT_SECRET`);
  `--public` = hosted posture. dev.sh never installs an LLM CLI.
- Before claiming done: `python3 scripts/check.py` (both suites + typecheck), or
  the individual commands in DEVELOPMENT.md. Both suites run parallel
  (`--dist loadfile` keeps each FILE on one worker — within-module state
  assumptions rely on it); `-n 0` to debug serially. If plugin autoload breaks
  collection: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p xdist`.
  Each server test process gets its own SQLite tmpdir and its own `redis-server`.
  A stale `studio/.next` gives false ENOENT/PageNotFound build errors.
- **Two Python interpreters.** The server venv (`server/.venv`,
  `server/requirements.txt`) runs FastAPI + arq; metadata enrichment runs IN
  PROCESS there. The pipeline interpreter (`runner.pipeline_python()`:
  `PIPELINE_PYTHON`, else the base interpreter the venv was made from) runs the
  step SCRIPTS and needs `pip install -e .` (+ extras). Never assume a script's
  deps are in the server venv.

## API keys (data sources)

Keys live in the git-ignored repo-root **`.env.local`** (template:
`.env.example`), loaded by each script via `environment.load_local_env` —
skipped in studio-managed runs (`BER_MANAGED_RUN=1`), which get the env `dev.sh`
exported. Names that are easy to get wrong:

| var | used by |
|---|---|
| `EMAIL` | **required** for PubMed/Crossref (no default; PubMed runs exit 2 without it); OpenAlex polite pool; fetchpdf/Unpaywall |
| `COREAPIKEY` | **no underscores** (`CORE_API_KEY=` does nothing). Trial keys expire ~monthly; an expired one 401s and fetchpdf disables CORE for the session. CORE 403 `error code: 1010` is Cloudflare rejecting a non-browser User-Agent, not a bad key |
| `OPENALEXAPIKEY` | also no underscores |
| `SEMANTIC_SCHOLAR_API_KEY`, `ENTREZ_EUTILS_API_KEY`, `SCOPUS_API_KEY`, `DIMENSIONS_API_KEY`, `CROSSREF_API_KEY` | search / abstracts / enrichment |
| `PUBPEER_DEVKEY` | INSPECT-SR PubPeer lookups (unavailable without it) |
| `BER_LLM_PROVIDER`, `OPENROUTER_API_KEY`, `BER_USAGE_*` | CLI provider selection, usage guard |

Keys sent as URL query params (OpenAlex, NCBI) are redacted from logs
(`utils.redact_secrets`). `download_pdfs.py` loads this repo's `.env.local`
BEFORE importing fetchpdf (which reads `EMAIL` at import; `load_dotenv` never
overwrites, so first loaded wins).

## PDF download — fetchpdf + the `ber.pdf_sources` hook

`steps/download_pdfs.py` fetches through the `fetchpdf` package (a sibling repo,
editable install; not in any requirements file), imported LAZILY. Extra sources
are separately installed plugins registered under the **`ber.pdf_sources`**
entry-point group: a zero-arg callable run BEFORE fetchpdf is imported (fetchpdf
binds settings at import), optionally returning a banner label; a raising plugin
is reported and skipped. `--no-extra-sources` disables them. The script exits 1
when 0 of n>0 papers yielded anything. Do not hard-import any particular source
package.

## PDF conversion quality — docling must load

`steps/convert_pdfs.py` drives the sibling `pdf4llm` (never `pip install pdf4llm`
from PyPI — unrelated project). The default `--profile content` is Docling
without GROBID; `--profile bibliographic` keeps the GROBID + Docling merge.
**Docling carries TABLES into `body.md`**; without it pdf4llm degrades to
`grobid-only` (no tables) and everything downstream still reports success — this
once cost every table-only outcome across a corpus. Guardrails:
- `docling_guard` probes docling BEFORE converting and exits non-zero.
  `--fix-docling` / `BER_FIX_DOCLING=1` force-reinstalls `numpy pandas pyarrow
  docling` and re-execs AT MOST ONCE (`BER_DOCLING_REEXEC`); `--allow-degraded`
  is the escape hatch. Probing catches `BaseException` (an ABI break can raise
  bare `SystemError`).
- `conversion.TABLE_BEARING_MODES` is the ONE set of table-carrying
  `extraction_mode`s; `convert_pdfs.py`, `redo_degraded_conversions.py` and the
  server import it. `conversion.conversion_quality` also checks partial/failed
  status and quality warnings — a table-bearing engine does not prove complete
  output. Evidence rows carry `conversion_mode` / `conversion_degraded` /
  `tables_captured` from each folder's `provenance.json`.
- **Tables are emitted as HTML**, not pipe tables (pipe tables can't express
  `rowspan`/`colspan`, silently reassigning values to the wrong arm). Table
  counting must look for both forms (`html_convert.count_tables()`), and
  `utils.is_garbled` strips markup/links/URLs before its run-on-word test.
- Pass 2 reads complete tables with captions/notes plus canonical supplements
  (native DOCX/XLSX supported); `--max-input-batches` defaults to six per source
  attempt and overflow is recorded for review. `--input-packaging legacy` is a
  benchmark control only.
- Repair: `scripts/maintenance/redo_degraded_conversions.py` (scratch store,
  verifies tables, archives old evidence under `conversion_history/`, dry run
  unless `--execute`). Benchmarking converters: `docs/conversion-benchmark.md`.
- `convert_pdfs.py --html-only` converts `.fulltext.html`-only papers via docling;
  it skips papers with XML **or a PDF**, so it never preempts the better route.

## Conventions & gotchas

- **`--config` is required everywhere**; start from `configs/example.yaml`.
  There is no `config.yaml` and no `api_keys:` block.
- **Studio config lives BOTH** on disk (`config.yaml`, read by the pipeline) and
  in the DB (`ReviewConfig.config_json`, read by the UI); writes go through
  `services/configuration.py`, which re-renders the file. Sync both if you edit
  one by hand.
- **No Alembic** — `create_all` makes tables, never columns. New state → new
  tables; the rare column on an existing table goes in
  `db/session.py::_ADDITIVE_COLUMNS` (applied on SQLite only).
- **DOI filenames**: `/` → `--` (`utils.doi_to_filename`); join with
  `identity.canonical_doi` / `join_key`. Rows keyed by `norm_doi` /
  `norm_paper_id`.
- **Atomic writes** for anything resumable (`storage.atomic_text` /
  `atomic_write_json`, `utils.write_csv_atomic`); `utils.read_jsonl(on_error=…)`.
- **Explicit `encoding="utf-8"`** on every text open.
- **Commits**: branch before committing on `main`; commit/push only when asked;
  end messages with a `Co-Authored-By:` trailer for the agent.
