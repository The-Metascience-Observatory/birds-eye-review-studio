"""The review agent (services/agent.py + /agent endpoints).

The design promise is COLLABORATOR, not tool: one conversation that follows the
user across stages. The properties that carry that promise:

* the transcript is studio-owned and replayable (OpenRouter has no `--resume`),
  with the CLI session id used only as an optimisation when present;
* stage context is injected just-in-time per turn, never stored;
* the post-run sanity check is idempotent per job and advisory — and a
  MALFORMED model response yields zero blockers, because a parse failure must
  never invent a problem.

All tests run against a fake provider: no real CLI, no network, no cost.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace


_TMP = Path(tempfile.mkdtemp(prefix="ber_agent_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402

from server.app.db.session import AsyncSessionLocal, init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services import agent as agent_svc  # noqa: E402



class FakeLLM:
    """Records every call; scripts responses."""

    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self.responses = list(responses or [])

    async def __call__(self, prompt, *, identifier, resume_session):
        self.calls.append(
            {"prompt": prompt, "identifier": identifier, "resume": resume_session}
        )
        if self.responses:
            return self.responses.pop(0)
        return SimpleNamespace(text="ok.", cost_usd=0.01, session_id="sess-1")


def _resp(text="ok.", session_id="sess-1", cost=0.01):
    return SimpleNamespace(text=text, cost_usd=cost, session_id=session_id)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _setup(client, email):
    await client.post("/auth/register",
                      json={"email": email, "password": "hunter2pw"})
    r = await client.post("/auth/login",
                          json={"email": email, "password": "hunter2pw"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=headers, json={
        "title": "Agent Test", "topic_name": "Agent Test",
        "synonyms": ["x"], "description": "d", "not_about": [],
    })
    return headers, r.json()["id"]


# --- the transcript ------------------------------------------------------------

async def test_a_turn_persists_and_records_the_stage_transition(monkeypatch) -> None:
    """One ask writes three rows: the navigation note (so the agent keeps the
    user's path without a paid greeting per page view), the user turn, and the
    assistant turn with cost + session metadata."""
    await init_models()
    fake = FakeLLM()
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent1@example.com")
        r = await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                              json={"stage": "setup", "message": "hi there"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "assistant"
        assert r.json()["meta"]["cost_usd"] == 0.01

        r = await client.get(f"/reviews/{rid}/agent/messages", headers=headers)
        roles = [(m["role"], m["stage"]) for m in r.json()]
        assert roles == [("system", "setup"), ("user", "setup"),
                         ("assistant", "setup")]
        assert "[user is now on stage: setup]" in r.json()[0]["content"]


async def test_second_turn_resumes_the_cli_session_without_replay(monkeypatch) -> None:
    """When a session id exists, `--resume` carries the history: the prompt must
    NOT re-send earlier turns (that is the whole optimisation), and the session
    id must come from the latest assistant row's meta — deliberately not a
    Review column, which create_all could never add."""
    await init_models()
    fake = FakeLLM()
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent2@example.com")
        # Session resumption is specific to explicitly selected Claude.
        await client.patch("/auth/settings", headers=headers,
                           json={"llm_provider": "claude_cli"})
        await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                          json={"stage": "setup", "message": "first question"})
        await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                          json={"stage": "setup", "message": "second question"})
        assert fake.calls[0]["resume"] is None
        assert fake.calls[1]["resume"] == "sess-1"
        assert "first question" not in fake.calls[1]["prompt"]


async def test_the_newest_session_wins_over_older_ones(monkeypatch) -> None:
    """Sessions change over a review's life (cold restarts mint new ones).
    Resuming a STALE session would fork the CLI conversation — the agent would
    continue from an old branch and silently forget recent turns. So the id
    must come from the NEWEST assistant row, not the first one found."""
    await init_models()
    fake = FakeLLM(responses=[
        _resp(session_id="sess-OLD"),
        _resp(session_id="sess-NEW"),
        _resp(session_id="sess-NEW"),
    ])
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent2b@example.com")
        # Session resumption is specific to explicitly selected Claude.
        await client.patch("/auth/settings", headers=headers,
                           json={"llm_provider": "claude_cli"})
        for msg in ("one", "two", "three"):
            await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                              json={"stage": "setup", "message": msg})
        assert fake.calls[2]["resume"] == "sess-NEW", fake.calls[2]["resume"]


async def test_a_cold_start_replays_the_transcript(monkeypatch) -> None:
    """No session id (provider change, cleaned logs): the conversation must
    survive by replay, because the studio owns the transcript."""
    await init_models()
    fake = FakeLLM(responses=[_resp(session_id=None), _resp()])
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent3@example.com")
        await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                          json={"stage": "setup", "message": "remember the eggs"})
        await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                          json={"stage": "setup", "message": "what did I say?"})
        assert fake.calls[1]["resume"] is None
        assert "remember the eggs" in fake.calls[1]["prompt"]


async def test_stage_context_is_injected_fresh_each_turn(monkeypatch) -> None:
    """Just-in-time injection: the preamble names the CURRENT stage, so moving
    from setup to search changes what the agent is told without any stored
    briefing going stale."""
    await init_models()
    fake = FakeLLM()
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent4@example.com")
        await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                          json={"stage": "setup", "message": "a"})
        await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                          json={"stage": "search", "message": "b"})
        assert "[current stage: Define review (setup)]" in fake.calls[0]["prompt"]
        assert "(search)]" in fake.calls[1]["prompt"]


async def test_concurrent_asks_are_refused_not_interleaved(monkeypatch) -> None:
    """Two turns at once would race two CLI processes over one --resume session
    file and interleave transcript rows. Second caller gets a 409."""
    await init_models()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_llm(prompt, *, identifier, resume_session):
        started.set()
        await release.wait()
        return _resp()

    monkeypatch.setattr(agent_svc, "_llm", slow_llm)
    async with _client() as client:
        headers, rid = await _setup(client, "agent5@example.com")

        async def first():
            return await client.post(
                f"/reviews/{rid}/agent/ask", headers=headers,
                json={"stage": "setup", "message": "slow one"})

        task = asyncio.create_task(first())
        await started.wait()
        r2 = await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                               json={"stage": "setup", "message": "impatient"})
        assert r2.status_code == 409, r2.text
        release.set()
        r1 = await task
        assert r1.status_code == 200


async def test_provider_timeout_is_a_clean_504_and_stores_nothing(monkeypatch) -> None:
    """async_call_claude returns an EMPTY response on timeout rather than
    raising. That must become a visible error, not a blank assistant row."""
    await init_models()
    fake = FakeLLM(responses=[_resp(text="")])
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent6@example.com")
        r = await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                              json={"stage": "setup", "message": "hello?"})
        assert r.status_code == 504, r.text
        r = await client.get(f"/reviews/{rid}/agent/messages", headers=headers)
        assert all(m["role"] != "assistant" for m in r.json())


# --- the sanity check -----------------------------------------------------------

_BLOCKER_JSON = (
    '{"summary": "The extraction looks off.", "blockers": [{"severity": "high",'
    ' "code": "no_control_arms", "message": "0 of 412 papers have a control arm",'
    ' "evidence": "n_control=0 of 412", "suggested_fix": "check extraction fields"}]}'
)


async def test_review_persists_blockers_to_stage_state(monkeypatch) -> None:
    await init_models()
    fake = FakeLLM(responses=[_resp(text=_BLOCKER_JSON)])
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent7@example.com")
        r = await client.post(f"/reviews/{rid}/agent/review", headers=headers,
                              json={"stage": "extract1", "job_id": "job123"})
        assert r.status_code == 200, r.text
        meta = r.json()["meta"]
        assert meta["kind"] == "review" and meta["job_id"] == "job123"
        assert meta["blockers"][0]["code"] == "no_control_arms"

        from server.app.db.models import StageState

        async with AsyncSessionLocal() as s:
            st = await s.get(StageState, (rid, "extract1"))
            assert st is not None and st.approved_at is None
            assert st.decisions["ai_blockers"]["job123"][0]["severity"] == "high"


async def test_reviewing_the_same_job_twice_returns_the_first_review(monkeypatch) -> None:
    """Idempotent per job_id: React strict-mode double-fires and reloads must
    not buy a second LLM call or a duplicate card."""
    await init_models()
    fake = FakeLLM(responses=[_resp(text=_BLOCKER_JSON)])
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent8@example.com")
        body = {"stage": "extract1", "job_id": "jobX"}
        r1 = await client.post(f"/reviews/{rid}/agent/review", headers=headers, json=body)
        r2 = await client.post(f"/reviews/{rid}/agent/review", headers=headers, json=body)
        assert r1.json()["id"] == r2.json()["id"]
        assert len(fake.calls) == 1, "second call must not reach the provider"


async def test_a_malformed_review_response_yields_zero_blockers(monkeypatch) -> None:
    """A parse failure must never invent a halt. The message is still stored so
    the user sees SOMETHING happened, but no blocker cards appear."""
    await init_models()
    fake = FakeLLM(responses=[_resp(text="I think it looks fine!! {not json")])
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent9@example.com")
        r = await client.post(f"/reviews/{rid}/agent/review", headers=headers,
                              json={"stage": "extract1", "job_id": "jobY"})
        assert r.status_code == 200, r.text
        assert r.json()["meta"]["blockers"] == []


async def test_an_approved_stage_is_not_reviewed(monkeypatch) -> None:
    """Approval closes the question. Raising new blockers on a stage the user
    already signed off would make approval meaningless."""
    await init_models()
    fake = FakeLLM()
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent10@example.com")
        await client.post(f"/reviews/{rid}/stages/setup/approve", headers=headers)
        r = await client.post(f"/reviews/{rid}/agent/review", headers=headers,
                              json={"stage": "setup", "job_id": "jobZ"})
        assert r.status_code == 409, r.text
        assert "approval closes the question" in r.json()["detail"]
        assert fake.calls == []


async def test_dismissal_is_recorded_durably_and_in_the_transcript(monkeypatch) -> None:
    """'I know, proceed' costs one click and leaves a record in both places:
    StageState.decisions (the Decision-shaped audit) and a system message (so
    the rail filters the card from the transcript alone)."""
    await init_models()
    async with _client() as client:
        headers, rid = await _setup(client, "agent11@example.com")
        r = await client.post(f"/reviews/{rid}/agent/dismiss", headers=headers,
                              json={"stage": "extract1", "code": "no_control_arms",
                                    "reason": "single-arm topic, expected"})
        assert r.status_code == 200, r.text
        assert r.json()["meta"] == {"kind": "dismiss", "code": "no_control_arms"}

        from server.app.db.models import StageState

        async with AsyncSessionLocal() as s:
            st = await s.get(StageState, (rid, "extract1"))
            d = st.decisions["dismissed_blockers"][0]
            assert d["code"] == "no_control_arms"
            assert d["reason"] == "single-arm topic, expected"
            assert d["by"]


async def test_another_users_transcript_is_unreachable() -> None:
    await init_models()
    async with _client() as client:
        _, rid = await _setup(client, "agentowner@example.com")
        other, _ = await _setup(client, "agentintruder@example.com")
        r = await client.get(f"/reviews/{rid}/agent/messages", headers=other)
        assert r.status_code == 403, r.text


async def test_default_codex_replays_history_even_with_old_claude_session(monkeypatch):
    await init_models()
    fake = FakeLLM()
    monkeypatch.setattr(agent_svc, "_llm", fake)
    async with _client() as client:
        headers, rid = await _setup(client, "agent-codex-replay@example.com")
        for message in ("Remember this question", "What did I ask?"):
            response = await client.post(f"/reviews/{rid}/agent/ask", headers=headers,
                                         json={"stage": "setup", "message": message})
            assert response.status_code == 200
        assert fake.calls[1]["resume"] is None
        assert "Remember this question" in fake.calls[1]["prompt"]
