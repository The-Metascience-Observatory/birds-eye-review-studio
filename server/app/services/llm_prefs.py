"""One place for the LLM-provider precedence rule and credential handling.

Provider precedence: a review's own ``llm_default_provider`` wins when set;
otherwise the owner's account-level ``User.llm_provider``; otherwise
``codex_cli``.

CREDENTIALS — the part that is easy to get subtly, silently wrong.

Claude Code picks its credential from the process environment using a fixed
precedence list (docs: "Authentication precedence"), highest first:

    1. CLAUDE_CODE_USE_BEDROCK / _VERTEX / _FOUNDRY   (cloud provider)
    2. ANTHROPIC_AUTH_TOKEN                            (bearer)
    3. ANTHROPIC_API_KEY                               (x-api-key)
    4. apiKeyHelper                                    (settings file)
    5. CLAUDE_CODE_OAUTH_TOKEN                         (`claude setup-token`)
    6. ANTHROPIC_PROFILE / federation vars
    7. the host's own `/login` subscription credential

A user's own subscription token lands at rank 5 — above the host's login, which
is what makes per-user billing work at all. But it is BELOW ranks 1-4. And this
server hands every pipeline step a copy of ``os.environ`` (``runner.build_step_env``),
which ``dev.sh`` has already loaded the whole repo-root ``.env.local`` into. So a
single ``ANTHROPIC_API_KEY`` appearing in that file would outrank every user's
token and silently bill the operator — no error, no log line, nothing to notice.

Hence: **credential injection is EXCLUSIVE, not additive.** Selecting a provider
clears every variable in ``MANAGED_VARS`` and then sets only the ones that
provider needs. That also stops one tenant's token leaking into the next job,
which additive injection would otherwise allow whenever the next job happens to
need no credential of its own.
"""
from __future__ import annotations

import contextlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crypto
from ..db.models import Review, User, UserCredential

DEFAULT_PROVIDER = "codex_cli"
# A review whose ``llm_default_provider`` is this sentinel (or empty) follows the
# owner's account-level choice, so flipping the account setting takes effect for
# every non-overridden review. A concrete provider name is a deliberate override.
INHERIT = "inherit"

# Providers where the USER supplies the credential. They never touch the host's
# shared CLI login, so they are exempt from the single-occupancy lease and are
# billed to the user, not the operator.
BYO_PROVIDERS = frozenset({"claude_token", "anthropic_key", "openrouter"})
# The complement: providers that run on the HOST's own login (one account for
# everyone on the machine), billed to the operator.
HOST_SHARED_PROVIDERS = frozenset({"claude_cli", "codex_cli"})

# The credential kind (UserCredential.kind) each provider needs, and the env var
# it is injected as. ``claude_cli`` is absent on purpose: it needs no credential
# because it falls through to the host's own login at rank 7.
PROVIDER_CREDENTIAL = {
    "claude_token": ("claude_token", "CLAUDE_CODE_OAUTH_TOKEN"),
    "anthropic_key": ("anthropic_key", "ANTHROPIC_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
}

# Every variable this module owns. Listed once so "what are we responsible for
# clearing" cannot drift from "what we set". Anything Claude Code consults at a
# precedence rank at or above CLAUDE_CODE_OAUTH_TOKEN belongs here, whether or
# not this app ever sets it — clearing is the point.
from ber_core.environment import MANAGED_VARS



def effective_provider(review: Review | None, user: User | None) -> str:
    """Provider to use: an explicit per-review override wins; otherwise the
    owner's account-level default; otherwise codex_cli.

    A review value of ``"inherit"``/``""`` means "follow the account", so the
    Settings switch governs every review the user didn't explicitly override.
    """
    rp = review.llm_default_provider if review is not None else None
    if rp and rp != INHERIT:
        return rp
    if user is not None and user.llm_provider:
        return user.llm_provider
    return DEFAULT_PROVIDER


def is_byo(provider: str) -> bool:
    """True when the user supplies the credential (so: not the host's CLI login)."""
    return provider in BYO_PROVIDERS


async def credential_for(
    session: AsyncSession, user_id: str, kind: str
) -> UserCredential | None:
    """The user's stored credential of *kind*, or None."""
    q = select(UserCredential).where(
        UserCredential.user_id == user_id, UserCredential.kind == kind
    )
    return (await session.execute(q)).scalar_one_or_none()


async def credentials_for_user(
    session: AsyncSession, user_id: str
) -> list[UserCredential]:
    """Every credential this user has stored (for the settings response)."""
    q = select(UserCredential).where(UserCredential.user_id == user_id)
    return list((await session.execute(q)).scalars().all())


async def openrouter_key_for(
    session: AsyncSession, user: User | None
) -> str | None:
    """The owner's decrypted OpenRouter key, or None if unset/undecryptable.

    Reads ``user_credentials`` (kind="openrouter") first, then FALLS BACK to the
    legacy ``User.openrouter_key_encrypted`` column, which held the key before
    every per-user secret moved into that table. Nothing writes the legacy
    column any more, so the fallback only serves keys saved before the move.
    """
    if user is None:
        return None
    row = await credential_for(session, user.id, "openrouter")
    if row is not None:
        return crypto.decrypt(row.secret_encrypted)
    return crypto.decrypt(user.openrouter_key_encrypted)


async def has_openrouter_key(session: AsyncSession, user: User) -> bool:
    """Whether the user has an OpenRouter key stored (either location)."""
    if await credential_for(session, user.id, "openrouter") is not None:
        return True
    return bool(user.openrouter_key_encrypted)


async def resolve_for_run(
    session: AsyncSession, review: Review
) -> tuple[str, dict[str, str]]:
    """``(effective_provider, credential env)`` for a background run.

    The returned mapping is what the provider needs and nothing else; the caller
    passes it to :func:`provider_env` or ``runner.build_step_env``, which do the
    clearing. Returns an EMPTY mapping for ``claude_cli`` — that provider is
    defined by the absence of any credential, falling through to the host login.
    """
    owner = await session.get(User, review.owner_id)
    provider = effective_provider(review, owner)
    entry = PROVIDER_CREDENTIAL.get(provider)
    if entry is None or owner is None:
        return provider, {}
    kind, var = entry

    if kind == "openrouter":
        secret = await openrouter_key_for(session, owner)
    else:
        row = await credential_for(session, owner.id, kind)
        secret = crypto.decrypt(row.secret_encrypted) if row else None

    # A provider selected with no usable credential yields an empty mapping
    # rather than a partial one. The run then fails on the provider's own auth
    # error, which names the problem, instead of silently falling through to the
    # host's login and billing the operator.
    return provider, ({var: secret} if secret else {})


async def owner_pdf_dir(session: AsyncSession, review: Review) -> str | None:
    """The review owner's account-level PDF directory, or None.

    Lives here (not in pdf_dir.py) for the same reason as ``resolve_for_run``:
    owner lookups for a background run belong in one place. The review-level
    ``dirs.base_pdf_dir`` override is applied later by
    ``pdf_dir.resolve_for_workspace``.
    """
    owner = await session.get(User, review.owner_id)
    return (owner.pdf_dir or None) if owner is not None else None


def credential_env(creds: dict[str, str] | None) -> dict[str, str | None]:
    """The full set of MANAGED_VARS with *creds* applied, ``None`` = must be unset.

    The single source of truth for "what the credential environment should look
    like", shared by the in-process context manager below and the subprocess env
    builder in ``runner.py`` — so an env assembled for a thread and one assembled
    for a pipeline step cannot disagree about precedence.
    """
    result: dict[str, str | None] = {var: None for var in MANAGED_VARS}
    for var, value in (creds or {}).items():
        if value:
            result[var] = value
    return result


@contextlib.contextmanager
def provider_env(provider: str, creds: dict[str, str] | None = None):
    """Scope the provider + credential environment for one in-process LLM call.

    The dispatcher (``claude_cli``) reads the provider at call time and the
    ``claude`` binary reads its credential from the environment it is handed, so
    both must be in force across the call.

    This does NOT touch ``os.environ``. It used to, and that was a cross-tenant
    leak rather than a style problem: ``os.environ`` is process-global, the
    window stays open across an ``await`` of up to ``LLM_TIMEOUT_S`` seconds, and
    ``agent.py`` locks per REVIEW — so two owners' turns interleaved freely. The
    save/restore then unwound in the wrong order (A enters, B enters and saves
    A's values, A restores, B restores **A's** credential as the durable one),
    and ``runner.build_step_env`` copies ``os.environ`` wholesale into every
    pipeline subprocess. ``search.py`` and ``taxonomy_draft.py`` enter this same
    manager from ``anyio.to_thread`` worker threads, racing the event loop on the
    same dict.

    ``claude_cli.llm_context`` holds the same values in ``contextvars`` instead,
    which are per-task and per-thread, so concurrent callers are isolated by
    construction and nothing global is mutated. The exclusive-clearing rule is
    unchanged — ``credential_env`` still yields the full ``MANAGED_VARS`` set with
    ``None`` meaning "must be unset" — it is now applied to the child process's
    environment at spawn time instead of to this one's.
    """
    from ._pipeline_path import ensure_pipeline_on_path
    ensure_pipeline_on_path()
    from ber_core import claude_cli

    with claude_cli.llm_context(provider or DEFAULT_PROVIDER, credential_env(creds)):
        yield
