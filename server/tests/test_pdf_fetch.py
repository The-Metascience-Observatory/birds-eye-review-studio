"""Background full-text fetch (download + convert) as a parallel PDF job.

The fetch runs on its own non-LLM queue so it executes CONCURRENTLY with LLM
steps, and — like metadata enrichment — must be EXEMPT from the one-job-per-review
guard. These tests pin the exemption, the no-stacking idempotency, and that the
runner builds the two off-range scripts (download → convert) without needing them
in canonical_steps().
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_pdf_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from server.app.db.models import Job, Review  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import jobs, runner  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email):
    await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Pdf Test", "topic_name": "Pdf Test",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


async def test_a_pdf_job_does_not_count_as_busy() -> None:
    """A running full-text fetch must NOT block a pipeline run (it writes pdfs/ +
    converted/, which a running step reads but never has open for writing)."""
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "pdfbusy@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="pdf", state="running"))
            await db.commit()
            assert await jobs.active_job(db, rid) is None

            # A running PIPELINE job still IS busy.
            db.add(Job(review_id=rid, kind="pipeline", state="running"))
            await db.commit()
            assert (await jobs.active_job(db, rid)) is not None


async def test_fetch_does_not_stack_duplicate_jobs() -> None:
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "pdfdup@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="pdf", state="queued"))
            await db.commit()
            review = await db.get(Review, rid)
            await jobs.enqueue(db, review, "pdf")
            n = len((await db.execute(
                select(Job).where(Job.review_id == rid))).scalars().all())
            assert n == 1, "duplicate pdf job was created"


def test_fulltext_steps_are_download_convert_install(tmp_path) -> None:
    """The runner builds the three off-canonical scripts in order, with the right
    produces dirs — no dependency on canonical_steps() (they left the range)."""
    ws = tmp_path
    (ws / "config.yaml").write_text("topic:\n  name: Pdf Test\n", encoding="utf-8")
    steps = runner._fulltext_steps(ws)
    assert [s.id for s in steps] == [
        "5_download_pdfs", "6_convert_pdfs", "7_install_structured"]
    assert steps[0].script == "download_pdfs.py"
    assert steps[1].script == "convert_pdfs.py"
    assert steps[2].script == "install_structured.py"
    assert steps[0].produces[0].endswith("/papers/")
    # Convert works IN PLACE inside the store, so its output dir is its input.
    assert steps[1].produces[0].endswith("/papers/")
    # No DOI subset by default → no --only-dois.
    assert "--only-dois" not in steps[0].args


def test_install_structured_runs_last_and_claims_no_output(tmp_path) -> None:
    """Order and `produces` are both load-bearing.

    AFTER convert: pdf4llm skips a paper whose converted/ dir already exists, so
    an XML-only dir created first would mask that paper's PDF permanently.
    No `produces`: having nothing new to install is the steady state, and
    verify_produces would turn that into a job failure.
    """
    ws = tmp_path
    (ws / "config.yaml").write_text("topic:\n  name: Pdf Test\n", encoding="utf-8")
    steps = runner._fulltext_steps(ws)
    install = steps[-1]
    assert install.id == "7_install_structured"
    assert install.produces == []
    assert "--execute" in install.args


def test_fulltext_download_restricts_to_only_dois_when_given(tmp_path) -> None:
    """A DOI subset path is forwarded to download_pdfs as --only-dois (so the
    download is limited to the screened-in set), while convert is unchanged."""
    ws = tmp_path
    (ws / "config.yaml").write_text("topic:\n  name: Pdf Test\n", encoding="utf-8")
    steps = runner._fulltext_steps(ws, only_dois_path="/tmp/only.csv")
    dl = steps[0].args
    assert "--only-dois" in dl
    assert dl[dl.index("--only-dois") + 1] == "/tmp/only.csv"
    # The downstream steps are untouched: they act on whatever landed on disk.
    assert "--only-dois" not in steps[1].args
    assert "--only-dois" not in steps[2].args


def test_screened_in_doi_file_is_the_included_pending_set(tmp_path, monkeypatch) -> None:
    """`_write_screened_in_dois` writes exactly the SCREENED-IN DOIs (screen_state
    included/pending), honoring user_inclusions.json — so the download population
    matches the dock/PRISMA, not the broad `is_relevant` filter. A `no`/`unknown`
    paper is left out; a force-included `unknown` paper is pulled IN (which
    --relevant-only could never do)."""
    from server.app.services import evidence

    data = tmp_path / "projects" / "x" / "data"
    data.mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("topic:\n  name: x\n")
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)
    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type\n"
        "10.1/yes,yes,Original Research\n"        # → pending (download)
        "10.1/no,no,Original Research\n"          # → screened_out (skip)
        "10.1/unknown,unknown,Original Research\n"  # → screened_out UNLESS force-included
    )
    # Force-include the unknown-relevance paper — proving the evidence tiers
    # (user_inclusions.json) are honored, which --relevant-only cannot do.
    (data / "user_inclusions.json").write_text('{"10.1/unknown": "wanted anyway"}')

    path = runner._write_screened_in_dois(tmp_path, "job1")
    assert path is not None
    written = {
        line.strip() for line in Path(path).read_text().splitlines()[1:]  # skip header
        if line.strip()
    }
    assert written == {"10.1/yes", "10.1/unknown"}
    assert "10.1/no" not in written


def test_empty_screened_in_doi_file_restricts_download_to_nothing(tmp_path, monkeypatch) -> None:
    """Empty selection must not remove the allowlist and download the corpus."""
    from server.app.services import evidence

    data = tmp_path / "projects" / "x" / "data"
    data.mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("topic:\n  name: x\n")
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)
    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type\n10.1/no,no,Original Research\n")
    path = runner._write_screened_in_dois(tmp_path, "job2")
    assert path is not None
    assert Path(path).read_text() == "doi\n"


async def test_upload_pdf_stores_and_enqueues_convert() -> None:
    """A manual PDF upload lands under pdfs/<escaped-doi>.pdf and enqueues the
    convert job so the paper gains full text; a non-PDF is a 400.

    Assertions ride the HTTP responses only — a post-call AsyncSessionLocal would
    run on a different event loop than the app's cached arq pool under the full
    suite. The 202 + returned `stored`/`convert_job_id` prove the file was written
    (save_uploaded_pdf raises otherwise) and a job was enqueued; the on-disk path
    is unit-tested separately in test_evidence.save_uploaded_pdf."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "pdfupload@example.com")
        # Pre-seed a queued convert job so the endpoint's enqueue COALESCES
        # (returns this row) instead of reaching Redis — the cached arq pool is
        # bound to another event loop under the full suite. We still assert the
        # endpoint returns a convert_job_id, proving the enqueue path ran.
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="pdf", state="queued"))
            await db.commit()

        # Non-PDF payload → 400, nothing stored.
        r = await client.post(
            f"/reviews/{rid}/evidence/upload-pdf", headers=headers,
            data={"doi": "10.1/x"},
            files={"file": ("x.pdf", b"<html>nope</html>", "application/pdf")})
        assert r.status_code == 400, r.text
        assert "not a PDF" in r.json()["detail"]

        # A real PDF → 202, stored under the canonical name + a convert job id.
        r = await client.post(
            f"/reviews/{rid}/evidence/upload-pdf", headers=headers,
            data={"doi": "10.1234/Foo"},
            files={"file": ("paper.pdf", b"%PDF-1.7\nbody", "application/pdf")})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["stored"] == "10.1234--foo.pdf"
        assert body["convert_job_id"]

        # An empty file is rejected too.
        r = await client.post(
            f"/reviews/{rid}/evidence/upload-pdf", headers=headers,
            data={"doi": "10.1/y"},
            files={"file": ("e.pdf", b"", "application/pdf")})
        assert r.status_code == 400, r.text
