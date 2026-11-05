"""paper_metadata: rows stay keyed by norm_doi; the fetcher gets the join key.

Rows must keep the key the evidence table joins them back on (``norm_doi``),
while the DOI handed to the network fetcher is the shared
``ber_core.identity.paper_join_key`` — which also decodes fetchpdf's ``~XX~``
filename escapes that a bare ``--`` -> ``/`` replacement left in place (so the
APIs were asked about ``10.1023/a~3a~1022`` and answered not-found).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_metakeys_"))
os.environ.setdefault("DATA_ROOT", str(_TMP / "_data"))
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import paper_metadata  # noqa: E402
from ber_core.utils import norm_doi  # noqa: E402


async def _review(email: str) -> str:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
        r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = await client.post("/reviews", headers=h, json={
            "title": "Keys", "topic_name": "Keys", "synonyms": ["x"],
            "description": "d", "not_about": []})
        return r.json()["id"]


async def test_fetcher_gets_decoded_doi_and_rows_keep_norm_doi(monkeypatch) -> None:
    await init_models()
    rid = await _review("metakeys@example.com")
    ws = Path(tempfile.mkdtemp())
    stems = ["10.1023--a~3a~1022", "10.3346--JKMS.2026.41.e279"]
    monkeypatch.setattr(paper_metadata, "_search_dois", lambda w: stems)
    asked: list[str] = []

    def fetcher(doi):
        asked.append(doi)
        return {"title": "T", "authors": "A", "source": "crossref",
                "url": "https://doi.org/" + stems[1]}

    async with AsyncSessionLocal() as db:
        await paper_metadata.enrich_review(db, rid, ws, fetcher=fetcher)
        md = await paper_metadata.load_metadata(db, rid)

    assert asked == ["10.1023/a:1022", "10.3346/jkms.2026.41.e279"]
    # Keys unchanged: exactly what the evidence table looks rows up by.
    assert set(md) == {norm_doi(s) for s in stems}
    # The dead `--` resolver link is rewritten to a working one.
    assert md[norm_doi(stems[1])]["url"] == "https://doi.org/10.3346/jkms.2026.41.e279"
