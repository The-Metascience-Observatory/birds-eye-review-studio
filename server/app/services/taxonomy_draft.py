"""AI drafting of the extraction taxonomies (intervention_categories +
outcome_domains) for a review.

Pass-2 extraction renders these two vocabularies into its prompt; empty, every
intervention / outcome lands in one meaningless "other" bucket (and the extract
preflight blocks the run). A new review starts with them empty, so this drafts
sensible topic-specific values with the selected LLM provider (Codex by default) — and persists them into config.yaml +
ReviewConfig. Mirrors ``search.draft_and_save``.

The CLI call is blocking, so it runs in a worker thread (``anyio.to_thread``).
"""
from __future__ import annotations

import re

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Review, ReviewConfig
from . import llm_prefs
from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()
from .search import DraftFailed

# The two taxonomies this drafts. Slugs are lowercase_snake_case so they match the
# enum values the extractor is asked to copy verbatim.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_category(s: str) -> str:
    s = _SLUG_RE.sub("_", str(s).strip().lower()).strip("_")
    return s


_PROMPT = """You are setting up a systematic review / meta-analysis of: {topic_name}.

{context}

Produce TWO controlled vocabularies used to classify every study's data:

1. intervention_categories — the distinct KINDS of intervention/treatment this
   review will compare. Group by mechanism or modality, not brand name. Include a
   category for the control/comparator if relevant, and always end with "other".
   6-12 items.

2. outcome_domains — the distinct FAMILIES of outcome these studies measure
   (group specific instruments into a domain, e.g. many sleep scales -> one
   "sleep_quality" domain). 5-10 items.

Each value MUST be a short lowercase_snake_case slug (letters, numbers,
underscores only). Be specific to THIS topic — do not return generic placeholders.

Respond with ONLY a JSON object, no other text:
{{"intervention_categories": ["...", "..."], "outcome_domains": ["...", "..."]}}"""


def _draft_blocking(
    *,
    topic_name: str,
    description: str,
    question: str,
    not_about: list[str],
    model: str,
    provider: str = llm_prefs.DEFAULT_PROVIDER,
    creds: dict[str, str] | None = None,
) -> dict:
    """Run the (blocking) taxonomy drafter under the chosen provider. In a thread.

    The provider + credential are scoped with ``llm_prefs.provider_env``
    (contextvars, never ``os.environ``), so concurrent drafts for different
    owners cannot see each other's credential.
    """
    from ber_core.claude_cli import call_claude, parse_json_response  # noqa: E402

    ctx_lines = []
    if question:
        ctx_lines.append(f"Research question: {question}")
    if description:
        ctx_lines.append(f"Description: {description}")
    if not_about:
        ctx_lines.append("Not about: " + "; ".join(not_about))
    context = "\n".join(ctx_lines) or "(no further description provided)"

    prompt = _PROMPT.format(topic_name=topic_name, context=context)
    with llm_prefs.provider_env(provider, creds):
        resp = call_claude(prompt, model, "taxonomy_draft", timeout=120, use_stdin=True)
    parsed = parse_json_response(resp.text, "taxonomy_draft")
    if not isinstance(parsed, dict):
        raise DraftFailed("The model did not return a JSON object of taxonomies.")

    def _clean(key: str) -> list[str]:
        raw = parsed.get(key) or []
        if not isinstance(raw, list):
            return []
        seen: list[str] = []
        for item in raw:
            slug = _slugify_category(item)
            if slug and slug not in seen:
                seen.append(slug)
        return seen

    return {
        "intervention_categories": _clean("intervention_categories"),
        "outcome_domains": _clean("outcome_domains"),
        "provider": provider,
    }


async def draft_and_save(
    db: AsyncSession, review: Review, *, model: str = "sonnet"
) -> dict:
    """Draft intervention_categories + outcome_domains with the LLM and persist.

    Only EMPTY vocabularies are written: a list the user filled in by hand is
    never overwritten because its sibling was empty. (It used to replace both,
    so "generate the missing one" silently discarded the user's edits to the
    other.) When NEITHER is empty the call is an explicit Regenerate from the
    Extract page, and both are redrafted.
    Returns ``{intervention_categories, outcome_domains, provider}``.
    """
    cfg_row = await db.get(ReviewConfig, review.id)
    config_json = dict(cfg_row.config_json) if cfg_row and cfg_row.config_json else {}
    expected_version = cfg_row.version if cfg_row else 0
    topic = dict(config_json.get("topic") or {})
    await db.commit()  # do not hold a read transaction during a model call

    provider, creds = await llm_prefs.resolve_for_run(db, review)
    await db.commit()

    result = await anyio.to_thread.run_sync(
        lambda: _draft_blocking(
            topic_name=topic.get("name") or review.topic_name,
            description=topic.get("description") or "",
            question=(topic.get("question") or topic.get("description") or ""),
            not_about=list(topic.get("not_about") or []),
            model=model,
            provider=provider,
            creds=creds,
        )
    )

    keys = ("intervention_categories", "outcome_domains")
    empty = [k for k in keys
             if not [v for v in (config_json.get(k) or []) if str(v).strip()]]
    targets = empty or list(keys)  # nothing empty => an explicit full regenerate

    drafted = {k: result[k] for k in targets if result[k]}
    # Nothing usable for any target is a failure — surfaced by the router's 502.
    if not drafted:
        raise DraftFailed(
            "The model returned no categories. Check the topic description, or "
            "enter the categories manually.")
    config_json.update(drafted)

    from .configuration import save_config
    await save_config(db, review, config_json, expected_version=expected_version)
    return {
        "intervention_categories": config_json.get("intervention_categories", []),
        "outcome_domains": config_json.get("outcome_domains", []),
        "provider": provider,
    }
