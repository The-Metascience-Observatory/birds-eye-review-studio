# Bird's Eye Review

An AI-assisted systematic-review toolkit for clinical questions. It searches the
literature at scale (100K+ records), screens abstracts and full texts with an LLM,
extracts structured trial data for meta-analysis, pools it, and builds a
dashboard bundle for the review:

```
search → abstracts → abstract screen → full text (download + convert)
       → full-text eligibility → extraction (RCT + observational)
       → canonicalize / classify → meta-analysis → dashboard bundle
       → optional INSPECT-SR manuscript
```

There are two front doors over one pipeline:

- **CLI pipeline**: the step scripts in `steps/`, driven end to end by
  `run_pipeline.py`. Every step is resumable and skips work already done.
- **Studio**: a FastAPI + Next.js web app that runs the same pipeline as a
  guided wizard, with a job queue, a live evidence table, per-stage approval,
  and a two-party publish flow.

Both build their work from `ber_core/pipeline.py::canonical_steps`, so they
cannot drift.

> **Status:** research software under active development. Interfaces and output
> formats may change between versions. LLM-extracted data must be checked by a
> human before it informs any clinical or policy decision.

## What's in this repo

```
run_pipeline.py      CLI driver: runs the steps for one review config
steps/               the pipeline steps, one script each (search_papers.py, classify.py,
                     extract_pass2.py, export_dashboard.py, …)
ber_core/            the shared library every step and the studio server import
                     (paper store, LLM backends, statistics, PRISMA, evidence rows, …)
prompts/             LLM prompt templates; prompts/<slug>/ holds per-review overrides (git-ignored)
configs/             example.yaml (annotated template); your review configs go here (git-ignored)
scripts/             standalone tools: QC report, pooling report, conversion benchmark,
                     INSPECT-SR helpers; maintenance/ repair tools
tests/               pipeline test suite
server/              studio backend (FastAPI, arq job workers)
studio/              studio frontend (Next.js)
packages/dashboard-ui/  React components shared by the studio and the public dashboards
docs/                architecture, scientific validation, hosting, WSL setup, INSPECT-SR
deploy/              example Cloudflare Tunnel config for hosting the studio
dev.sh               start / stop the whole studio stack locally
projects/            per-review output of CLI runs (git-ignored; can reach several GB)
```

## Prerequisites

- **Python ≥ 3.11** (CI uses 3.12).
- **An LLM provider** (see [LLM providers](#llm-providers)). The default is the
  Codex CLI logged in on the machine (`codex login`).
- For the studio: **Node 22** (`.nvmrc`) with **pnpm** (pinned via
  `packageManager`; `corepack enable pnpm`), and **Redis** (job queue and rate
  limits).
- For full-text work (optional):
  [`fetchpdf`](https://github.com/The-Metascience-Observatory/fetchpdf) for PDF
  download and [`pdf4llm`](https://github.com/The-Metascience-Observatory/pdf4llm)
  for PDF → Markdown conversion. Conversion uses **Docling** (which carries the
  tables) by default; **GROBID** is only needed for the optional
  `--profile bibliographic` conversion. Everything up to and including the
  abstract screen runs without them.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'                    # core libraries + test tools
pip install -e '.[download,conversion]'     # optional: full-text extras (lxml, docling, PyMuPDF)
pip install -e '.[bibtex]'                  # optional: Crossref BibTeX fallback

# Optional sibling packages (not on PyPI under these names — install from source):
pip install git+https://github.com/The-Metascience-Observatory/fetchpdf
pip install git+https://github.com/The-Metascience-Observatory/pdf4llm
```

Do **not** `pip install pdf4llm` from PyPI: that name belongs to an unrelated
project. PyMuPDF is AGPL-licensed and optional; without it, papers with no
converted text get no text (with one loud warning).

The step scripts are not packaged; run them from a checkout. `pip install -e .`
installs the shared `ber_core` package and the dependencies. Extras: `download`,
`conversion`, `bibtex`, `studio`, `test`.

## Configure

1. **Environment.** `cp .env.example .env.local` and fill in what you use. Only
   `EMAIL` is required (NCBI and Crossref require a contact address; PubMed runs
   refuse to start without it). API keys for OpenAlex, NCBI, Semantic Scholar,
   Scopus, CORE, Dimensions and PubPeer are optional and raise rate limits or
   enable sources. Note the exact names — e.g. `OPENALEXAPIKEY` and `COREAPIKEY`
   have no underscores.
2. **A review config.** `cp configs/example.yaml configs/config_my_topic.yaml` and
   edit it. The example is annotated key by key: `topic` (name, synonyms,
   description, `not_about`, `indication_focus`), `search` (queries per source),
   the vocabularies the models choose from (`article_types`,
   `intervention_categories`, `outcome_domains` — keep them non-empty), `models`,
   scope flags, `staging`, `fulltext`, `meta_analysis` and `dirs`. The project
   slug, and therefore `projects/<slug>/`, is derived from `topic.name`.
   Everything in `configs/` except `example.yaml` is git-ignored, as are
   per-review prompt overrides in `prompts/<slug>/`, so a review's specifics
   stay local unless you choose to commit them.

Every script takes `--config`; there is no default config.

## CLI quickstart

```bash
# Show every step (number, id, script) in run order:
python run_pipeline.py --config configs/config_my_topic.yaml --list

# Preview the exact commands, run nothing:
python run_pipeline.py --config configs/config_my_topic.yaml --dry-run

# Run end to end (asks for confirmation; --yes to skip):
python run_pipeline.py --config configs/config_my_topic.yaml

# Incremental update: only papers indexed since a date (or since the last run):
python run_pipeline.py --config configs/config_my_topic.yaml --since 2026-03-01
python run_pipeline.py --config configs/config_my_topic.yaml --since auto

# Periodic deep refresh (re-issue every query; results are deduplicated):
python run_pipeline.py --config configs/config_my_topic.yaml --fresh

# Select steps by id or number:
python run_pipeline.py --config configs/config_my_topic.yaml --only 13_build_bundle
python run_pipeline.py --config configs/config_my_topic.yaml --from 12 --to 18
```

Other flags: `--no-observational` (RCT-only topic), `--meta-analysis` (pool
forest plots into `meta_analysis.json` during the export). The run stops at the
first failing step, and also when a step exits 0 without writing its declared
output.

Output lands in `projects/<slug>/`: `data/` (CSVs and JSONL), `abstracts/`,
`papers/` (the per-paper store), `eligible_rct/` + `eligible_observational/`
(pass-2 inputs), and `dashboard/` (the built bundle). Nothing is published by a
run.

### Pipeline steps

The table is in run order. Each step has a stable **id** and a **number**; either
selects it with `--only` / `--from` / `--to`. The numbers are historical
selectors (some are shared, and they are not in run order); prefer ids.
`ber_core/pipeline.py` is authoritative, and `--list` prints the live sequence
for a config.

| # | Id | Script | What it does |
|---|----|--------|--------------|
| 1 | `1_search` | `search_papers.py --parallel` | Search PubMed, OpenAlex (title + abstract queries), Europe PMC and Semantic Scholar; dedupe by DOI / PMID / title → `data/search_dois.csv`. CyberLeninka and LessWrong also run when the config has queries for them. |
| 2 | `2_fetch_abstracts` | `fetch_abstracts.py` | Fetch abstracts (local SciSciNet tier if configured, then PubMed → Europe PMC → Crossref → OpenAlex) into `abstracts/`. Transient failures are retried on later runs. |
| 3 | `3_classify_quick` | `classify.py quick` | One cheap-model pass per abstract: relevance, article type, keep/drop, certainty, organism → `data/classifications.csv`. Re-files the paper store by type. |
| 8 | `3_classify_screen` | `classify.py screen` | Treatment-trial screen (incl. clinical phase) from full text where available, else the abstract → `data/trial_screening.csv`. |
| 9 | `4_enrich_screening` | `enrich_metadata.py` | Bibliographic metadata for the screened papers (`paper_*` columns). |
| 5–7 | `5_download_pdfs`, `6_convert_pdfs`, `7_install_structured` | `download_pdfs.py`, `convert_pdfs.py`, `install_structured.py` | *Prerequisites, inserted before pass 2:* fetch PDFs / publisher XML / HTML into the paper store, convert to `body.md`, file structured text and supplements. |
| 10–11 | `8_extract_pass1`, `stage_eligible` | `extract_pass1.py`, `stage_eligible.py` | *Prerequisites before pass 2:* full-text eligibility screen → `data/trial_extractions.jsonl`, then route eligible papers into `eligible_rct/` / `eligible_observational/`. Skipped with `staging: manual`. |
| 12 | `9_pass2_rct` | `extract_pass2.py --design-type rct` | Full quantitative extraction (arms, outcomes, effects, RoB 2) → `data/trial_extractions_pass2.jsonl`. |
| 13 | `9_pass2_observational` | `extract_pass2.py --design-type observational` | Same for observational designs (ROBINS-I) → `data/observational_extractions.jsonl`. Omitted with `--no-observational`. |
| 16 | `12_canonicalize_interventions`, `categorize_interventions`, `classify_intervention_indication`, `extra:<id>` | `canonicalize_interventions.py`, `categorize_interventions.py`, `classify_intervention_indication.py` | Canonical intervention names, re-categorize catch-all interventions, label treatment / prevention. Config-declared `extra_steps` run here. |
| 17 | `13_build_bundle` | `export_dashboard.py` | Union RCT + observational records, validate, apply exclusions and indication routing, optionally pool (`--meta-analysis`) → `projects/<slug>/dashboard/`. |
| 18 | `16_generate_dashboard` | `generate_dashboard.py` | Decide which dashboard panels the data supports → `dashboard.manifest.json`. |
| 19 | `17_systematic_review` | `systematic_review.py` | Optional (`systematic_review: {enabled: true}`): INSPECT-SR trustworthiness assessment and a source-linked manuscript. See [docs/inspect_sr](docs/inspect_sr/README.md). |

The prerequisites (5–7, 10–11) are added automatically when a pass-2 step is
selected, so `--only 13_build_bundle` re-exports without acquiring or extracting
anything, while `--only 6_convert_pdfs` runs just the conversion.

`steps/classify.py` has further subcommands for optional labels: `relevance`,
`type`, `drug_class` (category + named drug, vocabulary from
`drug_class_categories`), `study_design`, `indication` and `escalate`.
`spray_type` is the same category-plus-agent labeller with no shipped prompt: a
review that uses it supplies `prompts/<slug>/classify_spray_type.txt`.

**Standalone tools** (not part of a run), in `scripts/`:

| Script | What it does |
|---|---|
| `qc_report.py` | QC metrics and affiliation report for a project |
| `pool_meta_analysis.py` | Standalone pooling report over the same record set as the export |
| `classify_trial_verdicts.py` | Result direction per trial (`trial_verdicts.csv`, copied into the bundle when present) |
| `benchmark_conversion.py`, `import_supplements.py`, `mineru_standard_adapter.py` | Conversion benchmark and supplement import; see [docs/conversion-benchmark.md](docs/conversion-benchmark.md) |
| `download_inspect_sr.py`, `export_inspect_dashboard.py` | INSPECT-SR materials and dashboard export |
| `check.py` | Both test suites plus the frontend typecheck |
| `maintenance/` | Repair tools, dry run by default: `redo_degraded_conversions.py`, `migrate_to_paper_store.py`, `cleanup_provenance_dups.py`, `quarantine_pass2.py` |

`steps/synthesize_review.py` writes a narrative review from a built bundle; the
studio runs it as its "report" stage.

### Topic-specific steps and prompts

A config can declare its own derivation scripts, which run at step 16 (after the
taxonomy steps, before the export) with ids `extra:<id>`:

```yaml
extra_steps:
  - id: dose_normalization
    script: scripts/normalize_dose.py   # relative to the config file
    label: "Normalize doses to mg"
    args: []                            # --config <cfg> is always passed first
prompts_dir: prompts_my_topic           # prompt overrides, relative to the config
```

Prompt templates live in `prompts/` (`{placeholder}`s are filled from the
config); per-project overrides go in `prompts/<slug>/` or a config's
`prompts_dir`. The studio never accepts `extra_steps` or `prompts_dir` from its
API (see [docs/hosting.md](docs/hosting.md)).

### Optional PDF sources

`steps/download_pdfs.py` fetches through fetchpdf's built-in source chain.
Separately installed plugins can add sources by registering a zero-argument
installer under the `ber.pdf_sources` entry-point group; it runs before fetchpdf
is imported and may return a label for the run banner. `--no-extra-sources`
disables all plugins.

## LLM providers

`BER_LLM_PROVIDER` selects the backend for CLI runs (the studio stores the choice
per account and per review):

| Provider | Credential | Notes |
|---|---|---|
| `codex_cli` (default) | the machine's `codex login` | Model aliases `haiku`/`sonnet`/`opus` map to the Codex model. Text only (no direct PDF reading). |
| `claude_cli` | the machine's `claude` login | |
| `claude_token` | `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) | Your own Claude subscription. |
| `anthropic_key` | `ANTHROPIC_API_KEY` | Billed per token. |
| `openrouter` | `OPENROUTER_API_KEY` | Billed per token. |

Every Claude CLI call runs sandboxed: in a private empty working directory, with
no tools (the PDF-reading path gets `Read` only, confined to the directories it
is given), no MCP servers or user settings, and the prompt on stdin. A rejected
credential stops the run with a non-zero exit instead of marking every paper
failed.

## Studio quickstart

```bash
bash dev.sh up        # Redis check, API, both job workers, studio
bash dev.sh status
bash dev.sh logs api  # api | worker | pdfworker | studio
bash dev.sh down
```

Open <http://localhost:3002> (API on `:8000`). Everything binds `127.0.0.1` by
default; `--lan` opts in to binding all interfaces (refused while `JWT_SECRET` is
the placeholder). `dev.sh` creates `server/.venv` and runs `pnpm install` itself;
it installs *system* packages (Redis, Node, python3-venv) only with
`--install-deps` or `BER_AUTO_INSTALL=1`. The API runs pipeline scripts with the
base Python interpreter (or `PIPELINE_PYTHON`), so install the pipeline there
too. Before letting anyone else reach a studio, read
[docs/hosting.md](docs/hosting.md).

## Development

```bash
python3 scripts/check.py      # both test suites + frontend typecheck
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for setup, the individual test commands
and project conventions.

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pipeline, paper store,
  studio, job engine and credential model fit together, with a `ber_core` module map.
- [docs/scientific-validation.md](docs/scientific-validation.md) — estimate
  selection, pooling identity, design classes, GRADE, PRISMA.
- [docs/hosting.md](docs/hosting.md) — running the studio for other people.
- [docs/setup-wsl.md](docs/setup-wsl.md) — Windows (WSL2) setup.
- [docs/inspect_sr/README.md](docs/inspect_sr/README.md) — the INSPECT-SR step.
- [docs/conversion-benchmark.md](docs/conversion-benchmark.md) — comparing PDF converters.
- [server/README.md](server/README.md), [studio/README.md](studio/README.md).
- [DEVELOPMENT.md](DEVELOPMENT.md); [CLAUDE.md](CLAUDE.md) — dense guide for
  coding agents; [CHANGELOG.md](CHANGELOG.md).

## License

Copyright © 2026 Dan Elton. All rights reserved — see [LICENSE](LICENSE). This
is source-available software, not open source: the code is published for
reference only, and no license to use, copy, modify or redistribute it is
granted except in writing by the copyright holder.
Third-party material and data sources are listed in
[THIRD_PARTY.md](THIRD_PARTY.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).
