"""Independent, adversarial verification of the Phase-2 job engine contract.

These tests are authored from ENGINE_CONTRACT.md ONLY. The implementation is
treated as a black box that must satisfy the contract:

  G1  runner unit (no redis, deterministic subprocess execution + verification)
  G2  enqueue endpoints (real redis db15, no worker consuming)
  G3  full integration (real redis db15 + a real arq worker subprocess, NO network)

Isolation mirrors test_smoke.py: DATABASE_URL / DATA_ROOT / REDIS_URL are pinned to
throwaway locations BEFORE any server module is imported. The sqlite DB is a real
FILE (not :memory:) because G3 shares it across processes with a spawned worker.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

# --- Isolate BEFORE importing anything that reads settings / builds the engine. ---
_TMP = Path(tempfile.mkdtemp(prefix="ber_engine_"))
# A real sqlite FILE (absolute path) so the worker subprocess sees the same rows.
# Both come from tests/conftest.py, which pins them BEFORE any module imports the
# app — `settings` caches them on first read, so setting them here would be a
# no-op whenever another test module is collected first. Reading them back (as
# opposed to inventing our own) is what guarantees the worker subprocess below
# talks to the very same database and queue as the in-process API client.
_DATABASE_URL = os.environ["DATABASE_URL"]
_REDIS_URL = os.environ["REDIS_URL"]
_DATA_ROOT = _TMP / "_data"

os.environ["DATA_ROOT"] = str(_DATA_ROOT)
os.environ.setdefault("JWT_SECRET", "test-secret")
# NOTE: BER_ENGINE_FAKE_STEPS is intentionally NOT set in this process — G1/G2 must
# resolve real (or monkeypatched) steps. Only the G3 worker subprocess gets it.

import httpx  # noqa: E402

from ber_core.pipeline import Step  # noqa: E402  (repo root on PYTHONPATH; also inserted by bridge)
from server.app.db.models import Job, Review  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import pipeline_bridge, runner  # noqa: E402
from server.app.services.workspace import workspace_dir  # noqa: E402

# One event loop for the whole module so the shared async engine and the lazily
# cached arq pool are created and reused on a single loop (avoids the
# "attached to a different loop" trap when reusing them across tests).
pytestmark = pytest.mark.asyncio(loop_scope="module")

REPO_ROOT = Path(__file__).resolve().parents[2]
# The interpreter running this suite has the server's dependencies (server/.venv
# locally, the job's Python in CI), so the worker subprocess uses it too.
VENV_PY = Path(sys.executable)

TERMINAL = {"succeeded", "failed", "cancelled"}


# --------------------------------------------------------------------------- utils
def _redis_up() -> bool:
    try:
        from urllib.parse import urlparse
        address = urlparse(_REDIS_URL)
        with socket.create_connection((address.hostname, address.port), timeout=1):
            return True
    except OSError:
        return False


async def _flush_db15() -> None:
    """Best-effort clear of the test redis db so stale queued jobs never leak.

    The db number comes from REDIS_URL (per-xdist-worker, set in conftest), so
    a flush can only ever hit THIS worker's db — never a sibling's, never dev's.
    """
    try:
        import redis.asyncio as aioredis
    except Exception:
        return
    r = aioredis.Redis.from_url(_REDIS_URL)
    try:
        await r.flushdb()
    finally:
        close = getattr(r, "aclose", None) or getattr(r, "close", None)
        if close is not None:
            res = close()
            if asyncio.iscoroutine(res):
                await res


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _register_login(client: httpx.AsyncClient, email: str) -> dict:
    """Register + login a user; return Bearer auth headers."""
    r = await client.post(
        "/auth/register", json={"email": email, "password": "hunter2pw"}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/auth/login", json={"email": email, "password": "hunter2pw"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _make_user_and_review(client: httpx.AsyncClient, email: str) -> tuple[dict, dict]:
    """Register+login a user and POST /reviews to get a real on-disk workspace.

    Returns (auth_headers, review_json).
    """
    auth = await _register_login(client, email)
    r = await client.post(
        "/reviews",
        headers=auth,
        json={
            "title": "Engine Review",
            "topic_name": f"Topic {email.split('@')[0]}",
            "synonyms": ["topic", "t"],
            "description": "Engine-test review.",
            "not_about": ["nope"],
            "include_observational": True,
        },
    )
    assert r.status_code == 201, r.text
    review = r.json()
    assert workspace_dir(review["id"]).is_dir()
    return auth, review


def _write_script(name: str, body: str) -> str:
    """Write a tiny synthetic pipeline script and return its ABSOLUTE path."""
    p = _TMP / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


# A step that writes its produces target (argv[1]) and prints, then exits 0.
_SCRIPT_SUCCESS = (
    "import sys\n"
    "target = sys.argv[1]\n"
    "open(target, 'w').write('ok')\n"
    "print('synthetic step wrote', target)\n"
)
# A step that prints then exits non-zero WITHOUT writing its target.
_SCRIPT_EXIT3 = (
    "import sys\n"
    "print('synthetic step about to fail')\n"
    "sys.exit(3)\n"
)
# A step that exits 0 but writes NOTHING (produces target stays missing).
_SCRIPT_NOOP = (
    "import sys\n"
    "print('synthetic step did nothing for', sys.argv[1])\n"
)
# A step that dumps the multi-tenant env it was handed into its produces target.
_SCRIPT_ENV = (
    "import os, sys\n"
    "target = sys.argv[1]\n"
    "val = os.environ.get('BER_PROJECTS_DIR', '') + '|' "
    "+ os.environ.get('BER_LLM_PROVIDER', '')\n"
    "open(target, 'w').write(val)\n"
)


# ------------------------------------------------------------------- module setup
@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def _init_db():
    await init_models()  # create all tables (Job included) in the shared file DB
    if _redis_up():
        await _flush_db15()
    yield


# ================================================================== G1: runner unit
async def test_g1a_success(monkeypatch) -> None:
    """(a) success -> Job succeeded, log nonempty, produces exists."""
    async with _client() as client:
        _, review = await _make_user_and_review(client, "g1a@example.com")

    workspace = workspace_dir(review["id"])
    produces = _TMP / "g1a_out.txt"
    step = Step(
        id="synthetic", num=1, label="synthetic ok",
        script=_write_script("g1a", _SCRIPT_SUCCESS),
        args=[str(produces)], produces=[str(produces)],
    )
    _patch_step(monkeypatch, step)

    job = await _run_execute_step(review["id"], "synthetic")

    assert job.state == "succeeded", f"error={job.error!r} exit={job.exit_code}"
    assert job.exit_code == 0
    assert produces.exists() and produces.read_text() == "ok"
    # log file exists and is non-empty
    assert job.log_path, "job.log_path not set"
    log = Path(job.log_path)
    assert log.is_file() and log.stat().st_size > 0
    # log lives under <workspace>/_jobs/<job_id>.log
    assert log.parent == workspace / "_jobs"


async def test_g1b_nonzero_exit(monkeypatch) -> None:
    """(b) sys.exit(3) -> failed + exit_code 3."""
    async with _client() as client:
        _, review = await _make_user_and_review(client, "g1b@example.com")

    produces = _TMP / "g1b_out.txt"
    step = Step(
        id="synthetic", num=1, label="synthetic fail",
        script=_write_script("g1b", _SCRIPT_EXIT3),
        args=[str(produces)], produces=[str(produces)],
    )
    _patch_step(monkeypatch, step)

    job = await _run_execute_step(review["id"], "synthetic")

    assert job.state == "failed"
    assert job.exit_code == 3
    assert not produces.exists()


async def test_g1c_missing_produces(monkeypatch) -> None:
    """(c) exit 0 but produces not written -> failed + error names the missing output."""
    async with _client() as client:
        _, review = await _make_user_and_review(client, "g1c@example.com")

    produces = _TMP / "g1c_missing.txt"
    assert not produces.exists()
    step = Step(
        id="synthetic", num=1, label="synthetic noop",
        script=_write_script("g1c", _SCRIPT_NOOP),
        args=[str(produces)], produces=[str(produces)],
    )
    _patch_step(monkeypatch, step)

    job = await _run_execute_step(review["id"], "synthetic")

    assert job.state == "failed"
    assert job.error, "expected an error message about the missing output"
    err = job.error.lower()
    assert "missing" in err or "expected" in err or "produces" in err
    # the specific missing path should be referenced
    assert str(produces) in job.error


async def test_g1d_multitenant_env(monkeypatch) -> None:
    """(d) subprocess env carries BER_PROJECTS_DIR=<workspace>/projects + provider."""
    async with _client() as client:
        _, review = await _make_user_and_review(client, "g1d@example.com")

    workspace = workspace_dir(review["id"])
    produces = _TMP / "g1d_env.txt"
    step = Step(
        id="synthetic", num=1, label="synthetic env",
        script=_write_script("g1d", _SCRIPT_ENV),
        args=[str(produces)], produces=[str(produces)],
    )
    _patch_step(monkeypatch, step)

    job = await _run_execute_step(review["id"], "synthetic")

    assert job.state == "succeeded", f"error={job.error!r}"
    content = produces.read_text()
    assert str(workspace / "projects") in content, content
    assert "codex_cli" in content, content


async def test_g1e_pipeline_records_every_step(monkeypatch) -> None:
    """(e) a multi-step pipeline records EVERY step's timing in progress['steps'].

    Regression: the runner stored the live accumulator list by reference into
    job.progress and mutated it in place, so SQLAlchemy's JSON change detection
    saw the later assignment as unchanged and skipped the UPDATE — only the first
    step was ever persisted.
    """
    async with _client() as client:
        _, review = await _make_user_and_review(client, "g1e@example.com")

    o1 = _TMP / "g1e_1.txt"
    o2 = _TMP / "g1e_2.txt"
    steps = [
        Step(id="s1", num=1, label="first",
             script=_write_script("g1e", _SCRIPT_SUCCESS),
             args=[str(o1)], produces=[str(o1)]),
        Step(id="s2", num=2, label="second",
             script=_write_script("g1e", _SCRIPT_SUCCESS),
             args=[str(o2)], produces=[str(o2)]),
    ]

    def fake_canonical(workspace, **opts):  # noqa: ANN001
        return steps

    monkeypatch.setattr(pipeline_bridge, "canonical_steps_for", fake_canonical, raising=False)
    monkeypatch.setattr(runner, "canonical_steps_for", fake_canonical, raising=False)

    async with AsyncSessionLocal() as session:
        rev = await session.get(Review, review["id"])
        job = Job(review_id=review["id"], kind="pipeline", state="queued")
        session.add(job)
        await session.flush()
        await runner.execute_pipeline(session, job, rev)

    assert job.state == "succeeded", f"error={job.error!r}"
    recorded = [s["step_id"] for s in (job.progress or {}).get("steps", [])]
    assert recorded == ["s1", "s2"], f"expected both steps recorded, got {recorded}"
    for s in job.progress["steps"]:
        assert isinstance(s.get("duration_s"), (int, float)), s


def _patch_step(monkeypatch, step: Step) -> None:
    """Make find_step resolve to our synthetic step regardless of how runner imports it."""
    def fake_find_step(workspace, step_id, **opts):  # noqa: ANN001
        return step

    def fake_canonical(workspace, **opts):  # noqa: ANN001
        return [step]

    monkeypatch.setattr(pipeline_bridge, "find_step", fake_find_step, raising=False)
    monkeypatch.setattr(pipeline_bridge, "canonical_steps_for", fake_canonical, raising=False)
    monkeypatch.setattr(runner, "find_step", fake_find_step, raising=False)
    monkeypatch.setattr(runner, "canonical_steps_for", fake_canonical, raising=False)


async def _run_execute_step(review_id: str, step_id: str) -> Job:
    """Create a queued Job and drive runner.execute_step directly (no redis)."""
    async with AsyncSessionLocal() as session:
        review = await session.get(Review, review_id)
        job = Job(review_id=review_id, kind="step", step_id=step_id, state="queued")
        session.add(job)
        await session.flush()
        await runner.execute_step(session, job, review, step_id)
        return job


# ============================================================ G2: enqueue endpoints
async def test_g2_enqueue_and_ownership() -> None:
    if not _redis_up():
        pytest.skip("redis unreachable on localhost:6379")

    await _flush_db15()
    async with _client() as client:
        auth, review = await _make_user_and_review(client, "g2owner@example.com")
        review_id = review["id"]

        # POST run-step -> 202 + queued JobOut
        r = await client.post(
            f"/reviews/{review_id}/steps/1_search/run", headers=auth
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["state"] == "queued"
        assert body["review_id"] == review_id
        assert body["kind"] == "step"
        assert body["step_id"] == "1_search"
        job_id = body["id"]

        # DB row exists with arq_job_id set (JobOut does not expose it -> check DB).
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            assert job.arq_job_id, "arq_job_id not set after enqueue"

        # GET /jobs/{id} as owner -> 200
        r = await client.get(f"/jobs/{job_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == job_id

        # A second user's token -> 403
        other = await _register_login(client, "g2other@example.com")
        r = await client.get(f"/jobs/{job_id}", headers=other)
        assert r.status_code == 403, r.text

        # A second run while one is queued -> 409. Two jobs on one review share a
        # workspace: the second reads CSVs the first is still writing, so its
        # resumable steps skip work as "already done". Observed for real — a
        # duplicate run classified 0 abstracts because the first had written the
        # output file already.
        r = await client.post(f"/reviews/{review_id}/run", headers=auth, json={})
        assert r.status_code == 409, r.text
        assert job_id in r.json()["detail"], "409 should name the blocking job"

        r = await client.post(
            f"/reviews/{review_id}/steps/1_search/run", headers=auth
        )
        assert r.status_code == 409, r.text

        # An EMPTY `only` list must be rejected, not treated as "no filter":
        # pipeline.select_steps sees a falsy value and would run ALL 19 steps.
        # Checked on a DIFFERENT review so the 409 above doesn't mask the 400.
        auth2, review2 = await _make_user_and_review(client, "g2empty@example.com")
        r = await client.post(
            f"/reviews/{review2['id']}/run", headers=auth2, json={"only": []}
        )
        assert r.status_code == 400, r.text


# ====================================================== G3: full worker integration
async def test_g3_worker_pipeline() -> None:
    if not _redis_up():
        pytest.skip("redis unreachable on localhost:6379")

    # Two synthetic no-network steps, each writing its produces file.
    out1 = _TMP / "g3_step1.out"
    out2 = _TMP / "g3_step2.out"
    for p in (out1, out2):
        if p.exists():
            p.unlink()
    script = _write_script("g3_step", _SCRIPT_SUCCESS)
    fake_steps = [
        {"id": "fake_1", "num": 1, "label": "Fake step 1",
         "script": script, "args": [str(out1)], "produces": [str(out1)]},
        {"id": "fake_2", "num": 2, "label": "Fake step 2",
         "script": script, "args": [str(out2)], "produces": [str(out2)]},
    ]
    fake_json = _TMP / "fake_steps.json"
    fake_json.write_text(json.dumps(fake_steps), encoding="utf-8")

    await _flush_db15()  # clear any leftovers (e.g. G2's queued job) before the worker runs

    async with _client() as client:
        auth, review = await _make_user_and_review(client, "g3@example.com")
        review_id = review["id"]

        worker_env = dict(os.environ)
        worker_env.update(
            {
                "DATABASE_URL": _DATABASE_URL,
                "DATA_ROOT": str(_DATA_ROOT),
                "REDIS_URL": _REDIS_URL,
                "BER_ENGINE_FAKE_STEPS": str(fake_json),
                "PYTHONPATH": str(REPO_ROOT),
            }
        )
        worker_log = open(_TMP / "worker.log", "wb")
        worker = subprocess.Popen(
            [str(VENV_PY), "-m", "arq", "server.app.worker.WorkerSettings"],
            cwd=str(REPO_ROOT),
            env=worker_env,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
        )
        try:
            # Enqueue a full-pipeline run.
            r = await client.post(f"/reviews/{review_id}/run", headers=auth, json={})
            assert r.status_code == 202, r.text
            job_id = r.json()["id"]

            # Poll until terminal (worker startup + run), timeout ~30s.
            body = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if worker.poll() is not None:
                    break  # worker died; assertions below will report it
                r = await client.get(f"/jobs/{job_id}", headers=auth)
                if r.status_code == 200:
                    body = r.json()
                    if body["state"] in TERMINAL:
                        break
                await asyncio.sleep(0.5)

            worker_tail = _tail(_TMP / "worker.log")
            assert body is not None, f"no job status read.\nworker log:\n{worker_tail}"
            assert body["state"] == "succeeded", (
                f"state={body['state']} error={body.get('error')}\n"
                f"worker log:\n{worker_tail}"
            )
            assert out1.exists() and out1.read_text() == "ok"
            assert out2.exists() and out2.read_text() == "ok"
            assert body["progress"] is not None
            assert body["progress"].get("total") == 2, body["progress"]
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
            worker_log.close()


def _tail(path: Path, n: int = 40) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(no worker log)"
    return "\n".join(lines[-n:])
