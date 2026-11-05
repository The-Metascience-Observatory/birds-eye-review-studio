"""The config write endpoints as a security boundary.

`dirs.base_pdf_dir` is the only user-supplied filesystem path in a review's
config, and it becomes `--output-dir` for `download_pdfs.py`, which does
mkdir(parents=True) and writes as the server user. An unchecked value is
therefore an arbitrary-directory write by any authenticated review owner.

Both doors are covered here, because they used to disagree: PATCH validated and
PUT did not, so the same config a PATCH refused could be installed wholesale
through PUT.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ber_cfggate_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

import httpx  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402


# Every one of these is denied for the ACCOUNT-level pdf_dir by
# services/pdf_dir.py. The review-level key must not be a way around that.
UNSAFE = ["/etc", "/root/pdfs", "/home/someone/.ssh", "not-absolute"]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _auth_and_review(c: httpx.AsyncClient) -> tuple[dict, str]:
    await c.post("/auth/register",
                 json={"email": "cfggate@example.com", "password": "hunter2pw"})
    r = await c.post("/auth/login",
                     json={"email": "cfggate@example.com", "password": "hunter2pw"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await c.post("/reviews", headers=auth, json={
        "title": "Gate Review", "topic_name": "Gate Topic",
        "synonyms": ["gate topic"], "description": "d", "not_about": ["x"],
    })
    assert r.status_code == 201, r.text
    return auth, r.json()["id"]


@pytest.mark.parametrize("bad", UNSAFE)
async def test_patch_refuses_an_unsafe_pdf_dir(bad: str) -> None:
    await init_models()
    async with _client() as c:
        auth, rid = await _auth_and_review(c)
        r = await c.patch(f"/reviews/{rid}/config", headers=auth,
                          json={"config_json": {"dirs": {"base_pdf_dir": bad}}})
    assert r.status_code == 422, f"{bad} was accepted: {r.text}"


@pytest.mark.parametrize("bad", UNSAFE)
async def test_put_refuses_an_unsafe_pdf_dir(bad: str) -> None:
    """PUT is the door that had no gate at all."""
    await init_models()
    async with _client() as c:
        auth, rid = await _auth_and_review(c)
        current = (await c.get(f"/reviews/{rid}/config", headers=auth)).json()[
            "config_json"
        ]
        current["dirs"] = {"base_pdf_dir": bad}
        r = await c.put(f"/reviews/{rid}/config", headers=auth,
                        json={"config_json": current})
    assert r.status_code == 422, f"{bad} was accepted: {r.text}"


async def test_put_still_validates_the_rest_of_the_config() -> None:
    """PUT must apply the SAME checks as PATCH, not just the new one."""
    await init_models()
    async with _client() as c:
        auth, rid = await _auth_and_review(c)
        current = (await c.get(f"/reviews/{rid}/config", headers=auth)).json()[
            "config_json"
        ]
        current["article_types"] = []
        r = await c.put(f"/reviews/{rid}/config", headers=auth,
                        json={"config_json": current})
    assert r.status_code == 422, r.text
    assert "article_types" in r.json()["detail"]


async def test_a_legitimate_pdf_dir_is_still_accepted(tmp_path: Path) -> None:
    """The gate must not break the feature it guards."""
    await init_models()
    good = str(tmp_path / "my_pdfs")
    async with _client() as c:
        auth, rid = await _auth_and_review(c)
        r = await c.patch(f"/reviews/{rid}/config", headers=auth,
                          json={"config_json": {"dirs": {"base_pdf_dir": good}}})
    assert r.status_code == 200, r.text
    assert r.json()["config_json"]["dirs"]["base_pdf_dir"] == good


# --- what is validated must be what is stored ------------------------------
# `validate_pdf_dir` returns a NORMALIZED path (expanduser, quote-stripping,
# Windows->WSL translation, resolve()). Validating that and then persisting the
# raw string let the two mean different directories: the check ran against an
# absolute path while the pipeline received a relative one.

@pytest.mark.parametrize("door", ["patch", "put"])
async def test_pdf_dir_is_stored_normalized(door: str) -> None:
    """`~/pdfs` must not reach the pipeline as a RELATIVE path.

    `validate_pdf_dir("~/pdfs")` yields `/home/<user>/pdfs`, but `Path("~/pdfs")`
    is relative — so storing the raw string made `paper_store.papers_root` build
    the store under the repo root, in a literal directory named `~`.
    """
    await init_models()
    expected = str(Path("~/pdfs").expanduser().resolve())
    async with _client() as c:
        auth, rid = await _auth_and_review(c)
        if door == "patch":
            r = await c.patch(f"/reviews/{rid}/config", headers=auth,
                              json={"config_json": {"dirs": {"base_pdf_dir": "~/pdfs"}}})
        else:
            current = (await c.get(f"/reviews/{rid}/config", headers=auth)).json()[
                "config_json"
            ]
            current["dirs"] = {"base_pdf_dir": "~/pdfs"}
            r = await c.put(f"/reviews/{rid}/config", headers=auth,
                            json={"config_json": current})
        assert r.status_code == 200, r.text
        assert r.json()["config_json"]["dirs"]["base_pdf_dir"] == expected

        # And it must SURVIVE as the normalized form — the stored row is what
        # `resolve_for_workspace` and `papers_root` read later, not the response.
        stored = (await c.get(f"/reviews/{rid}/config", headers=auth)).json()
        assert stored["config_json"]["dirs"]["base_pdf_dir"] == expected


async def test_pdf_dir_quotes_are_stripped_before_storing() -> None:
    """A pasted, quoted path validates fine and must not store the quotes.

    `Path('"/home/x/pdfs"')` is a relative path whose first component is a quote
    character — the store would land in a junk directory under the repo root.
    """
    await init_models()
    home_pdfs = str(Path("~/quoted_pdfs").expanduser().resolve())
    async with _client() as c:
        auth, rid = await _auth_and_review(c)
        r = await c.patch(
            f"/reviews/{rid}/config", headers=auth,
            json={"config_json": {"dirs": {"base_pdf_dir": f'"{home_pdfs}"'}}},
        )
    assert r.status_code == 200, r.text
    got = r.json()["config_json"]["dirs"]["base_pdf_dir"]
    assert got == home_pdfs
    assert '"' not in got
