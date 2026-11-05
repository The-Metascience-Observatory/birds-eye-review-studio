"""Approve / unapprove endpoints, end to end over the ASGI app.

`test_stage_gates.py` proves the gate REASONING in isolation. This proves the
wiring: that approval persists, that the digest stamped at approval is the one
staleness later compares against, and that withdrawing a sign-off never destroys
a user's artifacts.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_approve_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402

from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email: str) -> tuple[dict, str]:
    """Register, log in, create a review. Returns (headers, review_id)."""
    await client.post("/auth/register",
                      json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login",
                          json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Gate Test", "topic_name": "Gate Test",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    assert r.status_code == 201, r.text
    return headers, r.json()["id"]


def _stage(payload: dict, slug: str) -> dict:
    return next(s for s in payload["stages"] if s["slug"] == slug)


async def test_approval_persists_and_unblocks_the_next_stage() -> None:
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "approve1@example.com")

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        assert r.status_code == 200, r.text
        stages = r.json()["stages"]
        # A fresh review: the first gated stage is open, later ones blocked.
        gated = [s for s in stages if s["gate"]]
        assert gated[0]["gate"]["state"] == "open"
        assert gated[1]["gate"]["state"] == "blocked"
        assert gated[1]["gate"]["blocked_by"] == gated[0]["slug"]

        first = gated[0]["slug"]
        r = await client.post(f"/reviews/{rid}/stages/{first}/approve",
                              headers=headers, json={"notes": "looks right"})
        assert r.status_code == 200, r.text
        after = r.json()
        assert _stage(after, first)["gate"]["state"] == "approved"
        assert _stage(after, first)["gate"]["approved_at"]

        # Survives a fresh read (i.e. it is in the DB, not just the response).
        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        assert _stage(r.json(), first)["gate"]["state"] == "approved"


async def test_editing_a_stages_own_config_makes_it_stale() -> None:
    """The digest stamped at approval must be the one staleness compares to.

    Uses `setup` because it is a checkpoint stage (kind="setup") and so is
    approvable with no artifacts — every artifact-producing stage correctly
    refuses approval on a review that has run nothing. Edits `topic.description`
    rather than `topic.name`, which is immutable (it determines the workspace
    directory, so renaming would orphan the review's data).
    """
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "approve2@example.com")

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        assert "topic" in _stage(r.json(), "setup")["config_keys"]

        r = await client.post(f"/reviews/{rid}/stages/setup/approve",
                              headers=headers)
        assert r.status_code == 200, r.text
        assert _stage(r.json(), "setup")["gate"]["state"] == "approved"

        r = await client.patch(f"/reviews/{rid}/config", headers=headers,
                               json={"config_json": {"topic": {"description": "a new scope"}}})
        assert r.status_code == 200, r.text

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        gate = _stage(r.json(), "setup")["gate"]
        assert gate["state"] == "stale"
        assert gate["stale_keys"] == ["topic"], gate


async def test_a_config_edit_elsewhere_leaves_an_approval_alone() -> None:
    """The reason staleness is scoped to a stage's own config_keys: editing the
    meta-analysis block must not invalidate the setup approval."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "approve3@example.com")
        await client.post(f"/reviews/{rid}/stages/setup/approve", headers=headers)

        r = await client.patch(f"/reviews/{rid}/config", headers=headers,
                               json={"config_json": {"meta_analysis": {"model": "random"}}})
        assert r.status_code == 200, r.text

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        assert _stage(r.json(), "setup")["gate"]["state"] == "approved"


async def test_unapprove_removes_the_signoff_but_not_the_work() -> None:
    """Withdrawing a sign-off must never delete artifacts: the pipeline is
    resumable, so re-running forward is the recovery path, and silently
    discarding a user's extraction would be unrecoverable."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "approve4@example.com")
        await client.post(f"/reviews/{rid}/stages/setup/approve", headers=headers)

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        before = _stage(r.json(), "search")["status"]

        r = await client.post(f"/reviews/{rid}/stages/setup/unapprove",
                              headers=headers)
        assert r.status_code == 200, r.text
        assert _stage(r.json(), "setup")["gate"]["state"] == "open"

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        assert _stage(r.json(), "search")["status"] == before


async def test_approving_a_stage_that_never_ran_is_refused() -> None:
    """A rubber-stamped empty stage is worse than no approval at all."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "approve5@example.com")
        # `classify` produces artifacts and this review has run nothing.
        r = await client.post(f"/reviews/{rid}/stages/classify/approve",
                              headers=headers)
        assert r.status_code == 409, r.text
        assert "run it before" in r.json()["detail"]


async def test_an_unknown_stage_is_404_not_a_silent_no_op() -> None:
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "approve6@example.com")
        r = await client.post(f"/reviews/{rid}/stages/not_a_stage/approve",
                              headers=headers)
        assert r.status_code == 404, r.text


async def test_another_users_review_is_not_approvable() -> None:
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "owner@example.com")
        other, _ = await _setup(client, "intruder@example.com")
        r = await client.post(f"/reviews/{rid}/stages/setup/approve",
                              headers=other)
        # 403, not 404: _get_owned_review distinguishes "exists but not yours".
        assert r.status_code == 403, r.text


# --- editing scope after approval (the Topic & scope stage) -------------------

async def test_editing_one_scope_field_preserves_the_others() -> None:
    """Scope is the field users most often revise after seeing what the search
    returned, so the setup stage must be safe to come BACK to. The server merges
    one level deep, so patching `topic.not_about` alone must not drop the name,
    synonyms, or description — a read-modify-write client would."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "scope1@example.com")

        r = await client.patch(f"/reviews/{rid}/config", headers=headers,
                               json={"config_json": {"topic": {
                                   "synonyms": ["alpha"],
                                   "description": "original scope",
                                   "not_about": ["rats"]}}})
        assert r.status_code == 200, r.text

        r = await client.patch(f"/reviews/{rid}/config", headers=headers,
                               json={"config_json": {"topic": {
                                   "not_about": ["rats", "in vitro"]}}})
        assert r.status_code == 200, r.text
        topic = r.json()["config_json"]["topic"]
        assert topic["not_about"] == ["rats", "in vitro"]
        assert topic["synonyms"] == ["alpha"], "sibling field was clobbered"
        assert topic["description"] == "original scope"
        assert topic["name"], "topic.name must survive a scope edit"


async def test_revising_scope_stales_the_setup_approval() -> None:
    """Not a nuisance — the gate doing its job. Later stages screened papers
    against the OLD scope, so a scope change should be re-confirmed."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "scope2@example.com")
        await client.post(f"/reviews/{rid}/stages/setup/approve", headers=headers)

        await client.patch(f"/reviews/{rid}/config", headers=headers,
                           json={"config_json": {"topic": {
                               "not_about": ["added later"]}}})

        r = await client.get(f"/reviews/{rid}/stages", headers=headers)
        gate = _stage(r.json(), "setup")["gate"]
        assert gate["state"] == "stale"
        assert gate["stale_keys"] == ["topic"]


async def test_the_topic_name_stays_immutable() -> None:
    """It names the workspace directory, so a rename would orphan every artifact
    already written. The SetupStage UI shows it read-only for this reason."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "scope3@example.com")
        r = await client.patch(f"/reviews/{rid}/config", headers=headers,
                               json={"config_json": {"topic": {"name": "Renamed"}}})
        assert r.status_code == 422, r.text
        assert "orphan" in r.json()["detail"]
