"""AI taxonomy drafting (services/taxonomy_draft.py).

The extract stage requires two prompt vocabularies (intervention_categories +
outcome_domains). A new review starts with them empty, so the studio auto-drafts
them from the topic. These tests pin the slug cleaning and the parse/persist path
without a real LLM call (the blocking drafter is monkeypatched).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_taxdraft_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from server.app.db.models import ReviewConfig  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import taxonomy_draft as tax  # noqa: E402



def test_slugify_category_normalizes_to_snake_case() -> None:
    assert tax._slugify_category("taVNS") == "tavns"
    assert tax._slugify_category("Sleep Quality") == "sleep_quality"
    assert tax._slugify_category("  invasive-VNS ") == "invasive_vns"
    assert tax._slugify_category("!!!") == ""


def test_draft_blocking_cleans_and_dedupes(monkeypatch) -> None:
    """The parse layer coerces every value to a slug and drops dups/blanks."""
    from ber_core.claude_cli import ClaudeResponse

    def fake_call(prompt, model, ident, timeout=120, use_stdin=False):
        return ClaudeResponse(text=(
            '{"intervention_categories": ["taVNS", "taVNS", "Sham Control", "!!"],'
            ' "outcome_domains": ["Sleep Quality", "sleep_quality"]}'))

    monkeypatch.setattr("ber_core.claude_cli.call_claude", fake_call)
    out = tax._draft_blocking(
        topic_name="VNS for insomnia", description="", question="", not_about=[],
        model="sonnet", provider="claude_cli")
    assert out["intervention_categories"] == ["tavns", "sham_control"]  # deduped, blank dropped
    assert out["outcome_domains"] == ["sleep_quality"]  # deduped


async def _make_review(client, email):
    await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Tax", "topic_name": "VNS for insomnia",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


async def test_draft_and_save_persists_both_vocabularies(monkeypatch) -> None:
    await init_models()

    # Bypass the LLM: return fixed slugged lists.
    def fake_blocking(**kw):
        return {
            "intervention_categories": ["tavns", "sham_control", "other"],
            "outcome_domains": ["sleep_quality", "insomnia_severity"],
            "provider": "claude_cli",
        }

    monkeypatch.setattr(tax, "_draft_blocking", lambda **kw: fake_blocking(**kw))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers, rid = await _make_review(client, "taxdraft@example.com")
        r = await client.post(f"/reviews/{rid}/taxonomies/draft", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["intervention_categories"] == ["tavns", "sham_control", "other"]
        assert body["outcome_domains"] == ["sleep_quality", "insomnia_severity"]

        # Persisted to the DB config so the UI reads it back.
        async with AsyncSessionLocal() as db:
            cfg = (await db.get(ReviewConfig, rid)).config_json
            assert cfg["intervention_categories"] == ["tavns", "sham_control", "other"]
            assert cfg["outcome_domains"] == ["sleep_quality", "insomnia_severity"]


async def test_draft_and_save_raises_when_the_model_returns_nothing(monkeypatch) -> None:
    await init_models()
    monkeypatch.setattr(tax, "_draft_blocking", lambda **kw: {
        "intervention_categories": [], "outcome_domains": [], "provider": "claude_cli"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers, rid = await _make_review(client, "taxempty@example.com")
        r = await client.post(f"/reviews/{rid}/taxonomies/draft", headers=headers)
        # The router wraps the service ValueError as a 502 with a clean message.
        assert r.status_code == 502, r.text
        assert "no categories" in r.json()["detail"].lower()
