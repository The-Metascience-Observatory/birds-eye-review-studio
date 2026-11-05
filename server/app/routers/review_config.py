"""A review's configuration and its editable prompt templates.

Every config write goes through ``services.configuration.save_config`` — the
one validated, serialized write path (shared with the AI drafters), which is
also where the security rules for config keys live.
"""
from __future__ import annotations

from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Review, ReviewConfig
from ..deps import get_db, owned_review
from ..schemas import ConfigIn, ConfigOut, PromptIn, PromptOut
from ..services import workspace as ws
from ..services.configuration import save_config
from ..services.pipeline_bridge import (
    EDITABLE_PROMPTS,
    list_prompts,
    missing_placeholders,
    save_prompt,
)

router = APIRouter(prefix="/reviews/{review_id}", tags=["config"])


@router.get("/config", response_model=ConfigOut)
async def get_review_config(
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> ConfigOut:
    cfg = await db.get(ReviewConfig, review.id)
    # Normalize on READ only — reviews created before article_types was seeded
    # show the canonical list immediately, with no write and no migration. The
    # DB row is only updated if the user actually saves.
    return ConfigOut(
        config_json=ws.normalize_config(cfg.config_json if cfg else {})
    )


@router.put("/config", response_model=ConfigOut)
async def put_review_config(
    payload: ConfigIn,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> ConfigOut:
    cfg = await save_config(db, review, payload.config_json)
    return ConfigOut(config_json=cfg.config_json)


@router.patch("/config", response_model=ConfigOut)
async def patch_review_config(
    payload: ConfigIn,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> ConfigOut:
    """Merge a partial config into the stored one.

    Preferred over PUT for targeted edits: the merge happens server-side against
    the row as it is NOW, so an editor that only knows about one key can never
    drop another (and cannot revert a concurrent change it never fetched).
    """
    cfg = await save_config(db, review, payload.config_json, merge=True)
    return ConfigOut(config_json=cfg.config_json)


@router.get("/prompts", response_model=list[PromptOut])
async def get_prompts(review: Review = Depends(owned_review)) -> list[PromptOut]:
    """The editable classification prompts (this review's overrides, else defaults)."""
    prompts = await anyio.to_thread.run_sync(list_prompts, Path(review.workspace_path))
    return [PromptOut(**p) for p in prompts]


@router.put("/prompts/{name}", response_model=PromptOut)
async def put_prompt(
    name: str,
    payload: PromptIn,
    review: Review = Depends(owned_review),
) -> PromptOut:
    """Save a per-review prompt override; empty text reverts to the default."""
    if name not in EDITABLE_PROMPTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown prompt {name!r}")

    # A template that lost a placeholder still "works" — it just sends the model
    # a prompt with no abstract or no vocabulary. Catch it at save time.
    if payload.text and payload.text.strip():
        missing = missing_placeholders(name, payload.text)
        if missing:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Prompt is missing required placeholder(s): {', '.join(missing)}. "
                f"They are substituted at run time; without them the classifier "
                f"receives an incomplete prompt.",
            )
    return PromptOut(**save_prompt(Path(review.workspace_path), name, payload.text))
