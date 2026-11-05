"""AI search-query drafting for a review.

Wraps the pipeline's ``ber_core.llm_search.generate_search_queries`` (which calls the selected LLM provider) so the studio can turn a review's topic/description/
synonyms/not_about into per-source literature-search queries, then persists them
into the review's ``config.yaml`` + ``ReviewConfig`` so the search step can run.

The CLI call is blocking, so it runs in a worker thread (``anyio.to_thread``)
rather than on the event loop.
"""
from __future__ import annotations


import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Review, ReviewConfig
from . import llm_prefs
from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()


class DraftFailed(ValueError):
    """A draft that produced nothing usable. The message is written for the
    user (it is shown verbatim, even on a public instance)."""

# The per-source query keys llm_search emits == the config `search:` block keys.
SOURCES = (
    "pubmed",
    "openalex_title",
    "openalex_abstract",
    "europepmc",
    "semantic_scholar",
    "cyberleninka",
)


def _draft_blocking(
    *,
    topic_name: str,
    description: str,
    synonyms: list[str],
    mesh_terms: list[str],
    not_about: list[str],
    question: str,
    model: str,
    provider: str = llm_prefs.DEFAULT_PROVIDER,
    creds: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """Run the (blocking) query drafter under the chosen provider. In a thread.

    The provider + credential are scoped with ``llm_prefs.provider_env``
    (contextvars, never ``os.environ``), so concurrent drafts for different
    owners cannot see each other's credential.
    """
    from ber_core.llm_search import generate_search_queries  # noqa: E402

    with llm_prefs.provider_env(provider, creds):
        return generate_search_queries(
            topic_name=topic_name,
            description=description,
            synonyms=synonyms,
            mesh_terms=mesh_terms,
            not_about=not_about,
            question=question,
            model=model,
        )


async def draft_and_save(
    db: AsyncSession, review: Review, *, model: str = "sonnet"
) -> dict:
    """Draft per-source search queries with the selected LLM and persist them.

    Writes the non-empty query lists into ``config_json['search']``, re-renders
    ``config.yaml``, bumps ``ReviewConfig.version``, and returns
    ``{queries, meta, provider}``.
    """
    cfg_row = await db.get(ReviewConfig, review.id)
    config_json = dict(cfg_row.config_json) if cfg_row and cfg_row.config_json else {}
    expected_version = cfg_row.version if cfg_row else 0
    topic = dict(config_json.get("topic") or {})
    await db.commit()  # do not hold a read transaction during a model call

    provider, creds = await llm_prefs.resolve_for_run(db, review)
    await db.commit()

    queries, meta = await anyio.to_thread.run_sync(
        lambda: _draft_blocking(
            topic_name=topic.get("name") or review.topic_name,
            description=topic.get("description") or "",
            synonyms=list(topic.get("synonyms") or []),
            mesh_terms=list(topic.get("mesh_terms") or []),
            not_about=list(topic.get("not_about") or []),
            # The plain-language question leads the prompt; older reviews with
            # no question fall back to the description, so nothing regresses.
            question=(topic.get("question") or topic.get("description") or ""),
            model=model,
            provider=provider,
            creds=creds,
        )
    )

    # Keep only sources that produced at least one query.
    search_block = {src: queries.get(src, []) for src in SOURCES if queries.get(src)}

    # An empty result is a FAILURE, not a success. It used to be persisted
    # silently: the request returned 200, the config gained an empty `search`
    # block, and the UI's "generate terms first" guard stayed on — so the page
    # looked identical to "nothing happened". Raising lets the router's 502
    # wrapper surface a real message the user can act on.
    if not search_block:
        raise DraftFailed(
            "The model returned no search queries for any source. Check the "
            "topic description and synonyms, or enter queries manually.")

    config_json["search"] = search_block

    from .configuration import save_config
    await save_config(db, review, config_json, expected_version=expected_version)
    return {"queries": search_block, "meta": meta, "provider": provider}
