"""Operator CLI for who may use this studio: mint codes, list them, revoke them.

The bootstrap path. The same actions exist as admin API routes (``/admin/invites``)
and as a studio page, but those need an admin account to already exist — and on a
fresh install nobody is an admin, because promotion is deliberately not
self-serve. This script is how you break that circle.

Run from the REPO ROOT so the default relative ``sqlite:///./server/dev.db``
resolves:

    PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py \
        mint --label "Alice Chen — Stanford"

    PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py list

    PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py \
        revoke <invite-id>

    PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py \
        make-admin you@example.com

    PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py \
        disable someone@example.com --reason "left the project"

    PYTHONPATH=server server/.venv/bin/python server/scripts/manage_access.py \
        enable someone@example.com

``mint`` prints the raw code ONCE — only its sha256 is stored, so it cannot be
recovered afterwards. Lost it? Revoke that invite and mint another.

``disable`` switches off ANY account — including ones made without an invite
(open signup, the shared SIGNUP_CODE, seeded users), which revoking an invite
cannot reach. It takes effect on the user's next request; ``enable`` undoes it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

# Runnable as a module from server/ (python -m scripts.manage_access).
_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app._time import utcnow  # noqa: E402
from app.db.models import InviteCode, User, UserDisabled  # noqa: E402
from app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from app.services import invites as invites_svc  # noqa: E402


async def _mint(label: str, note: str | None, days: int | None) -> int:
    expires_at = utcnow() + timedelta(days=days) if days else None
    async with AsyncSessionLocal() as db:
        invite, code = await invites_svc.mint(
            db, label=label, note=note, expires_at=expires_at
        )
        await db.commit()
        print("\nAccess code for:", invite.label)
        print("  code:", code)
        print("  id:  ", invite.id)
        print("  expires:", expires_at.isoformat() if expires_at else "never")
        print("\nThis is the only time the code is shown. Copy it now.\n")
    return 0


async def _list() -> int:
    async with AsyncSessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(InviteCode).order_by(InviteCode.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            print("No access codes yet.")
            return 0
        ids = {r.redeemed_by_user_id for r in rows if r.redeemed_by_user_id}
        emails = {}
        if ids:
            emails = {
                uid: mail
                for uid, mail in (
                    await db.execute(
                        select(User.id, User.email).where(User.id.in_(ids))
                    )
                ).all()
            }
        print(f"{'ID':34} {'STATE':9} {'REDEEMED BY':32} LABEL")
        for r in rows:
            if r.revoked_at is not None:
                state = "revoked"
            elif not invites_svc.check_active(r).ok:
                state = "expired"
            elif r.redeemed_at is not None:
                state = "redeemed"
            else:
                state = "pending"
            who = emails.get(r.redeemed_by_user_id or "", "—")
            print(f"{r.id:34} {state:9} {who:32} {r.label}")
    return 0


async def _revoke(invite_id: str) -> int:
    async with AsyncSessionLocal() as db:
        invite = await db.get(InviteCode, invite_id)
        if invite is None:
            print(f"No access code with id {invite_id}", file=sys.stderr)
            return 1
        if invite.revoked_at is not None:
            print(f"Already revoked at {invite.revoked_at}.")
            return 0
        invite.revoked_at = utcnow()
        await db.commit()
        print(f"Revoked {invite.id} ({invite.label}).")
        if invite.redeemed_by_user_id:
            print("Their session stops working on their next request.")
    return 0


async def _make_admin(email: str) -> int:
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == email.strip().lower()))
        ).scalar_one_or_none()
        if user is None:
            print(f"No user with email {email}", file=sys.stderr)
            return 1
        if user.role == "admin":
            print(f"{user.email} is already an admin.")
            return 0
        user.role = "admin"
        await db.commit()
        print(f"{user.email} is now an admin.")
        print("They must sign out and back in: role is baked into the JWT.")
    return 0


async def _user_by_email(db, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()


async def _disable(email: str, reason: str | None) -> int:
    async with AsyncSessionLocal() as db:
        user = await _user_by_email(db, email)
        if user is None:
            print(f"No user with email {email}", file=sys.stderr)
            return 1
        if await db.get(UserDisabled, user.id) is not None:
            print(f"{user.email} is already disabled.")
            return 0
        db.add(UserDisabled(user_id=user.id, reason=reason))
        await db.commit()
        print(f"{user.email} is disabled; their session stops on the next request.")
    return 0


async def _enable(email: str) -> int:
    async with AsyncSessionLocal() as db:
        user = await _user_by_email(db, email)
        if user is None:
            print(f"No user with email {email}", file=sys.stderr)
            return 1
        row = await db.get(UserDisabled, user.id)
        if row is None:
            print(f"{user.email} is not disabled.")
            return 0
        await db.delete(row)
        await db.commit()
        print(f"{user.email} is enabled again.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="create one access code for one person")
    m.add_argument("--label", required=True, help='e.g. "Alice Chen — Stanford"')
    m.add_argument("--note", default=None)
    m.add_argument(
        "--expires-days", type=int, default=None, help="omit for a code that never expires"
    )

    sub.add_parser("list", help="show every access code and its state")

    r = sub.add_parser("revoke", help="withdraw a code and its account")
    r.add_argument("invite_id")

    a = sub.add_parser("make-admin", help="promote an existing user to admin")
    a.add_argument("email")

    d = sub.add_parser("disable", help="switch an account off (any account)")
    d.add_argument("email")
    d.add_argument("--reason", default=None)

    e = sub.add_parser("enable", help="switch a disabled account back on")
    e.add_argument("email")

    args = ap.parse_args()
    # Creates the invite_codes table on first run. `create_all` adds new TABLES
    # (which is why invites are a table, not a users column — see models.py).
    await init_models()

    if args.cmd == "mint":
        return await _mint(args.label, args.note, args.expires_days)
    if args.cmd == "list":
        return await _list()
    if args.cmd == "revoke":
        return await _revoke(args.invite_id)
    if args.cmd == "make-admin":
        return await _make_admin(args.email)
    if args.cmd == "disable":
        return await _disable(args.email, args.reason)
    if args.cmd == "enable":
        return await _enable(args.email)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
