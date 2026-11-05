"""The locally-built dashboard bundle, its meta-analysis, and the narrative
review document synthesized from it."""
from __future__ import annotations

from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job, Review, ReviewConfig, User
from ..deps import get_current_user, get_db, owned_review
from ..ratelimit import llm_limit
from ..schemas import JobOut, MetaAnalysisOut, ReviewDocumentOut, SynthesizeIn
from ..services import bundle as bundle_svc
from ..services import jobs as jobs_svc
from ..services import llm_access
from ..services import meta_bundle as meta_bundle_svc
from ..services import review_doc as review_doc_svc
from ..services import workspace as ws

router = APIRouter(prefix="/reviews/{review_id}", tags=["dashboard"])

#: Largest page /dashboard/rows will return in one call.
MAX_ROWS_PAGE = 5000


@router.post("/synthesize-review", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(llm_limit)])
async def synthesize_review(
    body: SynthesizeIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Kick off the AI narrative-review synthesis as a background LLM job.

    NOT exempt from the one-job guard: the script reads the bundle files a
    pipeline run rewrites, and the run is user-initiated, so a 409 while other
    work is active is the honest answer. Options in the body override the
    config's ``synthesis`` block; the merged result rides Job.params so the job
    record says what it actually ran with.
    """
    if not bundle_svc.bundle_state(Path(review.workspace_path))["exists"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No dashboard has been built for this review yet — the synthesis "
            "reads the built bundle. Open the 'View dashboard' stage first.")
    # Synthesis is NOT exempt from the one-job guard, so an active job of any
    # non-background kind blocks it — EXCEPT an active synthesis, where the
    # idempotent answer is that job itself (a double-click must not 409).
    existing = await jobs_svc.active_job(db, review.id)
    if existing is not None:
        if existing.kind == "synthesis":
            return existing
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This review already has a {existing.state} job ({existing.id}). "
            "Wait for it to finish before synthesizing — the synthesis reads "
            "the files that job may be rewriting.")
    await llm_access.ensure_allowed(db, review, requester=user)

    cfg_row = await db.get(ReviewConfig, review.id)
    cfg = ws.normalize_config(cfg_row.config_json if cfg_row else {})
    syn_cfg = cfg.get("synthesis") if isinstance(cfg.get("synthesis"), dict) else {}

    depth = body.depth or syn_cfg.get("depth") or "standard"
    audience = body.audience or syn_cfg.get("audience") or "clinician"
    model = body.model or (cfg.get("models") or {}).get("synthesis") or "auto"
    sections = (body.sections if body.sections is not None
                else syn_cfg.get("sections"))

    # Validate against the literals synthesize_review.py owns — a bad value
    # would otherwise only surface as an argparse exit deep in a worker log.
    if depth not in review_doc_svc.DEPTHS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"depth must be one of {review_doc_svc.DEPTHS}")
    if audience not in review_doc_svc.AUDIENCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"audience must be one of {review_doc_svc.AUDIENCES}")
    if model not in review_doc_svc.MODELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"model must be one of {review_doc_svc.MODELS}")
    if sections is not None:
        unknown = [s for s in sections if s not in review_doc_svc.SECTION_IDS]
        if unknown or not sections:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"unknown section id(s): {unknown}" if unknown
                else "sections must not be empty")

    params: dict = {"depth": depth, "audience": audience, "model": model}
    if sections is not None:
        params["sections"] = list(sections)
    return await jobs_svc.enqueue(db, review, "synthesis", params=params)


@router.get("/review-document", response_model=ReviewDocumentOut)
async def get_review_document(review: Review = Depends(owned_review)) -> dict:
    """The synthesized narrative document + metadata (exists:false when absent)."""
    return await anyio.to_thread.run_sync(
        review_doc_svc.document_state, Path(review.workspace_path))


@router.get("/dashboard/summary")
async def get_dashboard_summary(review: Review = Depends(owned_review)) -> dict:
    """The locally-built dashboard: precomputed aggregates + the panel plan.

    Everything here was computed once by the pipeline (see dashboard_summary.py)
    and is passed through untouched. The studio must never re-derive a count, or
    it could disagree with the published dashboard.
    """
    ws_path = Path(review.workspace_path)
    state = bundle_svc.bundle_state(ws_path)
    if not state["exists"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No dashboard has been built for this review yet. "
                   "Open the 'View dashboard' stage and it will build "
                   "automatically.")
    return {
        "state": state,
        "summary": bundle_svc.read_summary(ws_path),
        "manifest": bundle_svc.read_manifest(ws_path),
    }


@router.get("/dashboard/rows")
async def get_dashboard_rows(
    offset: int = 0,
    limit: int = MAX_ROWS_PAGE,
    review: Review = Depends(owned_review),
) -> dict:
    """The canonical per-trial rows the client filters and reduces.

    ``limit`` is clamped to 1..MAX_ROWS_PAGE and ``offset`` to >= 0: an
    unbounded ``limit`` let one request parse and serialize an arbitrarily
    large row file in a single response.
    """
    offset = max(0, offset)
    limit = max(1, min(limit, MAX_ROWS_PAGE))
    rows = await anyio.to_thread.run_sync(
        lambda: bundle_svc.read_rows(Path(review.workspace_path), offset=offset, limit=limit))
    return {"rows": rows, "offset": offset, "count": len(rows)}


@router.get("/dashboard/state")
async def get_dashboard_state(review: Review = Depends(owned_review)) -> dict:
    """Cheap check for whether a dashboard exists and is current."""
    return bundle_svc.bundle_state(Path(review.workspace_path))


@router.get("/meta-analysis", response_model=MetaAnalysisOut)
async def get_meta_analysis(review: Review = Depends(owned_review)) -> MetaAnalysisOut:
    """The pooled meta-analysis (forest-plot groups) from the built bundle.

    Read from `meta_analysis.json`, which only the meta-analysis build step
    writes. Returns empty groups when a review has no pooling — the dashboard
    then renders no forest-plot section. Owner-only, ungated.
    """
    data = await anyio.to_thread.run_sync(meta_bundle_svc.read_meta, Path(review.workspace_path))
    return MetaAnalysisOut(**data)
