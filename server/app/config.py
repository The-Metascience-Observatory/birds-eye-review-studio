"""Application settings loaded from environment / server/.env.

Uses pydantic-settings so the same code path works for local sqlite dev and a
Postgres deployment. Only ``DATABASE_URL`` needs to change between them.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# server/app/config.py -> parents[1] == server/ , parents[2] == repo root
_SERVER_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SERVER_DIR.parent

# Dev-convenience CORS: any loopback / RFC-1918 origin on any port. PUBLIC_MODE
# disables this exact string (apply_public_defaults) so a forgotten
# CORS_ORIGIN_REGEX= in .env cannot leave the LAN regex in front of the tunnel.
DEV_CORS_ORIGIN_REGEX = (
    r"^https?://(localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3})(:\d+)?$"
)

# Values that have appeared in the repo / docs. PUBLIC_MODE refuses them
# (anyone who has read the source can forge a token).
_INSECURE_JWT_SECRETS = frozenset({
    "dev-insecure-change-me",
    "change-me-in-prod",
})
_MIN_JWT_SECRET_LEN = 32
_DEV_ACCESS_TTL_MIN = 60 * 24 * 30
_PUBLIC_ACCESS_TTL_MIN = 1440
_PUBLIC_RATELIMIT_REGISTER = "10/hour"
_PUBLIC_RATELIMIT_LOGIN = "10/minute;100/hour"
# Per-USER limits on endpoints that spend LLM money or spawn a CLI child.
_PUBLIC_RATELIMIT_LLM = "20/minute;300/hour"
_PUBLIC_RATELIMIT_CREDENTIAL = "5/minute;20/hour"

# Addresses that are only reachable from this machine.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_SERVER_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Persistence -------------------------------------------------------
    # Default: zero-friction local sqlite (relative to repo-root cwd).
    # Postgres path: postgresql+asyncpg://user:pass@localhost:5432/studio
    DATABASE_URL: str = "sqlite+aiosqlite:///./server/dev.db"

    # --- Auth --------------------------------------------------------------
    JWT_SECRET: str = "dev-insecure-change-me"
    JWT_ALGO: str = "HS256"
    # Token lifetime. The default is generous so local dev stays signed in across
    # restarts without re-logging every day; PUBLIC_MODE shortens this to one
    # day unless an explicit value is set (see apply_public_defaults).
    ACCESS_TTL_MIN: int = _DEV_ACCESS_TTL_MIN  # 30 days (dev-convenience default)

    # --- Signup gating -----------------------------------------------------
    # Shared access code required to register. EMPTY = open signup, which is the
    # dev/test default and what the existing test suite relies on (13 test files
    # POST /auth/register with only email+password). A public deploy MUST set
    # this — otherwise anyone who can reach the port gets an account, and every
    # account can spend the host's Claude subscription.
    #
    # Follows this file's empty-string-means-disabled idiom (APP_SECRET,
    # PIPELINE_PYTHON, CORS_ORIGIN_REGEX) rather than a separate bool, so
    # "gating on but no code configured" is unrepresentable.
    SIGNUP_CODE: str = ""

    # --- Rate limiting -----------------------------------------------------
    # Per-IP limits on the auth endpoints. EMPTY = disabled (dev/tests): the
    # suite registers many users in a tight loop and would otherwise trip them.
    # Set both in production. Syntax is slowapi/limits: "10/hour", "5/minute".
    RATELIMIT_REGISTER: str = ""
    RATELIMIT_LOGIN: str = ""
    # Per-USER limits (keyed on the JWT subject, not the IP) for the endpoints
    # that spend LLM money in-process or spawn a model call: agent ask/review,
    # search/taxonomy drafting, the background-job triggers. EMPTY = disabled.
    RATELIMIT_LLM: str = ""
    # Per-user limit on saving a Claude credential in Settings: every save runs
    # a real (if tiny) model call through a CLI child process to verify it.
    RATELIMIT_CREDENTIAL: str = ""
    # Storage for the counters. Empty => derive from REDIS_URL. Redis (not
    # in-memory) because dev.sh runs the API plus two worker processes, so
    # per-process counters would neither be shared nor survive a restart.
    RATELIMIT_STORAGE_URI: str = ""

    # --- Secrets at rest ---------------------------------------------------
    # Master secret for Fernet-encrypting user secrets (e.g. the OpenRouter API
    # key). Set a strong random value in production. Empty => dev fallback: a key
    # is derived from JWT_SECRET so local dev works with no extra config. That
    # fallback is DEV-ONLY — rotating JWT_SECRET would then make stored keys
    # undecryptable, so a real deploy must set APP_SECRET explicitly.
    APP_SECRET: str = ""

    # --- Filesystem --------------------------------------------------------
    # Per-review workspaces live under DATA_ROOT/reviews/{id}/.
    DATA_ROOT: Path = _SERVER_DIR / "_data"
    # Root of the pipeline repo (so pipeline_bridge can import ber_core / pipeline).
    REPO_ROOT: Path = _REPO_ROOT
    # Checkout of the public website repo that serves published dashboards.
    # Publishing copies a review's bundle into WEBSITE_DIR/data/birds_eye_reviews/.
    # UNSET by default: an instance without it simply cannot publish (preflight
    # and approve explain why) — it never guesses a sibling directory to write to.
    WEBSITE_DIR: Path | None = None

    # --- Infra ---------------------------------------------------------------
    # Required: the arq job queue (both workers) and the rate-limit counters.
    REDIS_URL: str = "redis://localhost:6379/0"

    # Interpreter used to run the pipeline SCRIPTS (which need the pipeline's deps
    # — requests, biopython, pandas, …). This is deliberately NOT the server's
    # lean venv. Empty => auto: the base interpreter the server venv was created
    # from (usually /usr/bin/python3). A fresh box needs the pipeline
    # requirements installed on that interpreter.
    PIPELINE_PYTHON: str = ""

    # --- Pipeline extension hooks ---------------------------------------------
    # The pipeline runs config-declared ``extra_steps`` scripts (pipeline.py).
    # Honoring them from a review workspace means "whoever can edit this config
    # can run a script as the server user", so the server STRIPS them from every
    # config it hands the pipeline unless the operator opts in here. The API
    # never accepts the key from a client either way (configuration.py). Refused
    # outright under PUBLIC_MODE.
    ALLOW_CONFIG_EXTRA_STEPS: bool = False

    # --- Local SciSciNet tier (optional) -----------------------------------
    # Directory holding journals.db / abstracts.db / papers_extra.db / mesh.db.
    # When set, metadata + abstract lookups hit local SQLite before the APIs
    # (~82% / ~52% hit rate measured, ~60-100ms vs seconds per paper). Empty =>
    # the tier is off and everything uses the API waterfall exactly as before.
    # Exported to pipeline subprocesses as BER_SCISCINET_DIR.
    SCISCINET_DIR: str = ""
    # Write API results back into those DBs so each paper costs a network fetch
    # at most once. Default OFF; also refused on FUSE filesystems, where SQLite
    # WAL locking is unreliable (the dev external drive). Safe on ext4/NVMe.
    SCISCINET_WRITE: bool = False

    # --- HTTP --------------------------------------------------------------
    # Comma-separated list of allowed CORS origins (studio dev server, etc.).
    # Both loopback spellings are included by default: browsers treat
    # localhost:3000 and 127.0.0.1:3000 as DIFFERENT origins, and a mismatch
    # surfaces in the studio as an opaque network error ("Sign up failed").
    CORS_ORIGINS: str = "http://localhost:3002,http://127.0.0.1:3002"
    # Regex of additionally-allowed origins. Dev default: any port on loopback
    # or a private-LAN address, so browsing the studio via the machine's LAN IP
    # (or Next picking a different port) just works.
    # All three RFC-1918 ranges are covered. 172.16-31.x matters specifically for
    # WSL2, whose Linux-side interface lives in that range; omitting it made the
    # studio unusable when browsed via the WSL IP instead of localhost.
    # Set to "" in production behind the tunnel to allow only CORS_ORIGINS.
    # PUBLIC_MODE treats this exact default as unset and disables it — see
    # apply_public_defaults.
    CORS_ORIGIN_REGEX: str = DEV_CORS_ORIGIN_REGEX

    # --- Public hosting ----------------------------------------------------
    # ON when this instance is reachable from outside the host (e.g. behind a
    # Cloudflare tunnel). One flag rather than several because these all answer
    # the same question — "can a stranger reach this process?" — and splitting
    # them invites a deploy that sets two of the three.
    #
    # Effects:
    #   * /docs, /redoc and /openapi.json are removed (no free API map).
    #   * unhandled errors return an opaque reference instead of the exception
    #     text (see main.py).
    #   * BER_ENGINE_FAKE_STEPS is refused even if the env var is set.
    #   * Boot REFUSES a missing/placeholder JWT_SECRET or empty APP_SECRET.
    #   * Rate limits, a 1-day token TTL, and no LAN CORS regex apply unless
    #     an explicit value was set (apply_public_defaults).
    PUBLIC_MODE: bool = False

    # Trust CF-Connecting-IP / X-Forwarded-For for the rate-limiter's per-client
    # key. OFF by default because a spoofable header must never be trusted on a
    # directly-reachable socket — an attacker would simply vary it to get
    # unlimited login attempts. Turn it ON only when every request genuinely
    # arrives through a proxy you control (the tunnel + the Next rewrite), which
    # is exactly the PUBLIC_MODE deployment. See app/ratelimit.py.
    TRUST_PROXY_HEADERS: bool = False

    # Minimum password length at registration. 0 = no policy, which is the dev
    # and test default (the suite registers with short throwaway passwords).
    # PUBLIC_MODE raises the floor automatically — see
    # ``effective_min_password_len`` — so turning on public hosting cannot leave
    # one-character passwords enabled through an oversight.
    MIN_PASSWORD_LEN: int = 0
    # The floor a public instance gets when MIN_PASSWORD_LEN is left at 0.
    PUBLIC_MIN_PASSWORD_LEN: int = 12

    # Hard cap on an uploaded PDF, in megabytes. The upload endpoint buffers the
    # body, so without a cap one request can exhaust memory.
    MAX_UPLOAD_MB: int = 50
    # Per-USER lifetime caps on hand-uploaded PDFs (count and total size), so one
    # account cannot fill the disk 50 MB at a time.
    UPLOAD_MAX_FILES_PER_USER: int = 2000
    UPLOAD_MAX_TOTAL_MB_PER_USER: int = 10_000

    # The address the API is served on, as the launcher knows it (dev.sh exports
    # it). The app cannot see its own bind address, so this is how it learns it
    # is reachable from other machines — and refuses to start that way with the
    # documented placeholder JWT_SECRET (see public_posture_errors).
    BIND_HOST: str = "127.0.0.1"

    # Under PUBLIC_MODE, refuse the host-shared LLM logins (claude_cli,
    # codex_cli) for non-admin review owners: on a hosted instance they would
    # bill the operator's own subscription. Users bring their own credential
    # (claude_token / anthropic_key / openrouter) instead.
    PUBLIC_REQUIRE_BYO_LLM: bool = True

    # Live log streams (SSE): a keepalive comment every SSE_PING_SECONDS so
    # proxies don't cut an idle stream, and a hard lifetime after which the
    # server closes it (the client reconnects).
    SSE_PING_SECONDS: int = 15
    SSE_MAX_SECONDS: int = 6 * 3600

    # How long a queued LLM job waits before re-checking when the shared CLI is
    # held by someone else, or when another job holds the review's workspace.
    LLM_LEASE_DEFER_S: int = 60
    WORKSPACE_BUSY_DEFER_S: int = 20

    VERSION: str = "0.1.0"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def effective_min_password_len(self) -> int:
        """Password floor actually enforced. An explicit value always wins; a
        public instance that never set one still gets a real policy rather than
        silently inheriting the dev default of "any non-empty string"."""
        if self.MIN_PASSWORD_LEN > 0:
            return self.MIN_PASSWORD_LEN
        return self.PUBLIC_MIN_PASSWORD_LEN if self.PUBLIC_MODE else 0


def public_posture_errors(s: Settings) -> list[str]:
    """Why a PUBLIC_MODE process must not start, or empty if it may.

    Local-dev defaults are fine on a loopback laptop. The same defaults on an
    internet-facing instance mean anyone who has read this file can mint an
    admin token and decrypt stored API keys. Called at API and worker boot so
    a forgotten JWT_SECRET cannot silently ship.
    """
    errors: list[str] = []
    secret = (s.JWT_SECRET or "").strip()
    weak_secret = (
        not secret
        or secret in _INSECURE_JWT_SECRETS
        or len(secret) < _MIN_JWT_SECRET_LEN
    )
    if not s.PUBLIC_MODE:
        # A LAN-reachable dev server with the documented secret is the same
        # forgeable-admin hole as a public one, just with a smaller audience.
        if not is_loopback(s.BIND_HOST) and (not secret or secret in _INSECURE_JWT_SECRETS):
            errors.append(
                f"The API is bound to {s.BIND_HOST} (reachable from other "
                "machines) but JWT_SECRET is the documented placeholder, so "
                "anyone on the network can forge an admin token. Set "
                "JWT_SECRET=$(openssl rand -hex 32) in server/.env, or start "
                "without --lan."
            )
        return errors
    if weak_secret:
        errors.append(
            "JWT_SECRET is missing, a documented placeholder, or shorter than "
            f"{_MIN_JWT_SECRET_LEN} characters. Anyone who can reach the API "
            "can forge a token for any user, including admin. Set "
            "JWT_SECRET=$(openssl rand -hex 32)."
        )
    if not (s.APP_SECRET or "").strip():
        errors.append(
            "APP_SECRET is empty. Stored API keys would be encrypted under "
            "JWT_SECRET (or a public default). Set "
            "APP_SECRET=$(openssl rand -hex 32) before anyone saves a key."
        )
    if s.ALLOW_CONFIG_EXTRA_STEPS:
        errors.append(
            "ALLOW_CONFIG_EXTRA_STEPS=true lets a review config run arbitrary "
            "scripts as the server user. It is never allowed under PUBLIC_MODE."
        )
    return errors


def is_loopback(host: str | None) -> bool:
    """True when *host* is only reachable from this machine."""
    h = (host or "").strip().lower()
    return h in _LOOPBACK_HOSTS or h.startswith("127.")


def assert_public_posture(s: Settings) -> None:
    """Raise RuntimeError if PUBLIC_MODE is on and the secrets are not real."""
    errors = public_posture_errors(s)
    if errors:
        raise RuntimeError(
            "PUBLIC_MODE=true but this process is not safe to expose:\n- "
            + "\n- ".join(errors)
        )


def apply_public_defaults(s: Settings) -> None:
    """Fill the public-posture defaults an operator should not have to remember.

    Only empty / documented-dev values are replaced; an explicit setting wins.
    Mutates *s* in place so the already-imported singleton picks them up.
    No-op when PUBLIC_MODE is off, so the test suite (which pins PUBLIC_MODE
    false at import) is untouched.
    """
    if not s.PUBLIC_MODE:
        return
    if not s.RATELIMIT_REGISTER:
        s.RATELIMIT_REGISTER = _PUBLIC_RATELIMIT_REGISTER
    if not s.RATELIMIT_LOGIN:
        s.RATELIMIT_LOGIN = _PUBLIC_RATELIMIT_LOGIN
    if s.ACCESS_TTL_MIN == _DEV_ACCESS_TTL_MIN:
        s.ACCESS_TTL_MIN = _PUBLIC_ACCESS_TTL_MIN
    if not s.RATELIMIT_LLM:
        s.RATELIMIT_LLM = _PUBLIC_RATELIMIT_LLM
    if not s.RATELIMIT_CREDENTIAL:
        s.RATELIMIT_CREDENTIAL = _PUBLIC_RATELIMIT_CREDENTIAL
    if s.CORS_ORIGIN_REGEX == DEV_CORS_ORIGIN_REGEX:
        s.CORS_ORIGIN_REGEX = ""
    # A public instance sits behind the tunnel + the studio's /api rewrite, so
    # the socket peer is always 127.0.0.1 and only CF-Connecting-IP identifies
    # the client — the same thing `dev.sh up --public` exports. An explicit
    # TRUST_PROXY_HEADERS=false (e.g. a public instance reached without
    # Cloudflare) still wins.
    if "TRUST_PROXY_HEADERS" not in s.model_fields_set:
        s.TRUST_PROXY_HEADERS = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Frozen at first import. Do NOT read `settings.DATA_ROOT` into a module-level
# constant anywhere: the test suite sets a per-module DATA_ROOT via env, and
# isolation holds ONLY because workspace paths are read per request/call (see
# `workspace.workspace_dir` and `tests/conftest.py`). Caching it would break that.
settings = get_settings()
