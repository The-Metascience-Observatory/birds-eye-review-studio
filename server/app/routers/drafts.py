"""AI-drafted configuration: search queries and extraction vocabularies.

Both run an LLM call IN-PROCESS on the review owner's provider, so both go
through the same access check (``llm_access``) and per-user rate limit as every
other endpoint that spends model money.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Review, User
from ..deps import get_current_user, get_db, owned_review
from ..errors import public_detail
from ..ratelimit import llm_limit
from ..schemas import SearchDraftIn, SearchDraftOut, TaxonomyDraftOut
from ..services import llm_access
from ..services import search as search_svc
from ..services import taxonomy_draft as taxonomy_draft_svc

router = APIRouter(prefix="/reviews/{review_id}", tags=["drafts"],
                   dependencies=[Depends(llm_limit)])


@router.post("/search/draft", response_model=SearchDraftOut)
async def draft_search_queries(
    payload: SearchDraftIn | None = None,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchDraftOut:
    """Draft per-source literature-search queries with the owner's LLM provider
    and save them into the review's config (so the search step can run)."""
    await llm_access.ensure_allowed(db, review, requester=user)
    model = (payload.model if payload else None) or "sonnet"
    try:
        result = await search_svc.draft_and_save(db, review, model=model)
    except HTTPException:
        raise
    except search_svc.DraftFailed as exc:  # a message written for the user
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except Exception as exc:  # surface a clean message to the studio
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, public_detail(exc, "Search-query drafting"),
        ) from exc
    return SearchDraftOut(**result)


@router.post("/taxonomies/draft", response_model=TaxonomyDraftOut)
async def draft_taxonomies(
    payload: SearchDraftIn | None = None,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyDraftOut:
    """AI-draft the EMPTY extraction vocabularies (intervention_categories /
    outcome_domains) from the topic and save them into config, so pass-2
    extraction has real buckets instead of one "other". A vocabulary the user
    already filled in is never overwritten."""
    await llm_access.ensure_allowed(db, review, requester=user)
    model = (payload.model if payload else None) or "sonnet"
    try:
        result = await taxonomy_draft_svc.draft_and_save(db, review, model=model)
    except HTTPException:
        raise
    except search_svc.DraftFailed as exc:  # a message written for the user
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except Exception as exc:  # surface a clean message to the studio
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, public_detail(exc, "Taxonomy drafting"),
        ) from exc
    return TaxonomyDraftOut(**result)
