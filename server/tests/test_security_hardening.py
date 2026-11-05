"""Pre-public-release hardening: config keys, paths, LLM access, secrets, auth.

Each test names the hole it closes. They run against the real app over
ASGITransport (no network, no real CLI) unless noted.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from server.app import errors  # noqa: E402
from server.app.config import Settings, apply_public_defaults, public_posture_errors, settings  # noqa: E402
from server.app.db.models import Job, Review, ReviewConfig, User  # noqa: E402
from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import (  # noqa: E402
    agent as agent_svc,
    llm_lease,
    pdf_dir,
    pipeline_bridge,
    publishing,
    redact,
    runner,
)
from server.app.services import workspace as ws  # noqa: E402



def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _user(client, email, *, topic=None):
    r = await client.post("/auth/register", json={"email": email, "password": "hunter2pw"})
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    rid = None
    if topic:
        r = await client.post("/reviews", headers=headers, json={
            "title": "Sec", "topic_name": topic, "synonyms": ["x"],
            "description": "d", "not_about": [],
        })
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
    return headers, rid


async def _set_user(email, **fields):
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        for k, v in fields.items():
            setattr(user, k, v)
        await db.commit()
        return user.id


@pytest.fixture(autouse=True)
async def _db():
    await init_models()
    yield


@pytest.fixture(autouse=True)
def _no_cli(monkeypatch):
    async def probe(*a, **k):
        return llm_lease.CliAuth(True, True, "host@example.com", "")
    monkeypatch.setattr(llm_lease, "probe_cli_auth", probe)
    monkeypatch.setattr(llm_lease, "probe_codex_auth", probe)


# --- S1: config keys that name paths or programs ------------------------------

@pytest.mark.parametrize("patch", [
    {"extra_steps": [{"id": "x", "script": "/tmp/evil.py"}]},
    {"prompts_dir": "/etc"},
    {"dirs": {"eligible_trials": "/tmp/elsewhere"}},
    {"dirs": {"screening_dirs": ["/tmp/x"]}},
    {"dirs": {"papers_dir": "/tmp/x"}},
])
@pytest.mark.parametrize("door", ["patch", "put"])
async def test_operator_only_config_keys_are_refused(patch, door):
    async with _client() as c:
        h, rid = await _user(c, f"s1-{door}-{abs(hash(json.dumps(patch)))}@example.com",
                             topic=f"S1 {door} {abs(hash(json.dumps(patch)))}")
        if door == "patch":
            r = await c.patch(f"/reviews/{rid}/config", headers=h, json={"config_json": patch})
        else:
            cur = (await c.get(f"/reviews/{rid}/config", headers=h)).json()["config_json"]
            cur.update(patch)
            r = await c.put(f"/reviews/{rid}/config", headers=h, json={"config_json": cur})
    assert r.status_code == 422, r.text


async def test_operator_seeded_dirs_key_does_not_lock_the_review():
    """An operator-set dirs.* value is carried through unchanged by later edits."""
    async with _client() as c:
        h, rid = await _user(c, "s1-seeded@example.com", topic="S1 seeded")
        async with AsyncSessionLocal() as db:
            row = await db.get(ReviewConfig, rid)
            row.config_json = {**row.config_json, "dirs": {"eligible_trials": "/srv/x"}}
            await db.commit()
        r = await c.patch(f"/reviews/{rid}/config", headers=h,
                          json={"config_json": {"include_observational": False}})
        assert r.status_code == 200, r.text
        assert r.json()["config_json"]["dirs"] == {"eligible_trials": "/srv/x"}
        # …but changing it is still refused.
        r = await c.patch(f"/reviews/{rid}/config", headers=h,
                          json={"config_json": {"dirs": {"eligible_trials": "/srv/y"}}})
        assert r.status_code == 422


def _workspace_with_extra_step(tmp_path: Path) -> Path:
    script = tmp_path / "derive.py"
    script.write_text("print('hi')\n")
    w = tmp_path / "ws"
    (w / "projects" / "t").mkdir(parents=True)
    (w / "config.yaml").write_text(
        "topic:\n  name: T\narticle_types: [RCT]\n"
        f"extra_steps:\n  - id: derive\n    script: {script}\n")
    return w


async def test_workspace_extra_steps_are_stripped_by_default(tmp_path, monkeypatch):
    """A config.yaml that predates the write-time refusal must not run scripts."""
    monkeypatch.setattr(settings, "ALLOW_CONFIG_EXTRA_STEPS", False)
    w = _workspace_with_extra_step(tmp_path)
    assert "extra_steps" not in pipeline_bridge.build_project(w).config
    ids = [s.id for s in pipeline_bridge.canonical_steps_for(w)]
    assert not any(i.startswith("extra:") for i in ids)


async def test_workspace_extra_steps_run_only_when_the_operator_opts_in(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_CONFIG_EXTRA_STEPS", True)
    w = _workspace_with_extra_step(tmp_path)
    ids = [s.id for s in pipeline_bridge.canonical_steps_for(w)]
    assert "extra:derive" in ids
    # …and never under PUBLIC_MODE, even with the flag on.
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    ids = [s.id for s in pipeline_bridge.canonical_steps_for(w)]
    assert "extra:derive" not in ids


def test_public_mode_refuses_to_boot_with_config_extra_steps_enabled():
    s = Settings(PUBLIC_MODE=True, JWT_SECRET="a" * 32, APP_SECRET="b" * 32,
                 ALLOW_CONFIG_EXTRA_STEPS=True)
    assert any("ALLOW_CONFIG_EXTRA_STEPS" in e for e in public_posture_errors(s))


# --- S2: the PDF folder validator ------------------------------------------------

def test_pdf_dir_refuses_the_studios_own_trees():
    for inside in (Path(settings.DATA_ROOT) / "reviews" / "someone" / "pdfs",
                   Path(settings.REPO_ROOT) / "projects" / "pdfs"):
        with pytest.raises(ValueError):
            pdf_dir.validate_pdf_dir(str(inside))


def test_pdf_dir_refuses_an_existing_pipeline_project(tmp_path):
    proj = tmp_path / "corpus" / "projects" / "long_covid"
    (proj / "data").mkdir(parents=True)
    with pytest.raises(ValueError, match="project"):
        pdf_dir.validate_pdf_dir(str(proj / "papers"))
    # A folder merely NAMED projects is fine when it holds no pipeline project.
    assert pdf_dir.validate_pdf_dir(str(tmp_path / "projects" / "pdfs")) is not None


@pytest.mark.parametrize("name", [".codex", ".local"])
def test_pdf_dir_refuses_more_credential_dirs(tmp_path, name):
    with pytest.raises(ValueError):
        pdf_dir.validate_pdf_dir(str(tmp_path / name / "pdfs"))


def test_pdf_dir_refuses_another_users_home(monkeypatch):
    monkeypatch.setattr(pdf_dir.Path, "home", staticmethod(lambda: Path("/home/operator")))
    with pytest.raises(ValueError, match="another user"):
        pdf_dir.validate_pdf_dir("/home/someone-else/pdfs")
    assert pdf_dir.validate_pdf_dir("/home/operator/pdfs") is not None


def test_pdf_dir_is_disabled_on_a_public_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    with pytest.raises(ValueError, match="disabled"):
        pdf_dir.validate_pdf_dir(str(tmp_path / "pdfs"))
    w = tmp_path / "ws"
    (w / "projects" / "s").mkdir(parents=True)
    (w / "config.yaml").write_text(f'topic:\n  name: S\ndirs:\n  base_pdf_dir: "{tmp_path}/pdfs"\n')
    proj = w / "projects" / "s"
    # A value stored before PUBLIC_MODE was switched on is ignored too.
    assert pdf_dir.resolve_for_workspace(w, proj, str(tmp_path / "user")) == proj / "papers"


# --- S4: LLM access is judged on the OWNER, everywhere ---------------------------

async def test_agent_ask_is_refused_while_another_user_holds_the_cli(monkeypatch):
    called = []

    async def fake_llm(*a, **k):
        called.append(1)
        raise AssertionError("must not reach the model")

    monkeypatch.setattr(agent_svc, "_llm", fake_llm)
    async with _client() as c:
        h, rid = await _user(c, "s4-owner@example.com", topic="S4 ask")
        await _set_user("s4-owner@example.com", llm_provider="claude_cli")
        await _user(c, "s4-holder@example.com")
        holder = await _set_user("s4-holder@example.com")
        async with AsyncSessionLocal() as db:
            await llm_lease.release(db, "x", force=True)
            await llm_lease.acquire(db, holder, None)
        try:
            r = await c.post(f"/reviews/{rid}/agent/ask", headers=h,
                             json={"stage": "setup", "message": "hi"})
            assert r.status_code == 409, r.text
            assert not called
        finally:
            async with AsyncSessionLocal() as db:
                await llm_lease.release(db, holder)


async def test_admin_running_a_users_review_is_judged_by_the_owners_provider(monkeypatch):
    """The owner's work bills the owner's provider; whoever clicked is irrelevant."""
    async with _client() as c:
        h_owner, rid = await _user(c, "s4b-owner@example.com", topic="S4 admin")
        await _set_user("s4b-owner@example.com", llm_provider="claude_cli")
        h_admin, _ = await _user(c, "s4b-admin@example.com")
        admin_id = await _set_user("s4b-admin@example.com", role="admin",
                                   llm_provider="openrouter")
        await _user(c, "s4b-holder@example.com")
        holder = await _set_user("s4b-holder@example.com")
        async with AsyncSessionLocal() as db:
            await llm_lease.release(db, "x", force=True)
            await llm_lease.acquire(db, holder, None)
        try:
            # The admin is on a BYO provider — irrelevant: the review's owner is
            # on the shared CLI, which someone else holds.
            r = await c.post(f"/reviews/{rid}/search/draft", headers=h_admin, json={})
            assert r.status_code == 409, r.text
            # An admin sees the holder's real address.
            assert "s4b-holder@example.com" in r.text
        finally:
            async with AsyncSessionLocal() as db:
                await llm_lease.release(db, holder)
        assert admin_id


async def test_public_instance_requires_byo_credentials(monkeypatch):
    async with _client() as c:
        h, rid = await _user(c, "s4c-owner@example.com", topic="S4 byo")
        await _set_user("s4c-owner@example.com", llm_provider="codex_cli")
        monkeypatch.setattr(settings, "PUBLIC_MODE", True)
        r = await c.post(f"/reviews/{rid}/taxonomies/draft", headers=h, json={})
        assert r.status_code == 403, r.text
        assert "credential" in r.json()["detail"]
        # Choosing a host-shared provider in Settings is refused up front too.
        r = await c.patch("/auth/settings", headers=h, json={"llm_provider": "claude_cli"})
        assert r.status_code == 422, r.text
        # The flag is the escape hatch for an operator who lends their login.
        monkeypatch.setattr(settings, "PUBLIC_REQUIRE_BYO_LLM", False)
        r = await c.patch("/auth/settings", headers=h, json={"llm_provider": "claude_cli"})
        assert r.status_code == 200, r.text


async def test_llm_endpoints_are_rate_limited_per_user(monkeypatch):
    calls = []

    async def fake_ask(db, review, *, stage_slug, message):
        calls.append(message)
        from server.app.db.models import AgentMessage
        return AgentMessage(id="m" + str(len(calls)), review_id=review.id, stage=stage_slug,
                            role="assistant", content="ok",
                            created_at=datetime.now(timezone.utc))

    monkeypatch.setattr(agent_svc, "ask", fake_ask)
    monkeypatch.setattr(settings, "RATELIMIT_LLM", "2/minute")
    async with _client() as c:
        h, rid = await _user(c, "s4-rate@example.com", topic="S4 rate")
        await _set_user("s4-rate@example.com", llm_provider="openrouter")
        codes = []
        for i in range(3):
            r = await c.post(f"/reviews/{rid}/agent/ask", headers=h,
                             json={"stage": "setup", "message": f"m{i}"})
            codes.append(r.status_code)
    assert codes == [200, 200, 429], codes
    assert len(calls) == 2


# --- S7: secrets never reach a log a user can read --------------------------------

@pytest.mark.parametrize("line, secret", [
    ("GET https://api.example.org/x?api_key=SEKRET123&q=1", "SEKRET123"),
    ("GET https://x.org/y?q=1&apikey=SEKRET123", "SEKRET123"),
    ("GET https://x.org/y?key=SEKRET123", "SEKRET123"),
    ("fetch ...&devkey=SEKRET123&format=json", "SEKRET123"),
    ("https://x.org/?token=SEKRET123", "SEKRET123"),
    ("Authorization: Bearer SEKRET123", "SEKRET123"),
    ("headers={'Authorization': 'Bearer SEKRET123'}", "SEKRET123"),
    ("X-ELS-APIKey: SEKRET123", "SEKRET123"),
    ("using sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"),
])
def test_redact_scrubs_key_shapes(line, secret):
    out = redact.redact(line)
    assert secret not in out and redact.REDACTED in out


def test_redact_leaves_ordinary_text_alone():
    text = "sort_key=year monkey=1 Progress: 5/10 doi=10.1000/xyz"
    assert redact.redact(text) == text


def test_step_log_never_contains_an_environment_secret(tmp_path, monkeypatch):
    """Exact-value scrubbing catches a key printed in no recognizable shape."""
    from ber_core.pipeline import Step

    monkeypatch.setenv("SCOPUS_API_KEY", "verysecretvalue42")
    script = tmp_path / "leak.py"
    script.write_text(
        "import os\n"
        "print('raw', os.environ['SCOPUS_API_KEY'])\n"
        "print('url https://api.x.org/s?api_key=other-secret-9&q=1')\n")
    log = tmp_path / "job.log"
    rc, _, _, _ = runner.run_subprocess(
        Step("leak", 1, "leak", str(script), []), tmp_path, "codex_cli", log)
    text = log.read_text()
    assert rc == 0
    assert "verysecretvalue42" not in text and "other-secret-9" not in text
    assert text.count(redact.REDACTED) >= 2


# --- S8: what an error may say --------------------------------------------------

def test_public_detail_masks_under_public_mode(monkeypatch):
    exc = RuntimeError("/srv/secret/path exploded")
    assert "/srv/secret/path" in errors.public_detail(exc, "X")
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    masked = errors.public_detail(exc, "X")
    assert "/srv/secret/path" not in masked and "reference" in masked


async def test_draft_failure_text_is_masked_on_a_public_instance(monkeypatch):
    from server.app.services import search

    async def boom(*a, **k):
        raise RuntimeError("/home/operator/.claude/creds unreadable")

    monkeypatch.setattr(search, "draft_and_save", boom)
    async with _client() as c:
        h, rid = await _user(c, "s8-draft@example.com", topic="S8 draft")
        await _set_user("s8-draft@example.com", llm_provider="openrouter")
        monkeypatch.setattr(settings, "PUBLIC_MODE", True)
        r = await c.post(f"/reviews/{rid}/search/draft", headers=h, json={})
    assert r.status_code == 502
    assert "/home/operator" not in r.text


async def test_route_holder_is_only_named_to_admins():
    async with AsyncSessionLocal() as db:
        from server.app.db.models import PublishedDashboard
        db.add(PublishedDashboard(route="s8-route", slug="s8_route",
                                  review_id="someone-elses-review", source="studio"))
        await db.commit()
        mine = Review(id="s8-mine", owner_id="x", slug="s", topic_name="t", title="t")
        ok, who = await publishing.route_available_for(db, "s8-route", mine)
        assert not ok and "someone-elses-review" not in (who or "")
        ok, who = await publishing.route_available_for(db, "s8-route", mine, reveal_holder=True)
        assert who == "review:someone-elses-review"


def test_mask_email():
    assert errors.mask_email("alice@example.com") == "a***@e***.com"
    assert errors.mask_email(None) is None


# --- S10: input bounds ------------------------------------------------------------

async def test_review_create_rejects_oversized_input():
    async with _client() as c:
        h, _ = await _user(c, "s10-create@example.com")
        r = await c.post("/reviews", headers=h, json={
            "title": "t", "topic_name": "x" * 400, "synonyms": [], "description": "",
            "not_about": []})
        assert r.status_code == 422
        r = await c.post("/reviews", headers=h, json={
            "title": "t", "topic_name": "ok", "synonyms": ["s"] * 600,
            "description": "", "not_about": []})
        assert r.status_code == 422


async def test_exclude_rejects_an_unbounded_doi_list():
    async with _client() as c:
        h, rid = await _user(c, "s10-excl@example.com", topic="S10 excl")
        r = await c.post(f"/reviews/{rid}/evidence/exclude", headers=h,
                         json={"dois": ["10.1/x"] * 6000})
        assert r.status_code == 422
        r = await c.post(f"/reviews/{rid}/evidence/exclude", headers=h,
                         json={"dois": ["10.1/" + "x" * 300]})
        assert r.status_code == 422


async def test_dashboard_rows_clamps_limit_and_offset(monkeypatch):
    from server.app.services import bundle

    seen = {}

    def fake_rows(ws_path, *, offset, limit):
        seen.update(offset=offset, limit=limit)
        return []

    monkeypatch.setattr(bundle, "read_rows", fake_rows)
    async with _client() as c:
        h, rid = await _user(c, "s10-rows@example.com", topic="S10 rows")
        r = await c.get(f"/reviews/{rid}/dashboard/rows?offset=-5&limit=99999999", headers=h)
    assert r.status_code == 200
    assert seen == {"offset": 0, "limit": 5000}


async def test_upload_quota_is_enforced_per_owner(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILES_PER_USER", 1)
    from server.app.services import jobs

    async def no_enqueue(*a, **k):
        return None

    monkeypatch.setattr(jobs, "enqueue", no_enqueue)
    pdf = b"%PDF-1.4 fake"
    async with _client() as c:
        h, rid = await _user(c, "s10-up@example.com", topic="S10 upload")
        r = await c.post(f"/reviews/{rid}/evidence/upload-pdf", headers=h,
                         data={"doi": "10.1000/a"}, files={"file": ("a.pdf", pdf)})
        assert r.status_code == 202, r.text
        r = await c.post(f"/reviews/{rid}/evidence/upload-pdf", headers=h,
                         data={"doi": "10.1000/b"}, files={"file": ("b.pdf", pdf)})
        assert r.status_code == 413, r.text


async def test_sse_stream_pings_and_ends_after_its_lifetime(monkeypatch):
    monkeypatch.setattr(settings, "SSE_PING_SECONDS", 0)
    monkeypatch.setattr(settings, "SSE_MAX_SECONDS", 1)
    async with _client() as c:
        h, rid = await _user(c, "s10-sse@example.com", topic="S10 sse")
        async with AsyncSessionLocal() as db:
            job = Job(review_id=rid, kind="metadata", state="running")
            db.add(job)
            await db.commit()
            jid = job.id
        body = (await asyncio.wait_for(c.get(f"/jobs/{jid}/logs", headers=h), 10)).text
    assert ": ping" in body
    assert "event: timeout" in body


async def test_verify_credential_kills_the_child_on_timeout(monkeypatch):
    killed = []

    class Proc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            killed.append(1)
            self.returncode = -9

        async def wait(self):
            return -9

    async def spawn(*a, **k):
        return Proc()

    monkeypatch.setattr(llm_lease, "find_cli", lambda: "/bin/true")
    monkeypatch.setattr(llm_lease.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(llm_lease, "_VERIFY_TIMEOUT", 0.05)
    res = await llm_lease.verify_credential({"ANTHROPIC_API_KEY": "x"})
    assert not res.logged_in and killed


# --- S11: authentication ----------------------------------------------------------

def _token(**claims):
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


async def test_tokens_without_exp_or_sub_are_refused():
    async with _client() as c:
        await _user(c, "s11-tok@example.com")
        uid = await _set_user("s11-tok@example.com")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        for claims in ({"sub": uid}, {"exp": future}):
            r = await c.get("/auth/me", headers={"Authorization": f"Bearer {_token(**claims)}"})
            assert r.status_code == 401, claims
        r = await c.get("/auth/me", headers={
            "Authorization": f"Bearer {_token(sub=uid, exp=future)}"})
        assert r.status_code == 200


async def test_admin_can_disable_and_reenable_an_account():
    async with _client() as c:
        h_user, _ = await _user(c, "s11-victim@example.com")
        uid = await _set_user("s11-victim@example.com")
        h_admin, _ = await _user(c, "s11-admin@example.com")
        admin_id = await _set_user("s11-admin@example.com", role="admin")
        assert (await c.get("/auth/me", headers=h_user)).status_code == 200
        r = await c.post(f"/admin/users/{uid}/disable", headers=h_admin, json={"reason": "t"})
        assert r.status_code == 200 and r.json()["disabled"] is True
        assert (await c.get("/auth/me", headers=h_user)).status_code == 401
        # Non-admins cannot, and an admin cannot lock themself out.
        assert (await c.post(f"/admin/users/{uid}/enable", headers=h_user)).status_code == 401
        r = await c.post(f"/admin/users/{admin_id}/disable", headers=h_admin)
        assert r.status_code == 409
        r = await c.post(f"/admin/users/{uid}/enable", headers=h_admin)
        assert r.json()["disabled"] is False
        assert (await c.get("/auth/me", headers=h_user)).status_code == 200


async def test_login_spends_an_argon2_verify_for_unknown_emails(monkeypatch):
    from server.app import security
    from server.app.routers import auth as auth_router

    burned = []
    real = security.burn_password_check
    monkeypatch.setattr(auth_router, "burn_password_check",
                        lambda pw: (burned.append(pw), real(pw)))
    async with _client() as c:
        r = await c.post("/auth/login", json={"email": "nobody@example.com", "password": "pw"})
    assert r.status_code == 401 and burned == ["pw"]


def test_public_defaults_trust_the_proxy_header_unless_told_otherwise():
    s = Settings(PUBLIC_MODE=True, JWT_SECRET="a" * 32, APP_SECRET="b" * 32)
    s.model_fields_set.discard("TRUST_PROXY_HEADERS")
    apply_public_defaults(s)
    assert s.TRUST_PROXY_HEADERS is True
    explicit = Settings(PUBLIC_MODE=True, JWT_SECRET="a" * 32, APP_SECRET="b" * 32,
                        TRUST_PROXY_HEADERS=False)
    apply_public_defaults(explicit)
    assert explicit.TRUST_PROXY_HEADERS is False


# --- S5: a LAN-reachable API with the documented secret ---------------------------

def test_lan_bind_with_placeholder_secret_is_refused():
    s = Settings(PUBLIC_MODE=False, JWT_SECRET="dev-insecure-change-me", BIND_HOST="0.0.0.0")
    assert any("JWT_SECRET" in e for e in public_posture_errors(s))
    assert public_posture_errors(Settings(PUBLIC_MODE=False, JWT_SECRET="dev-insecure-change-me",
                                          BIND_HOST="127.0.0.1")) == []
    assert public_posture_errors(Settings(PUBLIC_MODE=False, JWT_SECRET="x" * 40,
                                          BIND_HOST="0.0.0.0")) == []


async def test_new_review_normalized_config_is_not_shared_with_the_orm():
    """normalize_config returns a deep copy (C14), so callers can't mutate the
    stored JSON in place."""
    raw = {"synthesis": {"depth": "brief"}, "fulltext": {}}
    out = ws.normalize_config(raw)
    out["synthesis"]["depth"] = "deep"
    assert raw == {"synthesis": {"depth": "brief"}, "fulltext": {}}
