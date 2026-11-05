# Studio backend (`server/`)

FastAPI + SQLAlchemy 2.0 (async) backend for the Bird's Eye Review studio.

It handles auth and invites, per-review workspaces, an arq/Redis job engine that
runs the pipeline step scripts as subprocesses, the live evidence table, per-stage
approval gates, LLM provider selection (five providers: the host's shared Codex
or Claude CLI login behind a single-occupancy lease, or a user's own Claude
token, Anthropic key or OpenRouter key), and the two-party publish flow.

[`docs/architecture.md`](../docs/architecture.md) explains how the pieces fit;
[`docs/hosting.md`](../docs/hosting.md) covers running it for other people. This
file covers setup and the module layout.

## Quick start

```bash
# from the repo root
python3 -m venv server/.venv
server/.venv/bin/pip install -r server/requirements.txt

# the API (the SQLite dev DB is created on first start)
server/.venv/bin/uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000

# the two workers (separate processes)
server/.venv/bin/python -m arq server.app.worker.WorkerSettings      # ber:default — LLM work
server/.venv/bin/python -m arq server.app.worker.PdfWorkerSettings   # ber:pdf — PDF + metadata
```

Redis must be running (job queue and rate-limit counters). `bash dev.sh up` starts
the API, both workers and the studio together. Pipeline scripts run on the base
interpreter the venv was created from, or on `PIPELINE_PYTHON`; install the
pipeline there (`pip install -e .` from the repo root, plus any extras).

`GET /health` → `{"status":"ok","version":"..."}`. Interactive docs at `/docs`
(removed under `PUBLIC_MODE`).

## Configuration

Settings come from the environment and `server/.env`; `server/.env.example`
documents every one, and all have local-dev defaults. The main groups:

- **Persistence** — `DATABASE_URL` (default `sqlite+aiosqlite:///./server/dev.db`,
  relative to the repo root; Postgres via `postgresql+asyncpg://…`). The models
  use only portable column types.
- **Filesystem** — `DATA_ROOT` (per-review workspaces at
  `DATA_ROOT/reviews/<id>/`; deleted reviews move to `DATA_ROOT/_deleted/`),
  `REPO_ROOT` (this repository), `WEBSITE_DIR` (a website checkout to publish
  into; unset by default, so the instance cannot publish), `PIPELINE_PYTHON`,
  `SCISCINET_DIR` / `SCISCINET_WRITE` (optional local metadata tier).
- **Auth** — `JWT_SECRET`, `JWT_ALGO`, `ACCESS_TTL_MIN`, `APP_SECRET` (encrypts
  stored credentials), `SIGNUP_CODE` (legacy shared code; invites are preferred),
  `MIN_PASSWORD_LEN`.
- **Public posture** — `PUBLIC_MODE`, `PUBLIC_REQUIRE_BYO_LLM`, `BIND_HOST`,
  `TRUST_PROXY_HEADERS`, `ALLOW_CONFIG_EXTRA_STEPS`. A `PUBLIC_MODE` process, or a
  non-loopback `BIND_HOST`, refuses to start with placeholder secrets.
- **Limits** — `RATELIMIT_REGISTER`, `RATELIMIT_LOGIN` (per IP),
  `RATELIMIT_LLM`, `RATELIMIT_CREDENTIAL` (per user), `RATELIMIT_STORAGE_URI`,
  `MAX_UPLOAD_MB`, `UPLOAD_MAX_FILES_PER_USER`, `UPLOAD_MAX_TOTAL_MB_PER_USER`.
- **Jobs and streams** — `REDIS_URL`, `LLM_LEASE_DEFER_S`,
  `WORKSPACE_BUSY_DEFER_S`, `SSE_PING_SECONDS`, `SSE_MAX_SECONDS`.
- **HTTP** — `CORS_ORIGINS`, `CORS_ORIGIN_REGEX`. The studio calls the API
  same-origin through its `/api` proxy, so these matter only for other clients.

## Layout

```
server/app/
  main.py            FastAPI factory; /health; startup = create tables + start the reconciler
  config.py          pydantic-settings Settings + the public-posture boot checks
  security.py        argon2 hash/verify + HS256 JWTs
  deps.py            DB session, current user/admin, owned_review / owned_job
  errors.py          what an error may tell the client (PUBLIC_MODE masking)
  ratelimit.py       per-IP (auth) and per-user (LLM, credential) rate limits
  crypto.py          Fernet encryption of stored credentials
  db/                base, async session (+ additive-column shim), models
  schemas.py         Pydantic v2 request/response models (mirrored in studio/src/lib/types.ts)
  worker.py          arq worker settings — two queues (ber:default, ber:pdf)
  routers/           auth, reviews (CRUD, state, stages), evidence, dashboard,
                     publish, review_config (config + prompts), drafts, jobs,
                     agent, admin, llm_session
  services/          a load-bearing subset (see the directory for the rest):
    workspace.py         create a review workspace: config.yaml + projects/<slug>/
    configuration.py     THE config write path (validation, operator-only keys)
    pipeline_bridge.py   the main bridge to the pipeline (ber_core.pipeline, paper_store, projects)
    runner.py            subprocess execution, the shared step loop, log scrubbing
    jobs.py              enqueue, queue routing, the one-job-per-review guard
    reconcile.py         background dispatch + orphan sweep (runs in the API process)
    llm_access.py        may this review spend LLM money now? (lease + BYO policy)
    llm_prefs.py         provider precedence + exclusive credential env
    llm_lease.py         single-occupancy lease on the host CLI login
    stages.py            the stage registry (wizard structure, gating invariants)
    evidence.py          the live per-paper evidence table (progressive DOI merge)
    pdf_dir.py           validation of the one user-supplied write path
    redact.py            secret scrubbing for job logs
    bundle.py            read a built dashboard bundle (read-only)
    publishing.py / publish_deploy.py   two-party publish + atomic deploy
    invites.py           per-person invite codes
server/scripts/
  manage_access.py   mint / list / revoke invites; make-admin; disable / enable accounts
```

### `pipeline_bridge` — the multi-tenant rule

The server serves many reviews, so it never relies on the pipeline's import-time
`BER_PROJECTS_DIR`. Every `Project` is built with an explicit
`project_dir = <workspace>/projects/<slug>`, so state is derived from that
review's own workspace.

## Tests

```bash
cd server && .venv/bin/python -m pytest -q      # parallel (pytest-xdist)
.venv/bin/python -m pytest -q -n 0 tests/test_stages.py   # serial, one file
```

The suite drives the ASGI app in process (httpx `ASGITransport`), on a throwaway
SQLite database and `DATA_ROOT`, and starts its own disposable `redis-server` on
an ephemeral port per test process (it never touches a developer's Redis).
`conftest.py` pins `PUBLIC_MODE`, rate limits and the signup gate to local values
so the suite behaves the same on a hosted machine.

## Schema changes

There is no Alembic. Startup runs `metadata.create_all`, which creates missing
**tables** only; the few columns added to existing tables after release are listed
in `db/session.py::_ADDITIVE_COLUMNS` (applied on SQLite). Put new state in new
tables wherever possible.
