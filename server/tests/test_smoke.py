"""In-process smoke test of the whole vertical: auth -> create review -> state.

Runs against the ASGI app with httpx.ASGITransport (no network, no server).
Isolated onto a throwaway sqlite DB + DATA_ROOT under a temp dir so it never
touches server/dev.db or the developer's workspaces.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


# --- Isolate BEFORE importing anything that reads settings / builds the engine.
_TMP = Path(tempfile.mkdtemp(prefix="ber_smoke_"))
# DATABASE_URL and REDIS_URL are pinned session-wide in tests/conftest.py: they
# are read once into the cached `settings`, so a per-module assignment here would
# only take effect if this module happened to be imported first.
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402

from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services.workspace import workspace_dir  # noqa: E402



def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_vertical_smoke() -> None:
    await init_models()  # ASGITransport does not run lifespan; create tables here.

    async with _client() as client:
        # 1. health (no auth)
        r = await client.get("/health")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

        # 2. register
        r = await client.post(
            "/auth/register",
            json={"email": "smoke@example.com", "password": "hunter2pw"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["email"] == "smoke@example.com"
        assert r.json()["role"] == "user"
        # Registering LOGS YOU IN: the response carries a usable token so the
        # client can land on the reviews list without a second /auth/login.
        signup_token = r.json()["access_token"]
        assert signup_token
        r = await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {signup_token}"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "smoke@example.com"

        # 3. login -> token
        r = await client.post(
            "/auth/login",
            json={"email": "smoke@example.com", "password": "hunter2pw"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        assert token
        auth = {"Authorization": f"Bearer {token}"}

        # 4. /auth/me
        r = await client.get("/auth/me", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "smoke@example.com"

        # 5. create review -> 201 + workspace on disk
        r = await client.post(
            "/reviews",
            headers=auth,
            json={
                "title": "Smoke Review",
                "topic_name": "Test Topic",
                "synonyms": ["test topic", "TT"],
                "description": "A smoke-test review.",
                "not_about": ["unrelated stuff"],
                "include_observational": True,
            },
        )
        assert r.status_code == 201, r.text
        review = r.json()
        review_id = review["id"]
        assert review["slug"] == "test_topic"
        assert review["owner_id"]

        # The API no longer reveals server paths; the workspace location is a
        # server-side fact derived from the review id.
        assert "workspace_path" not in review
        workspace = workspace_dir(review_id)
        assert workspace.is_dir(), f"workspace missing: {workspace}"
        assert (workspace / "config.yaml").is_file(), "config.yaml not written"
        assert (workspace / "projects" / "test_topic" / "data").is_dir()

        # 6. pipeline state -> first step is the search step
        r = await client.get(f"/reviews/{review_id}/state", headers=auth)
        assert r.status_code == 200, r.text
        state = r.json()
        assert state["slug"] == "test_topic"
        assert isinstance(state["steps"], list) and state["steps"]
        first = state["steps"][0]
        assert first["number"] == 1
        assert first["script"] == "search_papers.py"
        assert first["status"] in {"pending", "blocked", "partial", "done"}
        assert "output_paths" not in first  # absolute server paths stay server-side
        assert "workspace_path" not in state
