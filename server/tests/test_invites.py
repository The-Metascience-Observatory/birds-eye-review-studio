"""Per-person access codes: redemption, single use, and revocation.

Companion to test_signup_code.py, which covers the LEGACY single shared
``SIGNUP_CODE``. Both mechanisms live side by side on /auth/register — an invite
row wins, the shared code is the fallback — so the two files together pin the
whole gate.

The behaviour that matters most here is REVOCATION. Access tokens are stateless
JWTs with no denylist, so "revoked" has to mean something enforced on every
request, not something that takes effect whenever the token happens to expire.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_invites_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app._time import utcnow  # noqa: E402
from server.app.config import settings  # noqa: E402
from server.app.db.models import InviteCode, User  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import invites as invites_svc  # noqa: E402



# Clears the PUBLIC_MODE password floor (12), so a test that means to exercise
# the code gate is never diverted into a 422 about password length.
PASSWORD = "hunter2pw-long-enough"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _mint(label: str = "Test Person", **kw) -> tuple[str, str]:
    """Mint an invite directly (no admin account needed). Returns (id, raw code)."""
    async with AsyncSessionLocal() as db:
        invite, code = await invites_svc.mint(db, label=label, **kw)
        await db.commit()
        return invite.id, code


async def _register(c: httpx.AsyncClient, email: str, code: str | None = None):
    body = {"email": email, "password": PASSWORD}
    if code is not None:
        body["invite_code"] = code
    return await c.post("/auth/register", json=body)


# --- redemption ------------------------------------------------------------

async def test_valid_code_registers_and_binds_the_user() -> None:
    await init_models()
    invite_id, code = await _mint("Alice")
    async with _client() as c:
        r = await _register(c, "inv_ok@example.com", code)
    assert r.status_code == 201, r.text
    assert r.json()["access_token"]

    async with AsyncSessionLocal() as db:
        invite = await db.get(InviteCode, invite_id)
        assert invite.redeemed_at is not None
        user = await db.get(User, invite.redeemed_by_user_id)
        assert user.email == "inv_ok@example.com"


async def test_code_is_single_use() -> None:
    """A per-person code admits ONE person. The second attempt is just a bad code."""
    await init_models()
    _, code = await _mint("Bob")
    async with _client() as c:
        first = await _register(c, "inv_first@example.com", code)
        second = await _register(c, "inv_second@example.com", code)
    assert first.status_code == 201, first.text
    assert second.status_code == 403


async def test_unknown_and_spent_codes_are_indistinguishable(monkeypatch) -> None:
    """Every failure mode returns the SAME message.

    A distinct response for "that code exists but is spent" would confirm a
    guessed code to an attacker, which is most of the secret.

    Pinned under PUBLIC_MODE because that is where the guarantee has to hold. On
    an open local instance a stray code is deliberately ignored rather than
    rejected (test_signup_code.test_open_signup_ignores_a_supplied_code), so an
    unknown code simply registers and there are no two failures to compare.
    """
    await init_models()
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    _, code = await _mint("Carol")
    async with _client() as c:
        await _register(c, "inv_c1@example.com", code)
        spent = await _register(c, "inv_c2@example.com", code)
        unknown = await _register(c, "inv_c3@example.com", "ber_totally-made-up")
    assert spent.status_code == unknown.status_code == 403
    assert spent.json()["detail"] == unknown.json()["detail"]


async def test_whitespace_around_a_pasted_code_is_tolerated() -> None:
    """Codes get copied out of email and chat, where a trailing newline is normal."""
    await init_models()
    _, code = await _mint("Dan")
    async with _client() as c:
        r = await _register(c, "inv_ws@example.com", f"  {code}\n")
    assert r.status_code == 201, r.text


async def test_expired_code_is_refused() -> None:
    await init_models()
    _, code = await _mint("Erin", expires_at=utcnow() - timedelta(days=1))
    async with _client() as c:
        r = await _register(c, "inv_exp@example.com", code)
    assert r.status_code == 403


async def test_revoked_code_cannot_be_redeemed() -> None:
    await init_models()
    invite_id, code = await _mint("Frank")
    async with AsyncSessionLocal() as db:
        invite = await db.get(InviteCode, invite_id)
        invite.revoked_at = utcnow()
        await db.commit()
    async with _client() as c:
        r = await _register(c, "inv_rev@example.com", code)
    assert r.status_code == 403


# --- revocation of a LIVE session -----------------------------------------

async def test_revoking_kills_an_already_issued_token() -> None:
    """The point of the whole design.

    JWTs are stateless with no denylist, so without the per-request invite check
    in deps.get_current_user, revoking someone would do nothing until their token
    expired — up to ACCESS_TTL_MIN later.
    """
    await init_models()
    invite_id, code = await _mint("Grace")
    async with _client() as c:
        reg = await _register(c, "inv_live@example.com", code)
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        before = await c.get("/auth/me", headers=headers)
        assert before.status_code == 200, before.text

        async with AsyncSessionLocal() as db:
            invite = await db.get(InviteCode, invite_id)
            invite.revoked_at = utcnow()
            await db.commit()

        after = await c.get("/auth/me", headers=headers)
    assert after.status_code == 401, after.text


async def test_expiry_also_ends_a_live_session() -> None:
    """Setting an expiry retroactively must cut access, not just block signup."""
    await init_models()
    invite_id, code = await _mint("Heidi")
    async with _client() as c:
        reg = await _register(c, "inv_exp_live@example.com", code)
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        assert (await c.get("/auth/me", headers=headers)).status_code == 200

        async with AsyncSessionLocal() as db:
            invite = await db.get(InviteCode, invite_id)
            invite.expires_at = utcnow() - timedelta(minutes=1)
            await db.commit()

        after = await c.get("/auth/me", headers=headers)
    assert after.status_code == 401


async def test_users_without_an_invite_are_unaffected() -> None:
    """The operator, seeded admins and every existing test fixture predate
    invites and have no row. They must keep working unconditionally."""
    await init_models()
    async with _client() as c:
        reg = await _register(c, "inv_none@example.com")
        assert reg.status_code == 201, reg.text
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        assert (await c.get("/auth/me", headers=headers)).status_code == 200


# --- public mode fails closed ---------------------------------------------

async def test_public_mode_refuses_open_signup(monkeypatch) -> None:
    """Minting invites must be sufficient to secure signup.

    Locally, empty SIGNUP_CODE means open registration. On an internet-facing
    instance that would hand an account to anyone who loads the page, so
    PUBLIC_MODE fails closed instead of relying on the operator ALSO remembering
    to set SIGNUP_CODE.
    """
    await init_models()
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    assert settings.SIGNUP_CODE == ""
    async with _client() as c:
        no_code = await _register(c, "inv_pub_none@example.com")
        bad_code = await _register(c, "inv_pub_bad@example.com", "ber_nope")
    assert no_code.status_code == 403
    assert bad_code.status_code == 403


async def test_public_mode_still_accepts_a_real_invite(monkeypatch) -> None:
    await init_models()
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    _, code = await _mint("Ivan")
    async with _client() as c:
        r = await _register(c, "inv_pub_ok@example.com", code)
    assert r.status_code == 201, r.text


async def test_public_mode_enforces_a_password_floor(monkeypatch) -> None:
    """A one-character password is acceptable in dev and never in public."""
    await init_models()
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    _, code = await _mint("Judy")
    async with _client() as c:
        r = await c.post("/auth/register", json={
            "email": "inv_pw@example.com", "password": "short", "invite_code": code,
        })
    assert r.status_code == 422
    assert "at least" in r.json()["detail"].lower()


# --- input shape -----------------------------------------------------------

async def test_junk_email_is_rejected() -> None:
    await init_models()
    async with _client() as c:
        r = await c.post(
            "/auth/register", json={"email": "not-an-email", "password": PASSWORD}
        )
    assert r.status_code == 422


# --- the stored form -------------------------------------------------------

async def test_raw_code_is_never_stored() -> None:
    """A database leak must not hand over usable codes."""
    await init_models()
    invite_id, code = await _mint("Ken")
    async with AsyncSessionLocal() as db:
        invite = await db.get(InviteCode, invite_id)
        assert invite.code_hash != code
        assert code not in str(invite.__dict__)
        assert invite.code_hash == invites_svc.hash_code(code)


async def test_minted_codes_are_unique_and_prefixed() -> None:
    codes = {invites_svc.generate_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(c.startswith(invites_svc.CODE_PREFIX) for c in codes)


async def test_claim_is_atomic_under_a_race() -> None:
    """Two registrations on one code: the DB predicate, not the earlier check,
    decides. Without the conditional UPDATE both would be admitted."""
    await init_models()
    invite_id, _ = await _mint("Leo")
    async with AsyncSessionLocal() as db:
        first = await invites_svc.claim(db, invite_id, "user-one")
        await db.commit()
    async with AsyncSessionLocal() as db:
        second = await invites_svc.claim(db, invite_id, "user-two")
        await db.commit()
    assert first is True
    assert second is False
