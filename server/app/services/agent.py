"""The review agent: one collaborator that follows the user through the wizard.

A TOOL is called, returns, and forgets. A COLLABORATOR remembers what the user
decided three stages ago and notices when new data contradicts it. That framing
(the user's) fixes three design choices:

* **Persistent transcript.** Cross-stage memory is the whole value, so every
  turn is a row in ``agent_messages``. The studio owns the history — `--resume`
  is a Claude-CLI luxury with no OpenRouter equivalent, so when a session id is
  available it is used as an optimisation, and when it is not the transcript is
  replayed. Same entry point either way.
* **Just-in-time stage context, not one big up-front briefing.** The preamble is
  rebuilt from live data on every turn and never stored, so it cannot go stale:
  a briefing written at setup would describe a corpus that no longer exists by
  extraction.
* **Advisory blockers.** After a run, `review()` reads the step's structured
  ``emit_run_summary`` counters (never log scraping) and may raise blockers —
  which annotate and warn, never halt. A malformed model response yields ZERO
  blockers: a parse failure must never invent a problem.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AgentMessage, Review, StageState
from . import llm_prefs, pipeline_bridge
from . import stages as stages_svc

from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()
# are replaced by an explicit omission marker — an honest gap beats a silent
# one, and the rolling-summary compressor is deferred until a corpus needs it.
HISTORY_TURNS = 30

AGENT_MODEL = "sonnet"
LLM_TIMEOUT_S = 120


class AgentBusy(Exception):
    """A turn is already in flight for this review."""


class AgentFailed(Exception):
    """The provider returned nothing (timeout or CLI failure)."""


# One lock per review: two concurrent turns would race two CLI processes over
# one --resume session file AND interleave transcript rows. In-process is
# sufficient — dev runs a single uvicorn worker (revisit if that changes).
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(review_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault(review_id, asyncio.Lock())


async def _resolve_provider(
    db: AsyncSession, review: Review
) -> tuple[str, dict[str, str]]:
    """(effective_provider, credential env) for this review's owner."""
    return await llm_prefs.resolve_for_run(db, review)


# ``llm_prefs.provider_env`` — NOT a local copy, so the rule about clearing
# higher-precedence credential variables lives in one place. It scopes the
# provider + credential in contextvars (never ``os.environ``), so turns for
# different reviews running concurrently cannot see each other's credential.
_provider_env = llm_prefs.provider_env


async def _llm(prompt: str, *, identifier: str, resume_session: str | None):
    """Provider call, isolated for tests to monkeypatch. Returns ClaudeResponse."""
    from ber_core.claude_cli import async_call_claude  # lazy: repo-root import

    return await async_call_claude(
        prompt,
        AGENT_MODEL,
        identifier,
        timeout=LLM_TIMEOUT_S,
        resume_session=resume_session,
    )


# ── Transcript helpers ───────────────────────────────────────────────────────

async def recent_messages(
    db: AsyncSession, review_id: str, *, stage: str | None = None, limit: int = 200
) -> list[AgentMessage]:
    """Oldest-first slice of the transcript (optionally one stage's)."""
    q = (
        select(AgentMessage)
        .where(AgentMessage.review_id == review_id)
        .order_by(AgentMessage.created_at.desc(), AgentMessage.id.desc())
        .limit(limit)
    )
    if stage is not None:
        q = q.where(AgentMessage.stage == stage)
    rows = list((await db.execute(q)).scalars().all())
    rows.reverse()
    return rows


def _last_session_id(history: list[AgentMessage]) -> str | None:
    """The newest assistant turn's CLI session id, if any.

    Lives in message meta rather than a Review column on purpose: create_all
    cannot add columns to existing tables and this repo has no Alembic.
    """
    for msg in reversed(history):
        if msg.role == "assistant" and (msg.meta or {}).get("session_id"):
            return msg.meta["session_id"]
    return None


def _replay(history: list[AgentMessage]) -> str:
    """Compact transcript for a cold start (no resumable session)."""
    tail = history[-HISTORY_TURNS:]
    lines = []
    if len(history) > len(tail):
        lines.append(f"[{len(history) - len(tail)} earlier turns omitted]")
    for m in tail:
        who = {"user": "User", "assistant": "You", "system": "Note"}[m.role]
        lines.append(f"{who}: {m.content}")
    return "\n".join(lines)


# ── Just-in-time stage context ───────────────────────────────────────────────

def _run_summaries_for(workspace: Path, step_ids: list[str]) -> list[dict]:
    """Newest ``emit_run_summary`` payload per step, from the durable copies.

    Structured counters, never log scraping — that is the seam the blockers
    design depends on. Files are ``<data_dir>/_runs/{step_id}_{ts}.summary.json``.
    """
    proj = pipeline_bridge.build_project(workspace)
    runs_dir = Path(proj.data_dir) / "_runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for sid in step_ids:
        candidates = sorted(runs_dir.glob(f"{sid}_2*.summary.json"))
        if not candidates:
            continue
        try:
            out.append(json.loads(candidates[-1].read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _preamble(workspace: Path, stage_slug: str) -> str:
    """Rebuilt fresh EVERY turn, never stored, so it cannot go stale."""
    resolved = stages_svc.resolve_stages(workspace)
    stage = next((s for s in resolved if s["slug"] == stage_slug), None)
    if stage is None:
        return (
            "You are the research assistant for a Bird's Eye systematic "
            "review. Answer concisely and honestly."
        )

    counters = _run_summaries_for(workspace, stage["step_ids"])
    parts = [
        "You are the research assistant collaborating on a Bird's Eye "
        "systematic literature review. You have followed this review across "
        "its stages; your conversation history is your memory of what the "
        "user decided and why. Be concise, concrete, and honest about "
        "limitations — never invent counts or claim work happened that the "
        "data below does not show.",
        f"\n[current stage: {stage['title']} ({stage_slug})]",
        f"purpose: {stage['description']}" if stage.get("description") else "",
        f"artifact status: {stage['status']}",
    ]
    if stage.get("details"):
        parts.append("details: " + " | ".join(stage["details"]))
    if stage.get("config_keys"):
        parts.append("config this stage owns: " + ", ".join(stage["config_keys"]))
    if counters:
        parts.append(
            "latest run counters (structured, trustworthy):\n"
            + json.dumps(counters, indent=1)[:4000]
        )
    return "\n".join(p for p in parts if p)


async def _transition_row(
    db: AsyncSession, review_id: str, history: list[AgentMessage], stage_slug: str
) -> None:
    """Record navigation in the transcript, so the agent keeps the user's path
    without a paid greeting per page view (`/agent/enter` was cut for that)."""
    prev = history[-1].stage if history else None
    if prev != stage_slug:
        db.add(
            AgentMessage(
                review_id=review_id,
                stage=stage_slug,
                role="system",
                content=f"[user is now on stage: {stage_slug}]",
            )
        )


async def prior_review(
    db: AsyncSession, review_id: str, job_id: str
) -> AgentMessage | None:
    """The sanity-check row already written for *job_id*, if any.

    A direct lookup on ``meta.job_id`` rather than a scan of the recent
    transcript: the scan only looked at the last 200 messages, so on a long
    conversation an old run fell out of the window and was reviewed (and
    billed) a second time.
    """
    q = (
        select(AgentMessage)
        .where(
            AgentMessage.review_id == review_id,
            AgentMessage.role == "assistant",
            AgentMessage.meta["kind"].as_string() == "review",
            AgentMessage.meta["job_id"].as_string() == job_id,
        )
        .order_by(AgentMessage.created_at.asc())
        .limit(1)
    )
    return (await db.execute(q)).scalars().first()


# ── The two entry points ─────────────────────────────────────────────────────

async def ask(
    db: AsyncSession, review: Review, *, stage_slug: str, message: str
) -> AgentMessage:
    """One chat turn. Returns the persisted assistant row."""
    lock = _lock(review.id)
    if lock.locked():
        raise AgentBusy("The assistant is still working on the previous message.")
    async with lock:
        workspace = Path(review.workspace_path)
        history = await recent_messages(db, review.id)
        await _transition_row(db, review.id, history, stage_slug)

        provider, creds = await _resolve_provider(db, review)
        # Only the Claude CLI has a resumable session; OpenRouter is stateless, so
        # replay the transcript and never send a resume id there.
        session_id = _last_session_id(history) if provider == "claude_cli" else None
        preamble = _preamble(workspace, stage_slug)
        if session_id:
            # The CLI holds the full history; send only fresh context + message.
            prompt = f"{preamble}\n\nUser message:\n{message}"
        else:
            prompt = (
                f"{preamble}\n\nConversation so far:\n{_replay(history)}"
                f"\n\nUser message:\n{message}"
            )

        with _provider_env(provider, creds):
            resp = await _llm(
                prompt,
                identifier=f"agent:{review.id[:8]}",
                resume_session=session_id,
            )
        if not (resp.text or "").strip():
            # async_call_claude returns an EMPTY response on timeout/CLI error
            # rather than raising — surface it instead of storing a blank turn.
            raise AgentFailed(
                "The assistant did not answer (timeout or provider error). "
                "Your message was not lost — try again."
            )

        db.add(
            AgentMessage(
                review_id=review.id, stage=stage_slug, role="user", content=message
            )
        )
        assistant = AgentMessage(
            review_id=review.id,
            stage=stage_slug,
            role="assistant",
            content=resp.text,
            meta={
                "cost_usd": resp.cost_usd,
                "model": AGENT_MODEL,
                "provider": provider,
                "session_id": resp.session_id,
            },
        )
        db.add(assistant)
        await db.commit()
        await db.refresh(assistant)
        return assistant


_REVIEW_PROMPT = """\
Review the run counters above as a skeptical methodologist. Look for signs the
stage produced misleading or unusable output (e.g. everything filtered out,
zero of N papers with a needed field, a count wildly inconsistent with an
earlier stage you remember).

Respond with ONLY a JSON object, no prose, no code fences:
{"summary": "<one or two plain sentences on how the run looks>",
 "blockers": [{"severity": "high"|"medium", "code": "<snake_case>",
               "message": "<one sentence>", "evidence": "<the number(s)>",
               "suggested_fix": "<one sentence>"}]}
Use an empty blockers list when nothing rises to a real concern — do not
manufacture findings."""


def _parse_review(text: str) -> tuple[str, list[dict]]:
    """(summary, blockers). Malformed output yields ZERO blockers — a parse
    failure must never invent a halt."""
    raw = (text or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return raw[:500], []
    try:
        data = json.loads(m.group())
    except ValueError:
        return raw[:500], []
    blockers = []
    for b in data.get("blockers") or []:
        if not isinstance(b, dict) or not b.get("message"):
            continue
        blockers.append(
            {
                "severity": b.get("severity", "medium"),
                "code": str(b.get("code", "unspecified"))[:64],
                "message": str(b["message"])[:500],
                "evidence": str(b.get("evidence", ""))[:500],
                "suggested_fix": str(b.get("suggested_fix", ""))[:500],
            }
        )
    return str(data.get("summary", ""))[:1000], blockers


async def review_stage(
    db: AsyncSession, review: Review, *, stage_slug: str, job_id: str
) -> AgentMessage:
    """The auto sanity-check after a run. Advisory: it annotates, never halts.

    Idempotent per job: one run gets at most one review, keyed on ``job_id`` in
    message meta, so React strict-mode double-fires and reloads are harmless.
    Suppressed on approved stages — approval closes the question.
    """
    st = await db.get(StageState, (review.id, stage_slug))
    if st is not None and st.approved_at is not None:
        raise AgentBusy(  # semantically a 409, reuse the same HTTP mapping
            "This stage is already approved; approval closes the question. "
            "Withdraw the approval first if you want it re-reviewed."
        )

    prior = await prior_review(db, review.id, job_id)
    if prior is not None:
        return prior  # already reviewed this run — return it, don't redo it
    history = await recent_messages(db, review.id)

    lock = _lock(review.id)
    if lock.locked():
        raise AgentBusy("The assistant is busy; the check will run when free.")
    async with lock:
        workspace = Path(review.workspace_path)
        await _transition_row(db, review.id, history, stage_slug)
        provider, creds = await _resolve_provider(db, review)
        preamble = _preamble(workspace, stage_slug)
        # OpenRouter is stateless — replay the transcript, never resume.
        session_id = _last_session_id(history) if provider == "claude_cli" else None
        prompt = f"{preamble}\n\n{_REVIEW_PROMPT}"
        if not session_id:
            prompt = (
                f"{preamble}\n\nConversation so far:\n{_replay(history)}"
                f"\n\n{_REVIEW_PROMPT}"
            )

        with _provider_env(provider, creds):
            resp = await _llm(
                prompt,
                identifier=f"agent-review:{review.id[:8]}",
                resume_session=session_id,
            )
        if not (resp.text or "").strip():
            raise AgentFailed("The sanity check did not complete; try again.")

        summary, blockers = _parse_review(resp.text)

        if blockers:
            # Durable, Decision-shaped record beside the approval data. Assign a
            # NEW dict — SQLAlchemy JSON columns track reassignment, not mutation.
            if st is None:
                st = StageState(review_id=review.id, stage=stage_slug)
                db.add(st)
            existing = dict((st.decisions or {}).get("ai_blockers") or {})
            existing[job_id] = blockers
            st.decisions = {**(st.decisions or {}), "ai_blockers": existing}

        assistant = AgentMessage(
            review_id=review.id,
            stage=stage_slug,
            role="assistant",
            content=summary or "Reviewed the run; nothing stood out.",
            meta={
                "cost_usd": resp.cost_usd,
                "model": AGENT_MODEL,
                "provider": provider,
                "session_id": resp.session_id,
                "kind": "review",
                "job_id": job_id,
                "blockers": blockers,
            },
        )
        db.add(assistant)
        await db.commit()
        await db.refresh(assistant)
        return assistant


async def dismiss_blocker(
    db: AsyncSession,
    review: Review,
    *,
    stage_slug: str,
    code: str,
    reason: str,
    user_id: str,
) -> AgentMessage:
    """Record 'I know, proceed' — the one click that makes a collaborator's
    objection different from a gate. Written both to StageState.decisions (the
    durable Decision record) and as a system transcript row (so the rail can
    filter dismissed cards from messages alone)."""
    st = await db.get(StageState, (review.id, stage_slug))
    if st is None:
        st = StageState(review_id=review.id, stage=stage_slug)
        db.add(st)
    dismissed = list((st.decisions or {}).get("dismissed_blockers") or [])
    dismissed.append(
        {
            "code": code,
            "reason": reason,
            "by": user_id,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    st.decisions = {**(st.decisions or {}), "dismissed_blockers": dismissed}

    note = AgentMessage(
        review_id=review.id,
        stage=stage_slug,
        role="system",
        content=f"[user dismissed blocker {code}"
        + (f": {reason}]" if reason else "]"),
        meta={"kind": "dismiss", "code": code},
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note
