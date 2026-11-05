# Hosting the studio

How the Bird's Eye Review studio behaves when people other than you can reach it,
and what to configure first. For a single-user local install, `bash dev.sh up` is
all you need (see the [README](../README.md#studio-quickstart)).

## The trust model — read this first

Every pipeline step runs as the server's OS user, with the server's environment
(including any data-source API keys from the repo-root `.env.local`), and with no
container or filesystem isolation between reviews. The hardening below —
ownership checks, path validation, operator-only config keys, sandboxed LLM calls,
redacted logs — narrows what an account can reach, but an account on a studio is
still a lot of trust. The intended deployment is a **small set of people you vouch
for**, with everyone else unable to reach the app at all: an access-control proxy
(e.g. Cloudflare Access) as the perimeter, and per-person invite codes as identity
inside it.

Keep `server/.env`, the repo-root `.env.local`, the database (`server/dev.db` or
Postgres data) and `server/_data/` out of any synced or shared folder.

## AI credentials: whose account pays

Five providers are selectable in **Settings → AI provider** (and per review):

| Provider | What the user supplies | Billed to |
|---|---|---|
| `codex_cli` — this machine's Codex login (default) | nothing | **the operator** (shared; leased) |
| `claude_cli` — this machine's Claude login | nothing | **the operator** (shared; leased) |
| `claude_token` — your own Claude subscription | output of `claude setup-token` | the user's Claude plan |
| `anthropic_key` — your own Anthropic API key | a Console key | the user, per token |
| `openrouter` | an OpenRouter key | the user, per token |

The host CLIs have no per-request credential: they authenticate as whichever
account is logged in for the server's OS user. So by default every AI step on the
host — screening, extraction, drafting, the review assistant — runs on that one
account.

### Bring-your-own credentials (recommended)

The three BYO providers bill the user, are exempt from the lease below, and never
queue behind anyone else's lease. `claude setup-token`, run on the user's own
machine, prints a one-year OAuth token for their Pro/Max/Team/Enterprise plan.
They paste it into Settings; the studio stores it Fernet-encrypted (key derived
from `APP_SECRET`) and injects it as `CLAUDE_CODE_OAUTH_TOKEN` for their jobs.
Credentials are **verified when saved** with a tiny model call, so a bad paste is
an error in the form, not a failed run an hour later. Settings warns as a token
approaches expiry.

If a credential stops working mid-run, the run **stops** with the CLI's own
explanation in the job log. It never falls back to the host's account, and it is
not recorded as a per-paper failure for every remaining paper.

> **Never put `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`, or a
> `CLAUDE_CODE_USE_*` flag) in the repo-root `.env.local` of a studio host.**
> `dev.sh` exports that file into the API and workers, and those variables
> outrank a user's `CLAUDE_CODE_OAUTH_TOKEN`. The studio clears every credential
> variable before setting the selected provider's (see
> [architecture.md](architecture.md#llm-providers-and-credentials) and
> `server/tests/test_user_credentials.py`); do not undo that.

### Host logins and the lease

Users on `codex_cli` or `claude_cli` share one account, so the studio serializes
them:

- **Settings** shows which account the host CLI is signed in as and who holds it.
- **Claim the CLI** reserves it. While you hold it, other users' AI work is
  refused (or deferred, for queued jobs) with a message naming you — never
  silently billed to your account.
- **Release** hands it back. A lease idle for 30 minutes (`LEASE_TTL_MIN` in
  `server/app/services/llm_lease.py`) can be taken over.

Only work that calls the shared CLI is gated; search, PDF download/conversion,
export and browsing are never blocked. The lease is decided on the review
**owner's** provider, at the endpoint and again when a job starts.

The lease is a courtesy among colleagues, not a security boundary, and not a
licence review: serving several people from one subscription login may violate
its terms. Under `PUBLIC_MODE`, `PUBLIC_REQUIRE_BYO_LLM` (default on) refuses the
host logins for non-admin review owners altogether.

To switch the host's account (interactive, so not from the web UI):

```bash
codex login                                     # or:
claude auth logout && claude auth login
```

`dev.sh` reports whether the CLI for `BER_LLM_PROVIDER` (default `codex_cli`) is
installed and logged in; it never installs a CLI or starts a login itself. Set
`BER_CLAUDE_BIN` to point the studio at a specific `claude` binary.

## Configure the server

```bash
cp server/.env.example server/.env    # every value is documented inline
```

| Setting | Why |
|---|---|
| `PUBLIC_MODE=true` | Removes `/docs`, `/redoc`, `/openapi.json`; masks unhandled-error text behind a reference; raises the password floor to 12; refuses the test-only `BER_ENGINE_FAKE_STEPS` hook; signup fails closed; refuses host LLM logins for non-admins (`PUBLIC_REQUIRE_BYO_LLM`); disables custom PDF folders; refuses `ALLOW_CONFIG_EXTRA_STEPS`; and **refuses to start** with a weak `JWT_SECRET` or empty `APP_SECRET`. `dev.sh up --public` exports it. |
| `JWT_SECRET` | Required under `PUBLIC_MODE` (`openssl rand -hex 32`, ≥ 32 characters). The documented default lets anyone forge an admin token. Also required for `--lan`. |
| `APP_SECRET` | Required under `PUBLIC_MODE`. Encrypts stored credentials. **Set it before anyone saves a key** — rotating it later makes stored keys undecryptable. |
| `ACCESS_TTL_MIN` | `PUBLIC_MODE` shortens the 30-day dev default to 1 day unless set. |
| `RATELIMIT_LOGIN` / `RATELIMIT_REGISTER` | Per-IP limits on `/auth/*`; `PUBLIC_MODE` fills in `10/minute;100/hour` and `10/hour` if empty. |
| `RATELIMIT_LLM` / `RATELIMIT_CREDENTIAL` | Per-user limits on endpoints that spend model calls and on saving a credential; `PUBLIC_MODE` fills in `20/minute;300/hour` and `5/minute;20/hour`. |
| `TRUST_PROXY_HEADERS` | Use `CF-Connecting-IP` as the client address for rate limiting. On by default under `PUBLIC_MODE`; only correct when every request arrives through the tunnel. |
| `CORS_ORIGIN_REGEX` | `PUBLIC_MODE` drops the LAN default. CORS barely matters: the browser calls the API same-origin. |
| `BIND_HOST` | Where the API listens (exported by `dev.sh`). A non-loopback bind with the placeholder `JWT_SECRET` refuses to start. |
| `MAX_UPLOAD_MB`, `UPLOAD_MAX_FILES_PER_USER`, `UPLOAD_MAX_TOTAL_MB_PER_USER` | Per-file and per-account caps on hand-uploaded PDFs. |
| `SSE_PING_SECONDS`, `SSE_MAX_SECONDS` | Keepalive interval and maximum lifetime of job-log streams. |
| `LLM_LEASE_DEFER_S`, `WORKSPACE_BUSY_DEFER_S` | How long a queued job waits before rechecking a held lease or a busy workspace. |
| `ALLOW_CONFIG_EXTRA_STEPS` | Off by default. Lets a hand-edited workspace config run `extra_steps` scripts as the server user; never allowed under `PUBLIC_MODE`. The API never accepts the key from a client. |
| `WEBSITE_DIR` | A website checkout to publish into. Unset by default: the instance cannot publish. |

## Start in public posture

```bash
bash dev.sh up --public
```

- Both servers bind `127.0.0.1` (as they do by default; `--lan` is refused
  alongside `--public`). The only way in is the tunnel.
- The studio is a production build (`next build && next start`), not
  `next dev`, whose error overlays include source.
- uvicorn gets `--proxy-headers`; `PUBLIC_MODE=true` and
  `TRUST_PROXY_HEADERS=true` are exported. Plain `up` refuses to start when
  `server/.env` already has `PUBLIC_MODE=true`.

If you run Redis from `docker-compose.yml`, it publishes on `127.0.0.1` only and
requires a password (`REDIS_PASSWORD`, default `bird-local-dev`); set
`REDIS_URL=redis://:<password>@localhost:6379/0` in `server/.env`.

## The tunnel

`deploy/cloudflared/` has a config template and a systemd unit. Point **one**
hostname at the studio (`http://127.0.0.1:3002`). The browser calls the
same-origin path `/api`, which `studio/next.config.mjs` proxies to the API, so the
API needs no public hostname and one Access application covers the whole app. Do
not tunnel the API separately: a second Access application answers cross-origin
XHR with a login redirect and breaks every request. (`NEXT_PUBLIC_API_BASE` exists
only as an escape hatch for pointing the browser at an API on another origin.)

Put a Cloudflare Access application over the hostname (policy: allow an explicit
list of emails; identity: one-time PIN), so unauthenticated traffic never reaches
the host. A WAF rate-limiting rule on `/api/auth/*` is a useful extra layer.

## Admitting people

Each invite code admits one person, records who used it, and can be revoked on
its own:

```bash
PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py mint --label "Reviewer, Example Lab"
PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py list
PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py revoke <id>
PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py make-admin you@example.com
PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py disable someone@example.com
PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py enable someone@example.com
```

A code is printed once and never stored. Admins can do the same at
`/admin/invites`. To add someone: add their email to the Access policy and send
them a code. To cut someone off: revoke their code (or `disable` an account that
never had one) and remove them from the Access policy. Their session ends on the
next request, because every request rechecks the invite and the `user_disabled`
table.

## What remains true

- A user who can create a review can run pipeline steps as the server user.
  Give codes accordingly.
- Sessions are kept in `localStorage`, readable by any script on the page. The app
  has no raw-HTML sink and sets a CSP (`studio/next.config.mjs`); keep it that way
  (e.g. keep `rehype-raw` out of `ReactMarkdown`).
- Publishing writes only into `WEBSITE_DIR`, and only after an admin approves.
