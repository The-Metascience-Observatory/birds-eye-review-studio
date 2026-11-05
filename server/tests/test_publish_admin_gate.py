"""The publish request → admin approve flow, end to end over the ASGI app.

Publishing is two-party and permission-gated. This pins the boundary and the
happy path: an owner can PROPOSE but not approve; only an admin can approve; the
deploy runs against a TEMP website dir (never the real site); the confirm_token
guards against a rebuild between preflight and approval; and route uniqueness
refuses a second review publishing over an occupied route.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_publish_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from server.app.db.models import PublishedDashboard, Review, User  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import bundle as bundle_svc  # noqa: E402
from server.app.services import publish_deploy as pd  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register_login(client, email: str) -> str:
    await client.post("/auth/register",
                      json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login",
                          json={"email": email, "password": "hunter2pw"})
    return r.json()["access_token"]


async def _make_admin(email: str) -> None:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await db.commit()


async def _new_review(client, headers, topic="Pub Topic") -> str:
    r = await client.post("/reviews", headers=headers, json={
        "title": "Pub", "topic_name": topic,
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _build_bundle(review_id: str, *, n: int) -> None:
    """Give the review a minimal built dashboard bundle so preflight/deploy work.

    Reads the review's ACTUAL workspace_path from the DB rather than
    reconstructing it from DATA_ROOT: `settings` is lru_cached and other test
    modules set DATA_ROOT at import, so the cached value may not match this
    module's env — the workspace on disk is the source of truth.
    """
    from server.app.services import pipeline_bridge
    async with AsyncSessionLocal() as db:
        review = await db.get(Review, review_id)
        ws = Path(review.workspace_path)
    slug = pipeline_bridge.project_slug(ws)
    bdir = ws / "projects" / slug / "dashboard"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / bundle_svc.SUMMARY).write_text(json.dumps({
        "totals": {"n_trials": n}, "feasibility": {"overall": "not_possible"},
    }), encoding="utf-8")
    (bdir / "trial_extractions.jsonl").write_text(
        "".join(json.dumps({"paper_id": f"p{i}"}) + "\n" for i in range(n)),
        encoding="utf-8")


async def test_owner_proposes_admin_approves_and_it_deploys(tmp_path, monkeypatch) -> None:
    await init_models()
    site = tmp_path / "site"
    monkeypatch.setattr(pd.settings, "WEBSITE_DIR", site)

    async with _client() as client:
        owner_tok = await _register_login(client, "owner1@example.com")
        owner = {"Authorization": f"Bearer {owner_tok}"}
        rid = await _new_review(client, owner)
        await _build_bundle(rid, n=3)

        # Owner proposes.
        r = await client.post(f"/reviews/{rid}/publish-request",
                              headers=owner, json={"route": "pub-topic"})
        assert r.status_code == 201, r.text
        req_id = r.json()["id"]

        # Preflight (owner may read it).
        r = await client.get(f"/reviews/{rid}/publish/preflight",
                             headers=owner, params={"route": "pub-topic"})
        assert r.status_code == 200, r.text
        pf = r.json()
        assert pf["bundle_exists"] and pf["first_publish"]
        assert pf["route_available"]
        token = pf["confirm_token"]

        # A non-admin cannot approve.
        r = await client.post(f"/admin/publish-requests/{req_id}/approve",
                              headers=owner, json={"confirm_token": token})
        assert r.status_code == 403, r.text

        # Promote + re-login for role=admin, then approve.
        await _register_login(client, "admin1@example.com")
        await _make_admin("admin1@example.com")
        r = await client.post("/auth/login",
                              json={"email": "admin1@example.com", "password": "hunter2pw"})
        admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = await client.post(f"/admin/publish-requests/{req_id}/approve",
                              headers=admin, json={"confirm_token": token})
        assert r.status_code == 200, r.text
        assert r.json()["route"] == "pub-topic"
        assert r.json()["n_trials"] == 3

        # The feed is live in the temp site.
        feed = site / "data" / "birds_eye_reviews" / "pub_topic" / "trial_extractions.jsonl"
        assert feed.exists()

        # The request is now approved and its DB record exists.
        async with AsyncSessionLocal() as db:
            pub = await db.get(PublishedDashboard, "pub-topic")
            assert pub is not None and pub.source == "studio"


async def test_a_stale_confirm_token_is_refused(tmp_path, monkeypatch) -> None:
    await init_models()
    site = tmp_path / "site"
    monkeypatch.setattr(pd.settings, "WEBSITE_DIR", site)

    async with _client() as client:
        owner_tok = await _register_login(client, "owner2@example.com")
        owner = {"Authorization": f"Bearer {owner_tok}"}
        rid = await _new_review(client, owner, topic="Stale Topic")
        await _build_bundle(rid, n=2)

        r = await client.post(f"/reviews/{rid}/publish-request",
                              headers=owner, json={"route": "stale-topic"})
        req_id = r.json()["id"]

        await _register_login(client, "admin2@example.com")
        await _make_admin("admin2@example.com")
        r = await client.post("/auth/login",
                              json={"email": "admin2@example.com", "password": "hunter2pw"})
        admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # Approve with a token that never matched.
        r = await client.post(f"/admin/publish-requests/{req_id}/approve",
                              headers=admin, json={"confirm_token": "deadbeef"})
        assert r.status_code == 409, r.text
        assert "changed since preflight" in r.json()["detail"]


async def test_a_second_review_cannot_take_an_occupied_route(tmp_path, monkeypatch) -> None:
    await init_models()
    site = tmp_path / "site"
    monkeypatch.setattr(pd.settings, "WEBSITE_DIR", site)

    async with _client() as client:
        await _register_login(client, "admin3@example.com")
        await _make_admin("admin3@example.com")
        r = await client.post("/auth/login",
                              json={"email": "admin3@example.com", "password": "hunter2pw"})
        admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # Admin is also the owner of both reviews here (simplest setup).
        rid1 = await _new_review(client, admin, topic="Shared Topic")
        await _build_bundle(rid1, n=2)
        r = await client.post(f"/reviews/{rid1}/publish-request",
                              headers=admin, json={"route": "shared-topic"})
        req1 = r.json()["id"]
        pf = (await client.get(f"/reviews/{rid1}/publish/preflight",
                               headers=admin, params={"route": "shared-topic"})).json()
        r = await client.post(f"/admin/publish-requests/{req1}/approve",
                              headers=admin, json={"confirm_token": pf["confirm_token"]})
        assert r.status_code == 200, r.text

        # A DIFFERENT review requests the same route: preflight says unavailable.
        rid2 = await _new_review(client, admin, topic="Shared Topic")
        await _build_bundle(rid2, n=5)
        pf2 = (await client.get(f"/reviews/{rid2}/publish/preflight",
                                headers=admin, params={"route": "shared-topic"})).json()
        assert pf2["route_available"] is False
        assert pf2["route_taken_by"] is not None

        # …and approving it anyway is refused, leaving the live feed alone.
        live = site / "data" / "birds_eye_reviews" / "shared_topic" / "trial_extractions.jsonl"
        before = live.read_text()
        req2 = (await client.post(f"/reviews/{rid2}/publish-request",
                                  headers=admin, json={"route": "shared-topic"})).json()
        r = await client.post(f"/admin/publish-requests/{req2['id']}/approve",
                              headers=admin, json={"confirm_token": pf2["confirm_token"]})
        assert r.status_code == 409, r.text
        assert "already taken" in r.json()["detail"]
        assert live.read_text() == before


async def test_republish_flags_a_changed_feasibility_verdict(tmp_path, monkeypatch) -> None:
    """When republishing an already-live route, a changed feasibility verdict is
    called out: the live site is then claiming something the data no longer says."""
    await init_models()
    site = tmp_path / "site"
    monkeypatch.setattr(pd.settings, "WEBSITE_DIR", site)

    async with _client() as client:
        await _register_login(client, "admin4@example.com")
        await _make_admin("admin4@example.com")
        r = await client.post("/auth/login",
                              json={"email": "admin4@example.com", "password": "hunter2pw"})
        admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

        rid = await _new_review(client, admin, topic="Feas Topic")
        # v1: publish with feasibility not_possible (the _build_bundle default).
        await _build_bundle(rid, n=2)
        req = (await client.post(f"/reviews/{rid}/publish-request",
                                 headers=admin, json={"route": "feas-topic"})).json()
        pf = (await client.get(f"/reviews/{rid}/publish/preflight",
                               headers=admin, params={"route": "feas-topic"})).json()
        assert pf["feasibility_changed"] is False  # first publish, nothing live yet
        r = await client.post(f"/admin/publish-requests/{req['id']}/approve",
                              headers=admin, json={"confirm_token": pf["confirm_token"]})
        assert r.status_code == 200, r.text

        # v2: rebuild with a DIFFERENT verdict, then preflight the same route.
        async with AsyncSessionLocal() as db:
            review = await db.get(Review, rid)
            ws = Path(review.workspace_path)
        from server.app.services import pipeline_bridge
        slug = pipeline_bridge.project_slug(ws)
        bdir = ws / "projects" / slug / "dashboard"
        (bdir / bundle_svc.SUMMARY).write_text(json.dumps({
            "totals": {"n_trials": 2}, "feasibility": {"overall": "supported"},
        }), encoding="utf-8")
        # The manifest is what preflight reads feasibility from; write one.
        (bdir / bundle_svc.MANIFEST).write_text(json.dumps({
            "feasibility": {"overall": "supported"}, "panels_rendered": [],
            "panels_skipped": [],
        }), encoding="utf-8")

        pf2 = (await client.get(f"/reviews/{rid}/publish/preflight",
                                headers=admin, params={"route": "feas-topic"})).json()
        assert pf2["route_available"] is True  # our own route
        assert pf2["live_feasibility_overall"] == "not_possible"
        assert pf2["feasibility_overall"] == "supported"
        assert pf2["feasibility_changed"] is True


async def test_publish_request_requires_authentication() -> None:
    await init_models()
    async with _client() as client:
        r = await client.post("/reviews/whatever/publish-request",
                              json={"route": "x"})
        assert r.status_code == 401, r.text


async def test_the_admin_queue_is_admin_only() -> None:
    await init_models()
    async with _client() as client:
        tok = await _register_login(client, "plainuser@example.com")
        r = await client.get("/admin/publish-requests",
                             headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, r.text
