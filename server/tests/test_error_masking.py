"""An unhandled 500 must not hand its exception text to the client.

Exception strings routinely carry absolute filesystem paths, SQL fragments and
internal identifiers. On a local instance that text is the fastest way to debug;
on an internet-facing one it is free reconnaissance, so PUBLIC_MODE swaps it for
an opaque reference that still ties the response to one log line.

The handler must stay registered on the app (not on Starlette's outer
ServerErrorMiddleware) — see the comment in main.py: registered outside
CORSMiddleware, the response comes back with no CORS headers and the browser
reports a bare network/CORS failure instead of the real status.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_errmask_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app.config import settings  # noqa: E402
from server.app.main import app  # noqa: E402


SECRET = "/srv/private/database.db — SELECT * FROM users"


@app.get("/__test_boom")
async def _boom():  # pragma: no cover - exercised through the client below
    raise RuntimeError(SECRET)


def _client() -> httpx.AsyncClient:
    # raise_app_exceptions=False so the transport returns the handler's RESPONSE
    # instead of re-raising the original exception — the response is the thing
    # under test.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_public_mode_masks_the_exception(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    async with _client() as c:
        r = await c.get("/__test_boom")
    assert r.status_code == 500
    body = r.text
    assert "SELECT" not in body
    assert "/srv/private" not in body
    assert "RuntimeError" not in body
    # A reference is still returned, so a user's bug report can be matched to the
    # logged traceback without exposing anything.
    assert r.json()["ref"]


async def test_local_mode_still_shows_the_exception(monkeypatch) -> None:
    """Masking everywhere would make local debugging materially worse; the
    detail is only a problem when strangers can reach the endpoint."""
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    async with _client() as c:
        r = await c.get("/__test_boom")
    assert r.status_code == 500
    assert "RuntimeError" in r.text
