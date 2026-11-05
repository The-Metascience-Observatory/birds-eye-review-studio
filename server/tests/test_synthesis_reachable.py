"""The review-synthesis job (`kind="synthesis"`) end to end, minus the LLM.

Pins the deliberate design decisions:
- synthesis is NOT exempt from the one-job guard (it reads the bundle files a
  pipeline run rewrites, and it is user-initiated — a 409 is the honest answer,
  in BOTH directions);
- the endpoint refuses before spending anything: no bundle → 400, bad option
  values → 400 (validated against the literals synthesize_review.py owns);
- enqueue is idempotent (an active synthesis job is returned, not stacked);
- the runner binds Job.params into the script's argv;
- GET /review-document returns the exists:false shape pre-generation and turns
  stale when a recorded input file changes after generation.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


_TMP = Path(tempfile.mkdtemp(prefix="ber_synth_"))
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from server.app.db.models import Job, Review  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import jobs, review_doc, runner  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email):
    await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login", json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Synth", "topic_name": "Synth Topic",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


def _make_bundle(workspace: Path) -> Path:
    """A minimal bundle so the endpoint's exists-check passes."""
    d = workspace / "projects" / "synth_topic" / "dashboard"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(
        json.dumps({"totals": {"n_trials": 1}}), encoding="utf-8")
    return d


async def test_synthesis_counts_as_busy_in_both_directions() -> None:
    """NOT in the exempt tuple: a running synthesis blocks a pipeline run and
    vice versa — both touch the same bundle files."""
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "synthbusy@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="synthesis", state="running"))
            await db.commit()
            active = await jobs.active_job(db, rid)
            assert active is not None and active.kind == "synthesis"


async def test_enqueue_is_idempotent_while_active() -> None:
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "synthdup@example.com")
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="synthesis", state="queued"))
            await db.commit()
            review = await db.get(Review, rid)
            job = await jobs.enqueue(db, review, "synthesis", params={"depth": "brief"})
            assert job.kind == "synthesis"
            n = len((await db.execute(
                select(Job).where(Job.review_id == rid))).scalars().all())
            assert n == 1, "duplicate synthesis job was created"


async def test_post_refuses_without_a_bundle() -> None:
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "synthnobundle@example.com")
        r = await client.post(f"/reviews/{rid}/synthesize-review",
                              headers=headers, json={})
        assert r.status_code == 400
        assert "dashboard" in r.json()["detail"].lower()


async def test_post_validates_options_and_409s_when_busy() -> None:
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "synthopts@example.com")
        async with AsyncSessionLocal() as db:
            review = await db.get(Review, rid)
            _make_bundle(Path(review.workspace_path))

        for body in ({"depth": "extreme"}, {"audience": "dog"},
                     {"model": "gpt4"}, {"sections": ["nope"]},
                     {"sections": []}):
            r = await client.post(f"/reviews/{rid}/synthesize-review",
                                  headers=headers, json=body)
            assert r.status_code == 400, body

        # A running pipeline job → 409 (synthesis is not exempt).
        async with AsyncSessionLocal() as db:
            db.add(Job(review_id=rid, kind="pipeline", state="running"))
            await db.commit()
        r = await client.post(f"/reviews/{rid}/synthesize-review",
                              headers=headers, json={})
        assert r.status_code == 409


async def test_post_returns_the_active_job_with_its_params() -> None:
    """The happy path short of redis: an already-active synthesis job is
    returned (202) instead of stacking — which also exercises the full
    validate-and-merge path over the config defaults."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "synthhappy@example.com")
        async with AsyncSessionLocal() as db:
            review = await db.get(Review, rid)
            _make_bundle(Path(review.workspace_path))
            db.add(Job(review_id=rid, kind="synthesis", state="queued",
                       params={"depth": "brief"}))
            await db.commit()
        r = await client.post(f"/reviews/{rid}/synthesize-review",
                              headers=headers,
                              json={"depth": "deep", "audience": "plain"})
        assert r.status_code == 202
        assert r.json()["kind"] == "synthesis"


def test_synthesis_steps_bind_params_into_argv(tmp_path) -> None:
    ws = tmp_path
    (ws / "config.yaml").write_text("topic:\n  name: Synth Topic\n",
                                    encoding="utf-8")
    steps = runner._synthesis_steps(ws, {
        "depth": "deep", "audience": "plain", "model": "opus",
        "sections": ["key_findings", "limitations"],
    })
    assert [s.id for s in steps] == ["synthesize_review"]
    step = steps[0]
    assert step.script == "synthesize_review.py"
    args = step.args
    assert args[args.index("--depth") + 1] == "deep"
    assert args[args.index("--audience") + 1] == "plain"
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--sections") + 1] == "key_findings,limitations"
    # --force: the job only exists because the user asked to (re)generate.
    assert "--force" in args
    assert step.produces[0].endswith("review_document.md")


def test_synthesis_steps_default_params(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text("topic:\n  name: Synth Topic\n",
                                          encoding="utf-8")
    step = runner._synthesis_steps(tmp_path, {})[0]
    assert step.args[step.args.index("--depth") + 1] == "standard"
    assert step.args[step.args.index("--audience") + 1] == "clinician"
    assert "--sections" not in step.args


async def test_get_review_document_states() -> None:
    """exists:false pre-generation; exists+fresh after; stale after an input
    file recorded in the sidecar's fingerprint gains a newer mtime."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "synthdoc@example.com")
        r = await client.get(f"/reviews/{rid}/review-document", headers=headers)
        assert r.status_code == 200
        assert r.json()["exists"] is False

        async with AsyncSessionLocal() as db:
            review = await db.get(Review, rid)
            ws = Path(review.workspace_path)
        bundle = _make_bundle(ws)
        summary = bundle / "summary.json"
        data = bundle.parent / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "review_document.md").write_text("# Review\n\nBody.",
                                                 encoding="utf-8")
        (data / "review_document.json").write_text(json.dumps({
            "depth": "standard", "audience": "clinician", "model": "sonnet",
            "provider": "claude_cli", "generated_at": "2026-01-01T00:00:00+00:00",
            "word_count": 3, "sections": [
                {"id": "key_findings", "title": "Key findings",
                 "source": "llm", "model": "sonnet"}],
            "source_fingerprint": {str(summary): summary.stat().st_mtime_ns},
        }), encoding="utf-8")

        r = await client.get(f"/reviews/{rid}/review-document", headers=headers)
        body = r.json()
        assert body["exists"] is True
        assert body["stale"] is False
        assert body["depth"] == "standard"
        assert body["markdown"].startswith("# Review")

        # Touch the recorded input → stale.
        os.utime(summary, ns=(summary.stat().st_atime_ns,
                              summary.stat().st_mtime_ns + 10_000_000))
        r = await client.get(f"/reviews/{rid}/review-document", headers=headers)
        assert r.json()["stale"] is True
        assert "changed" in (r.json()["stale_reason"] or "")


def test_option_literals_come_from_the_script() -> None:
    """The router validates against the script's own literals — pin the import
    so a rename there breaks loudly here, not as a silent 400-everything."""
    assert "standard" in review_doc.DEPTHS
    assert "clinician" in review_doc.AUDIENCES
    assert "auto" in review_doc.MODELS
    assert "key_findings" in review_doc.SECTION_IDS
