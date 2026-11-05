"""Approval gates over the resolved wizard stages.

Deliberately SEPARATE from ``services/stages.py``, which stays a pure function of
the workspace with no database import — that is what lets ``test_stages.py``
exercise the registry with no DB at all. This module is the only place that joins
the filesystem view to ``StageState`` rows.

THREE AXES, NEVER FLATTENED
---------------------------
A stage carries three independent facts, and collapsing them into one enum
discards information the user needs:

* ``status`` — did the work run? Observed from artifacts on disk. Owned by
  ``stages.py``.
* ``gate`` — did a human sign off? Owned here.
* ``activity`` — is a job touching it right now? Owned by ``Job`` rows.

The common wizard state is precisely the one a flattened enum cannot express:
``status="done"`` AND ``gate="blocked"`` — the work ran, an upstream stage has
not been approved yet. The UI composes the three into a single pill; that is a
presentation decision and belongs in the client, not in this contract.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

# Stages that are pure UI checkpoints produce no artifacts, so an artifact check
# can never call them done. They are approvable on the user's say-so alone.
_NO_ARTIFACT_KINDS = frozenset({"setup", "curate", "review"})


def config_digest(config: dict, config_keys: Iterable[str]) -> str:
    """Hash the subtree of `config` that one stage actually depends on.

    Staleness compares THIS, not ``ReviewConfig.version``. The version bumps on
    every PATCH — including one whose merge result is identical — so a version
    comparison would mark stages stale on saves that changed nothing. A digest
    over the stage's own keys is immune to that, immune to edits touching only
    other stages' keys, and correctly reports edit-away-and-back as NOT stale.

    Dotted keys ("models.classification") address nested values. A key that is
    absent hashes as absent, so adding it later is correctly a change.
    """
    subtree: dict[str, Any] = {}
    for key in sorted(set(config_keys)):
        node: Any = config
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        subtree[key] = node
    blob = json.dumps(subtree, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def key_digests(config: dict, config_keys: Iterable[str]) -> dict[str, str]:
    """Per-key digests, so staleness can name WHICH key moved.

    Stored alongside the combined digest in ``StageState.decisions`` rather than
    a new column. Without this the UI can only say "something changed", which a
    user cannot act on; with it we can say "meta_analysis changed since you
    approved this stage".
    """
    return {k: config_digest(config, [k]) for k in sorted(set(config_keys))}


def _stale_keys(config: dict, config_keys: Iterable[str], stored: dict | None) -> list[str]:
    """Which of a stage's keys changed since approval.

    Returns [] when no per-key snapshot was stored (an approval predating this
    feature): reporting every key would be a guess dressed as a fact.
    """
    if not stored:
        return []
    now = key_digests(config, config_keys)
    return sorted(k for k, d in now.items() if stored.get(k) != d)


def apply_gates(
    resolved: list[dict],
    states: dict[str, Any],
    config: dict,
    *,
    active_jobs: dict[str, str] | None = None,
    last_job_states: dict[str, str] | None = None,
) -> list[dict]:
    """Attach ``gate`` and ``activity`` blocks to each resolved stage.

    ``states`` maps stage slug -> StageState (or any object with the same
    attributes). ``active_jobs`` maps stage slug -> running job id;
    ``last_job_states`` maps stage slug -> state of the most recent job that
    ran any of its steps.

    Mutates nothing: returns new dicts.
    """
    active_jobs = active_jobs or {}
    last_job_states = last_job_states or {}
    out: list[dict] = []
    # Walks in canonical execution order, tracking the first upstream stage that
    # has not been approved. Everything after it is blocked.
    blocking: str | None = None

    for stage in resolved:
        stage = dict(stage)
        slug = stage["slug"]

        # `_other` is a drift alarm, not a wizard step: it is ordered LAST
        # regardless of where its steps really sit in the pipeline, so including
        # it in the upstream walk would give the wrong answer for every stage.
        # It never gates and is never gated.
        if slug == "_other":
            stage["gate"] = None
            stage["activity"] = {"running_job_id": None, "last_job_state": None}
            out.append(stage)
            continue

        st = states.get(slug)
        approved = st is not None and getattr(st, "approved_at", None) is not None

        if not stage.get("requires_approval", True) or stage.get("skipped"):
            gate = None
        else:
            stored = getattr(st, "stage_config_digest", None) if st else None
            stored_keys = (
                (getattr(st, "decisions", None) or {}).get("key_digests") if st else None
            )
            current = config_digest(config, stage.get("config_keys") or [])
            if approved and stored is not None and stored != current:
                state = "stale"
            elif approved:
                state = "approved"
            elif blocking is not None:
                state = "blocked"
            else:
                state = "open"
            gate = {
                "state": state,
                "approved_at": _iso(getattr(st, "approved_at", None)) if st else None,
                "approved_by": getattr(st, "approved_by", None) if st else None,
                "approved_config_version": (
                    getattr(st, "approved_config_version", None) if st else None
                ),
                "blocked_by": blocking if state == "blocked" else None,
                "stale_keys": (
                    _stale_keys(config, stage.get("config_keys") or [], stored_keys)
                    if state == "stale"
                    else []
                ),
            }

        stage["gate"] = gate
        stage["activity"] = {
            "running_job_id": active_jobs.get(slug),
            "last_job_state": last_job_states.get(slug),
        }
        out.append(stage)

        # A skipped stage has no work, so it cannot block. An approval-free stage
        # cannot block either. Otherwise, the first unapproved stage blocks all
        # that follow.
        if (
            blocking is None
            and not stage.get("skipped")
            and stage.get("requires_approval", True)
            and not approved
        ):
            blocking = slug

    return out


def can_run(stage: dict) -> tuple[bool, str | None]:
    """(allowed, reason). The gate the run endpoint enforces with a 409."""
    gate = stage.get("gate")
    if gate and gate["state"] == "blocked":
        return False, (
            f"{stage['title']!r} is blocked: {gate['blocked_by']!r} has not been "
            f"approved yet. Approve it first, or approve the stages in order."
        )
    return True, None


def can_approve(stage: dict) -> tuple[bool, str | None]:
    """(allowed, reason).

    Approving a stage that never ran is meaningless, so artifact evidence is
    required — EXCEPT for stages that produce no artifacts by design (setup,
    curation, review checkpoints), where the user's judgement is the only
    evidence there is.
    """
    if stage.get("skipped"):
        return False, f"{stage['title']!r} is skipped for this review."
    if not stage.get("requires_approval", True):
        return False, f"{stage['title']!r} does not require approval."
    if stage.get("kind") in _NO_ARTIFACT_KINDS:
        return True, None
    if stage.get("status") in ("pending", "n/a"):
        return False, (
            f"{stage['title']!r} has not produced any output yet — run it before "
            f"approving."
        )
    return True, None


def _is_background(stage: dict) -> bool:
    """A stage the wizard should not land on.

    `hidden` stages are never a landing target — they have no page at all.
    `autorun` (but visible) stages are pure fetches with nothing to configure;
    landing on one means watching a progress bar instead of doing the next real
    piece of work. They still run (auto-started) and still gate; their progress
    shows on whatever page the user IS on. Only skip a visible autorun stage once
    it is under way or finished, so a failure to start still surfaces a page the
    user can act on.
    """
    if stage.get("hidden"):
        return True
    return bool(stage.get("autorun")) and stage.get("status") in ("done", "partial")


def next_stage(resolved: list[dict]) -> str | None:
    """Where the wizard should land, once approvals exist.

    Sharper than artifacts alone: a stage can be finished on disk and still be
    waiting for a human, which is exactly the state the wizard exists to surface.
    So this returns the first stage that is either unfinished OR unapproved,
    rather than the first unfinished one.
    """
    for stage in resolved:
        if stage["slug"] == "_other" or stage.get("skipped"):
            continue
        if _is_background(stage):
            continue
        if stage["status"] not in ("done", "n/a"):
            return stage["slug"]
        gate = stage.get("gate")
        if gate and gate["state"] in ("open", "blocked", "stale"):
            return stage["slug"]
    real = [s for s in resolved if s["slug"] != "_other"
            and not s.get("skipped") and not s.get("hidden")]
    return real[-1]["slug"] if real else None


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None
