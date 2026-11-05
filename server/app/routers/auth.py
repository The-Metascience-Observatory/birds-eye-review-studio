"""Auth routes: register, login, me."""
from __future__ import annotations

import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crypto, ratelimit
from ..config import settings
from .._time import utcnow
from ..db.models import User, UserCredential
from ..ratelimit import limiter
from ..deps import get_current_user, get_db
from ..services import invites, llm_lease, llm_prefs, pdf_dir
from ..schemas import (
    LoginIn,
    RegisterIn,
    RegisterOut,
    TokenOut,
    UserOut,
    UserSettingsIn,
    UserSettingsOut,
)
from ..security import (
    burn_password_check,
    create_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# ONE message for every access-code failure — missing, wrong, whitespace-only.
# Distinguishing them would tell an attacker which half they got right, and a
# per-branch string is how that leak creeps back in. Keep it a single constant.
_BAD_CODE = (
    "That access code isn't valid. Ask whoever invited you for the current one."
)

# A shape check, not RFC 5322 — the lean server venv has no email-validator, and
# the access code is what actually admits someone. This only has to reject
# obvious junk so the address stays usable as an identifier and a contact.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post(
    "/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED
)
# slowapi reads the client IP off the Request, so the parameter below is required
# even though the handler body doesn't use it.
@limiter.limit(ratelimit.register_limit, exempt_when=ratelimit.register_disabled)
async def register(
    request: Request,
    payload: RegisterIn,
    db: AsyncSession = Depends(get_db),
) -> RegisterOut:
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Email required")
    if not _EMAIL_RE.match(email):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That doesn't look like an email address.",
        )

    # Access-code gate. Read settings HERE, not at module level: the object is
    # lru_cache'd and frozen at first import, so a module-level copy could not be
    # monkeypatched by tests (see the warning in config.py).
    #
    # This runs BEFORE the duplicate-email query on purpose. Without a code, an
    # anonymous caller cannot tell "email already registered" from "created" —
    # which closes the email-enumeration oracle /auth/register otherwise is.
    #
    # Two sources, checked in this order:
    #   1. A per-person InviteCode row (the real mechanism — revocable, and it
    #      records who used it).
    #   2. settings.SIGNUP_CODE, the legacy single shared code. Kept as a
    #      fallback so an instance configured before invites existed — and the
    #      13 test files that register with only email+password — keep working.
    # With neither configured, signup is OPEN — the dev/test default. PUBLIC_MODE
    # overrides that and fails closed; see the branch below.
    supplied = (payload.invite_code or "").strip()
    invite = await invites.find_by_code(db, supplied) if supplied else None
    if invite is not None:
        # The reason is deliberately discarded: revoked / expired / spent /
        # unknown all return _BAD_CODE. Distinguishing them would confirm to a
        # guesser that a code exists, which is most of the secret.
        if not invites.check(invite).ok:
            raise HTTPException(status.HTTP_403_FORBIDDEN, _BAD_CODE)
    elif settings.SIGNUP_CODE:
        # compare_digest, not ==: a naive comparison on a shared secret leaks its
        # length and prefix through timing.
        if not supplied or not secrets.compare_digest(supplied, settings.SIGNUP_CODE):
            raise HTTPException(status.HTTP_403_FORBIDDEN, _BAD_CODE)
    elif settings.PUBLIC_MODE:
        # No invite matched and no shared code is configured. On a local instance
        # that means open signup, which is the intended dev default. On an
        # internet-facing one it would mean anybody who loads the page gets an
        # account — so a public instance fails CLOSED. Minting invites must be
        # enough to secure signup; requiring the operator to ALSO remember to set
        # SIGNUP_CODE is exactly the kind of two-step configuration that gets
        # half-done.
        raise HTTPException(status.HTTP_403_FORBIDDEN, _BAD_CODE)
    # Otherwise: open signup (dev/test default). A stray code from an old client
    # is ignored rather than rejected — there is nothing to check it against, and
    # an attacker on an open instance would simply omit it.

    # Password floor. AFTER the access-code gate, so a caller holding no valid
    # code learns nothing at all about this instance — not even its password
    # policy. Enforced here rather than as a Field(min_length=...) on RegisterIn
    # because the policy is settings-driven, and a Field constraint is frozen at
    # class-definition time: it could neither relax for the test suite nor rise
    # automatically with PUBLIC_MODE. See config.effective_min_password_len.
    min_len = settings.effective_min_password_len
    if min_len and len(payload.password) < min_len:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Password must be at least {min_len} characters.",
        )

    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")

    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    if invite is not None:
        await db.flush()  # assign user.id before binding the invite to it
        # Conditional claim: two registrations racing on one code both pass the
        # check above, and only the row-level predicate can settle it. The loser
        # gets the same generic message as any other bad code.
        if not await invites.claim(db, invite.id, user.id):
            await db.rollback()
            raise HTTPException(status.HTTP_403_FORBIDDEN, _BAD_CODE)
    await db.commit()
    await db.refresh(user)
    # Signing up logs you in: hand back a token so the client lands on the
    # reviews list authenticated, with no second /auth/login round-trip.
    token = create_access_token(user.id, extra={"role": user.role})
    return RegisterOut(
        id=user.id, email=user.email, role=user.role, access_token=token
    )


@router.post("/login", response_model=TokenOut)
# The costliest unauthenticated endpoint: every attempt runs a full argon2
# verify, so unlimited guessing is both a stuffing risk and a CPU-exhaustion one.
@limiter.limit(ratelimit.login_limit, exempt_when=ratelimit.login_disabled)
async def login(
    request: Request,
    payload: LoginIn,
    db: AsyncSession = Depends(get_db),
) -> TokenOut:
    email = payload.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        # Same argon2 cost as a real check, so response time does not reveal
        # whether the address has an account.
        burn_password_check(payload.password)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not verify_password(user.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    token = create_access_token(user.id, extra={"role": user.role})
    return TokenOut(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


# The credential kinds Settings can store, and the env var each is verified
# under. Mirrors llm_prefs.PROVIDER_CREDENTIAL for the two CLI-backed kinds;
# OpenRouter is not here because it is not a Claude credential and cannot be
# verified with `claude auth status`.
_VERIFIABLE = {
    "claude_token": "CLAUDE_CODE_OAUTH_TOKEN",
    "anthropic_key": "ANTHROPIC_API_KEY",
}

# `claude setup-token` mints a token valid for one year. Recorded at save time
# so the UI can warn before it lapses instead of after a job has already failed.
_CLAUDE_TOKEN_TTL = timedelta(days=365)


async def _store_credential(
    db: AsyncSession, user: User, kind: str, secret: str
) -> None:
    """Verify a pasted credential, then store it encrypted. 422 if it is no good.

    Verification is the point: a credential that does not authenticate must fail
    HERE, where the user is looking at the field they just filled in, not on the
    worker three steps into a run. The probe also tells us which account the
    credential belongs to, which is the only way the user can confirm they pasted
    the right one.
    """
    var = _VERIFIABLE[kind]
    # Every save spawns a CLI child and makes a real model call; without a
    # limit the Settings form is a way to run unbounded processes on the host.
    ratelimit.hit_or_raise(settings.RATELIMIT_CREDENTIAL, "credential", user.id)
    # verify_credential, not probe_cli_auth: `claude auth status` answers
    # "logged in" for a completely invalid token, so it would happily store
    # anything the user pasted. The verifier makes one real haiku call, which is
    # the only thing that actually proves the credential is accepted (and costs
    # nothing at all when it isn't).
    probe = await llm_lease.verify_credential({var: secret})
    if not probe.installed:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "This server has no `claude` CLI installed, so the credential can't "
            "be verified or used.",
        )
    if not probe.logged_in:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            probe.detail
            or "That credential didn't authenticate. Check you pasted the whole "
            "value, and that it hasn't been revoked.",
        )

    now = utcnow()
    row = await llm_prefs.credential_for(db, user.id, kind)
    if row is None:
        row = UserCredential(user_id=user.id, kind=kind)
        db.add(row)
    row.secret_encrypted = crypto.encrypt(secret)
    row.account = probe.account
    row.last_verified_at = now
    row.expires_at = now + _CLAUDE_TOKEN_TTL if kind == "claude_token" else None


async def _clear_credential(db: AsyncSession, user: User, kind: str) -> None:
    row = await llm_prefs.credential_for(db, user.id, kind)
    if row is not None:
        await db.delete(row)


async def _settings_out_async(
    db: AsyncSession, user: User, pdf_dir_warning: str | None = None
) -> UserSettingsOut:
    """The settings response, including per-user credential state (booleans and
    the verified account only — never a secret)."""
    out = _settings_out(user, pdf_dir_warning)
    for row in await llm_prefs.credentials_for_user(db, user.id):
        if row.kind == "openrouter":
            out.has_openrouter_key = True
        elif row.kind == "claude_token":
            out.has_claude_token = True
            out.claude_token_account = row.account
            out.claude_token_expires_at = row.expires_at
        elif row.kind == "anthropic_key":
            out.has_anthropic_key = True
            out.anthropic_key_account = row.account
    return out


def _settings_out(user: User, pdf_dir_warning: str | None = None) -> UserSettingsOut:
    # Re-derive the warning on GET so advice (e.g. "this is a Windows drive")
    # persists across reloads instead of only showing right after saving.
    if pdf_dir_warning is None and user.pdf_dir:
        try:
            checked = pdf_dir.validate_pdf_dir(user.pdf_dir)
            pdf_dir_warning = checked.warning if checked else None
        except ValueError:
            pdf_dir_warning = None
    return UserSettingsOut(
        llm_provider=user.llm_provider or llm_prefs.DEFAULT_PROVIDER,
        # Legacy column; the user_credentials row (checked by the caller)
        # also sets this.
        has_openrouter_key=bool(user.openrouter_key_encrypted),
        pdf_dir=user.pdf_dir,
        pdf_dir_warning=pdf_dir_warning,
    )


@router.get("/settings", response_model=UserSettingsOut)
async def get_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsOut:
    """The user's AI-provider settings (never a raw secret — only whether one is
    set, and which account it verified as)."""
    return await _settings_out_async(db, user)


@router.patch("/settings", response_model=UserSettingsOut)
async def update_settings(
    payload: UserSettingsIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsOut:
    """Update provider and/or the OpenRouter key.

    The key is Fernet-encrypted before storage and never echoed back. Selecting
    ``openrouter`` with no key stored and none supplied is rejected (422) so the
    user can't strand their reviews on a provider they can't authenticate to.
    """
    # PDF directory: validate BEFORE any mutation so a bad path can't leave the
    # other fields half-applied. "" clears it (back to the per-review default).
    pdf_warning: str | None = None
    if payload.pdf_dir is not None:
        if not payload.pdf_dir.strip():
            user.pdf_dir = None
        else:
            try:
                checked = pdf_dir.validate_pdf_dir(payload.pdf_dir, user_id=user.id)
            except ValueError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
                ) from exc
            assert checked is not None  # non-empty input always yields a result
            reason = pdf_dir.writable_or_reason(checked.path)
            if reason:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, reason)
            user.pdf_dir = checked.path
            pdf_warning = checked.warning

    # Apply credential changes FIRST so the provider check below sees the final
    # state. Each is: None = leave alone, "" = clear, non-empty = verify and store.
    clearing_key = payload.openrouter_key is not None and not payload.openrouter_key.strip()
    if payload.openrouter_key is not None:
        key = payload.openrouter_key.strip()
        # Stored in user_credentials like every other per-user secret. The
        # legacy users.openrouter_key_encrypted column is cleared on any change
        # so the read-fallback (llm_prefs.openrouter_key_for) can never resurrect
        # a key the user replaced or removed.
        row = await llm_prefs.credential_for(db, user.id, "openrouter")
        if key:
            if row is None:
                row = UserCredential(user_id=user.id, kind="openrouter")
                db.add(row)
            row.secret_encrypted = crypto.encrypt(key)
            row.last_verified_at = None
            row.expires_at = None
        elif row is not None:
            await db.delete(row)
        user.openrouter_key_encrypted = None

    cleared: set[str] = set()
    for kind, supplied in (
        ("claude_token", payload.claude_token),
        ("anthropic_key", payload.anthropic_key),
    ):
        if supplied is None:
            continue
        secret = supplied.strip()
        if secret:
            # Raises 422 when it doesn't authenticate — before anything is
            # written, so a bad paste never half-applies.
            await _store_credential(db, user, kind, secret)
        else:
            await _clear_credential(db, user, kind)
            cleared.add(kind)

    if payload.llm_provider is not None:
        # On a public instance the host-shared logins bill the operator; refuse
        # choosing one here rather than at the first AI step (llm_access).
        from ..services import llm_access

        refusal = llm_access.policy_refusal(user, payload.llm_provider)
        if refusal:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, refusal)
        user.llm_provider = payload.llm_provider

    # Selecting a bring-your-own provider requires that provider's credential. An
    # explicit request for one without a credential is rejected (422) so the user
    # sees the problem; but CLEARING the credential while already on it must never
    # 422 — that would trap them in a setting they cannot leave — so the account
    # falls back to the shared CLI instead.
    await db.flush()  # so the has-credential checks below see this request's writes
    _REQUIREMENT = {
        "openrouter": (
            await llm_prefs.has_openrouter_key(db, user),
            "Add an OpenRouter API key before selecting the OpenRouter provider.",
        ),
        "claude_token": (
            await llm_prefs.credential_for(db, user.id, "claude_token") is not None,
            "Add your Claude token before selecting it. Run `claude setup-token` "
            "on your own machine and paste the result.",
        ),
        "anthropic_key": (
            await llm_prefs.credential_for(db, user.id, "anthropic_key") is not None,
            "Add an Anthropic API key before selecting it.",
        ),
    }
    have, message = _REQUIREMENT.get(user.llm_provider, (True, ""))
    if not have:
        if payload.llm_provider == user.llm_provider:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)
        if clearing_key or user.llm_provider in cleared:
            user.llm_provider = llm_prefs.DEFAULT_PROVIDER

    await db.commit()
    await db.refresh(user)
    return await _settings_out_async(db, user, pdf_warning)
