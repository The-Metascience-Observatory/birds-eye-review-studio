"""One validated, serialized configuration write path for UI and AI drafts.

SECURITY. A review's config is not just data: the pipeline turns some of its
keys into filesystem paths and even into programs to run. Anything a review
owner can set through the API must therefore be one of:

* an ordinary setting (vocabularies, queries, model choices, toggles), or
* ``dirs.base_pdf_dir`` — the one user-supplied path, which is validated and
  normalized by ``pdf_dir.validate_pdf_dir`` (see below).

Everything else that names a path or a program is OPERATOR-ONLY — see
:data:`OPERATOR_ONLY_KEYS`. ``extra_steps`` in particular makes the pipeline run
config-declared scripts as the server user: accepting it from a client would be
remote code execution.
"""
from copy import deepcopy

from fastapi import HTTPException, status
from sqlalchemy import select, update

from ..db.models import Job, Review, ReviewConfig
from . import pdf_dir
from . import workspace as ws

#: Top-level keys the API never lets a client add or change. An operator may
#: still put them in a workspace's config.yaml by hand (``extra_steps`` is then
#: honored only with ALLOW_CONFIG_EXTRA_STEPS — see pipeline_bridge).
#:   extra_steps  scripts the pipeline runs (pipeline.extra_steps) — RCE
#:   prompts_dir  a directory prompt templates are loaded from
OPERATOR_ONLY_KEYS = frozenset({"extra_steps", "prompts_dir"})

#: The only ``dirs.*`` key a client may set. The rest (eligible_trials,
#: screening_dirs, papers_dir, observational_treatment, …) become read/write
#: paths for pipeline steps and are never validated, so they are operator-only.
CLIENT_DIRS_KEYS = frozenset({"base_pdf_dir"})

#: Keys that may change while a job is RUNNING on the review. A running step
#: read config.yaml when it started, so changing a key it reads mid-run leaves
#: its outputs inconsistent with the config the UI shows — hence the 409. These
#: keys are read by no pipeline script a job runs (dashboard layout is studio
#: display state; ``synthesis`` is snapshotted into a synthesis job's params at
#: enqueue), or are applied live by the evidence table rather than by the job
#: (``include_animal_studies`` — the next full-text screen picks it up).
SAFE_DURING_RUN_KEYS = frozenset({"dashboard", "include_animal_studies", "synthesis"})


def _merge_config(base: dict, patch: dict) -> dict:
    """Shallow merge, one level deep into nested dicts. ``None`` deletes a key.

    Depth stops at 2 on purpose: ``{"topic": {"not_about": [...]}}`` must keep
    ``topic.name``, but merging deeper makes list-vs-dict semantics ambiguous
    (does a patched list append or replace?). Lists always replace.
    """
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            nested = dict(out[key])
            for k2, v2 in value.items():
                if v2 is None:
                    nested.pop(k2, None)
                else:
                    nested[k2] = v2
            out[key] = nested
        else:
            out[key] = value
    return out


def _reject_operator_only(result: dict, current: dict) -> None:
    """422 when a write ADDS or CHANGES an operator-only key.

    Compared against the stored config rather than rejected on sight: a review
    whose config an operator seeded with, say, ``dirs.eligible_trials`` must
    stay editable — every PATCH carries that key through unchanged. Removing
    one is allowed too. Only a client-supplied new value is refused.
    """
    for key in OPERATOR_ONLY_KEYS:
        if key in result and result.get(key) != current.get(key):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"`{key}` cannot be set through the API — it controls what the "
                "server executes or reads from disk. An operator can set it in "
                "the review's config.yaml.",
            )
    new_dirs = result.get("dirs") or {}
    old_dirs = current.get("dirs") or {}
    if not isinstance(new_dirs, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "`dirs` must be a mapping.")
    for key, value in new_dirs.items():
        if key in CLIENT_DIRS_KEYS:
            continue
        if not isinstance(old_dirs, dict) or old_dirs.get(key) != value:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"`dirs.{key}` cannot be set through the API. Only "
                "`dirs.base_pdf_dir` (the PDF download folder) is configurable.",
            )


def _validate_config(cfg: dict, *, user_id: str | None = None) -> None:
    """Reject configs that would silently break classification. 422 on failure.

    Also NORMALIZES ``dirs.base_pdf_dir`` in place (see the security note below).
    The caller passes the exact dict it goes on to persist, so mutating here is
    what keeps "whatever is checked is what is stored" true for that key.
    """
    types = [str(t).strip() for t in (cfg.get("article_types") or []) if str(t).strip()]
    if not types:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "`article_types` cannot be empty — the classifier renders it into "
            "its prompt, so an empty list makes the model invent labels that "
            "every downstream step rejects.",
        )

    pregate = (cfg.get("classification") or {}).get("pregate") or {}
    if pregate.get("enabled", True):
        has_synonyms = any(
            str(s).strip() for s in ((cfg.get("topic") or {}).get("synonyms") or [])
        )
        has_extra = any(str(t).strip() for t in (pregate.get("extra_terms") or []))
        if not has_synonyms and not has_extra:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The pre-gate is enabled but has no terms to gate on — no topic "
                "synonyms and no extra terms. Add terms, or disable the pre-gate.",
            )

    # SECURITY: `dirs.base_pdf_dir` is the one user-supplied filesystem path in
    # this config. It reaches `download_pdfs.py --output-dir` (which does
    # mkdir(parents=True) and writes as the server user), so an unchecked value
    # like `~/.ssh` or `/root` is an arbitrary-directory write. Run it through
    # the SAME validator that guards the account-level setting in
    # `routers/auth.py::update_settings` — one policy, one place
    # (`services/pdf_dir.py`), applied at every boundary that accepts a path.
    #
    # STORE WHAT WAS VALIDATED. `validate_pdf_dir` returns the expanded, quote-
    # stripped, WSL-translated, resolved absolute path, and that is the value the
    # pipeline must receive. Discarding it and persisting the raw string made the
    # two diverge: `~/pdfs` passed validation as `/home/<user>/pdfs` but reached
    # `paper_store.papers_root` as the literal `~/pdfs`, which is RELATIVE — the
    # store was created under `<repo>/~/pdfs`.
    base_pdf_dir = str((cfg.get("dirs") or {}).get("base_pdf_dir") or "").strip()
    if base_pdf_dir:
        try:
            checked = pdf_dir.validate_pdf_dir(base_pdf_dir, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
            ) from exc
        assert checked is not None  # non-empty input always yields a result
        cfg["dirs"]["base_pdf_dir"] = checked.path


def _changed_keys(current: dict, result: dict) -> set[str]:
    return {k for k in set(current) | set(result) if current.get(k) != result.get(k)}


async def _save_config(db, review, values, *, merge=False, expected_version=None):
    # Serialize writers before reading, on both SQLite and PostgreSQL.
    await db.execute(update(Review).where(Review.id == review.id).values(updated_at=Review.updated_at))
    cfg = await db.get(ReviewConfig, review.id, populate_existing=True)
    if expected_version is not None and (cfg.version if cfg else 0) != expected_version:
        raise HTTPException(409, "Configuration changed while generating the draft; regenerate against the latest version.")
    current = ws.normalize_config(cfg.config_json if cfg else {})
    result = _merge_config(current, deepcopy(values)) if merge else deepcopy(values)
    old = ((current.get("topic") or {}).get("name") or "").strip()
    new = ((result.get("topic") or {}).get("name") or "").strip()
    if old and old != new:
        raise HTTPException(422, "`topic.name` cannot be changed here; renaming would orphan existing data; a workspace migration is required.")

    changed = _changed_keys(current, result)
    if changed - SAFE_DURING_RUN_KEYS:
        running = (await db.execute(select(Job.id).where(
            Job.review_id == review.id, Job.state == "running",
            Job.kind != "metadata").limit(1))).scalar()
        if running:
            raise HTTPException(409, "Wait for the running job to finish or stop it before changing configuration.")

    _reject_operator_only(result, current)
    _validate_config(result, user_id=review.owner_id)
    if cfg is None:
        cfg = ReviewConfig(review_id=review.id, config_json=result)
        db.add(cfg)
    else:
        cfg.config_json = result
        cfg.version = (cfg.version or 1) + 1
    await db.commit()
    ws.create_review_workspace(review.id, result)
    return cfg


async def save_config(db, review, values, **kwargs):
    from ber_core.bundles import directory_lock
    target = ws.workspace_dir(review.id) / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = directory_lock(target)
    try:
        lock.__enter__()
    except ValueError as exc:  # the lock is held: another save is in flight
        raise HTTPException(
            409, "Another change to this review's configuration is being saved; "
                 "try again in a moment.") from exc
    try:
        return await _save_config(db, review, values, **kwargs)
    finally:
        lock.__exit__(None, None, None)
