# Development guide

For the project's developers. This repository is source-available, not open
source: it is published for reference under an all-rights-reserved
[LICENSE](LICENSE), and outside contributions are accepted only by prior
arrangement with the copyright holder.

This page covers the development setup, the checks to run before merging a
change, and the project conventions. For how the system fits
together, read [docs/architecture.md](docs/architecture.md) first; for the
statistics rules, [docs/scientific-validation.md](docs/scientific-validation.md).

## Development setup

The repo has **two Python environments**, and mixing them up is the most common
setup problem:

| Environment | What runs in it | Install |
|---|---|---|
| Pipeline interpreter (e.g. a repo-root `.venv`) | the step scripts in `steps/`, `scripts/`, `run_pipeline.py`, the `tests/` suite | `pip install -e '.[test]'` plus extras (`download`, `conversion`, `bibtex`) |
| Server venv (`server/.venv`) | the FastAPI API and the arq workers, the `server/tests/` suite | `server/.venv/bin/pip install -r server/requirements.txt` (it imports `ber_core` from the checkout) |

The server runs pipeline steps as subprocesses with the pipeline interpreter
(`PIPELINE_PYTHON`, else the base interpreter the server venv was created from),
so a step's dependencies must be installed there, not only in the server venv.

The frontend needs Node 22 (`.nvmrc`) and pnpm (`corepack enable pnpm`), then
`pnpm install` at the repo root. `bash dev.sh up` sets up `server/.venv` and the
pnpm workspace on first run and starts everything (Redis must be available).

## Checks before merging

One command runs both test suites and the frontend typecheck:

```bash
python3 scripts/check.py
```

Or individually:

```bash
python3 -m pytest -q                                  # pipeline suite (tests/)
python3 -m pyflakes run_pipeline.py steps ber_core scripts tests server/app server/tests server/scripts  # lint
cd server && .venv/bin/python -m pytest -q            # server suite (needs redis-server on PATH)
pnpm -r typecheck                                     # studio + dashboard-ui
cd studio && rm -rf .next && npx next build           # a stale .next gives false ENOENT errors
```

Both suites run in parallel with pytest-xdist (`--dist loadfile`, so each test
file stays on one worker). Use `-n 0` to debug serially. If a broken third-party
pytest plugin breaks collection, disable autoload:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p xdist`. Tests that need
the optional sibling packages (fetchpdf, pdf4llm, docling) skip when they are
absent, and `-rs` lists every skip. CI (`.github/workflows/ci.yml`) runs the same
three jobs.

## Where code goes

- **A pipeline step** is a script in `steps/` that takes `--config`, is
  resumable, and is registered in `ber_core/pipeline.py::canonical_steps` with a
  stable id and its `produces` artifacts. The studio maps every step to exactly
  one wizard stage (`server/app/services/stages.py`); `server/tests/test_stages.py`
  enforces it.
- **Shared logic** goes in `ber_core/` and is imported as
  `from ber_core import x`. Don't add modules at the repo root.
- **A one-off or standalone tool** goes in `scripts/` (repair tools in
  `scripts/maintenance/`, dry run by default with an `--execute` flag).
- **Topic-specific logic** for a single review belongs in that review's own
  `extra_steps:` scripts and `prompts_dir:` (see the README), not in the shared
  pipeline.

## Conventions

- Every script requires `--config`; there is no default config.
- Write resumable outputs atomically (`ber_core.storage.atomic_text` /
  `atomic_write_json`, `ber_core.utils.write_csv_atomic`), and open text files
  with an explicit `encoding="utf-8"`.
- Find papers through `ber_core.paper_store` (`build_index`, `paper_dir`); never
  build `papers/<type>/<stem>` paths by hand.
- One definition per rule: design classes (`ber_core.scientific.design_label`),
  failed statuses (`ber_core.constants.FAILED_STATUS`), DOI normalization
  (`ber_core.identity.canonical_doi`). Extend these rather than adding copies.
- The database has no migrations: startup creates missing **tables** only. Put
  new state in new tables; a column added to an existing table must be listed in
  `server/app/db/session.py::_ADDITIVE_COLUMNS`.
- `studio/src/lib/types.ts` mirrors `server/app/schemas.py`; change both
  together (`server/tests/test_contract_drift.py` checks them).
- Never put credentials in `os.environ` from server code; see the credential
  section of [CLAUDE.md](CLAUDE.md) for why and what to use instead.

## Branches and commits

Work on a branch and open a pull request against `main`. Keep commits focused,
and describe *why* a change was made, not only what changed. Include tests for
behaviour changes; a bug fix should come with a test that fails without it.

## Reporting security issues

Don't open a public issue for a vulnerability; see [SECURITY.md](SECURITY.md).
