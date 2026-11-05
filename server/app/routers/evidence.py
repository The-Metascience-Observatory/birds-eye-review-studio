"""The live evidence table, its per-paper detail reads, and its background jobs.

Everything here reads straight from the review's workspace, ungated, so rows
surface WHILE the jobs that produce them run. All file-system work runs in a
worker thread: ``build_evidence`` re-reads every artifact of a review, and on a
large corpus doing that on the event loop stalled every other request.
"""
from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.models import Job, PdfUpload, Review, User
from ..deps import get_current_user, get_db, owned_review
from ..schemas import EvidenceExcludeIn
from ..services import evidence as evidence_svc
from ..services import feasibility as feasibility_svc
from ..services import jobs as jobs_svc
from ..services import llm_access
from ..services import paper_metadata as metadata_svc
from ..services import prisma_svc

router = APIRouter(prefix="/reviews/{review_id}", tags=["evidence"])
logger = logging.getLogger(__name__)


@router.get("/evidence")
async def get_evidence(
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The live evidence table: one row per paper, whatever exists so far.

    Never touches the bundle build (which only exists after publish) — the whole
    point is to show progress mid-pipeline.
    """
    metadata = await metadata_svc.load_metadata(db, review.id)
    try:
        return await anyio.to_thread.run_sync(partial(
            evidence_svc.build_evidence, Path(review.workspace_path), metadata=metadata))
    except evidence_svc.CorruptVerdictFile as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/evidence/abstract")
async def get_paper_abstract(doi: str, review: Review = Depends(owned_review)) -> dict:
    """One paper's abstract text, read on demand for the detail panel.

    Kept out of the evidence rows (too large per row); fetched when a paper is
    opened. `{abstract: null}` when there's no abstract file for this DOI.
    """
    text = await anyio.to_thread.run_sync(
        evidence_svc.paper_abstract, Path(review.workspace_path), doi)
    return {"abstract": text}


@router.get("/evidence/extraction")
async def get_paper_extraction(doi: str, review: Review = Depends(owned_review)) -> dict:
    """The full pass-2 extraction record(s) for one paper — every outcome,
    per-arm result, verbatim quote, and the `_provenance` verification block.
    Too large for the evidence rows; the detail panel fetches it on demand.
    404 when the paper has no ok extraction record."""
    records = await anyio.to_thread.run_sync(
        evidence_svc.paper_extraction, Path(review.workspace_path), doi)
    if not records:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No extraction record for this paper")
    return {"records": records}


@router.get("/evidence/pdf")
async def get_paper_pdf(doi: str, review: Review = Depends(owned_review)) -> FileResponse:
    """Serve the downloaded PDF for one paper, inline, so the detail panel can
    open it in a new tab. 404 when no PDF was downloaded for this DOI."""
    path = await anyio.to_thread.run_sync(
        evidence_svc.paper_pdf_path, Path(review.workspace_path), doi)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No PDF for this paper")
    # `inline` (not attachment) so the browser renders it in the new tab.
    return FileResponse(
        path,
        media_type="application/pdf",
        content_disposition_type="inline",
        filename=path.name,
    )


@router.get("/evidence/authors")
async def get_paper_authors(
    doi: str,
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Full author names for one paper, expanding initials on demand.

    The evidence row already carries the best stored author string; this returns
    it unchanged when it has full first names, and otherwise fetches the full
    names from the DOI (mirroring the on-demand abstract fetch) and caches the
    result so a re-open is instant. The blocking network fetch runs off the event
    loop. Returns `{authors, expanded}`.
    """
    metadata = await metadata_svc.load_metadata(db, review.id)
    result = await anyio.to_thread.run_sync(
        partial(
            evidence_svc.paper_authors_full,
            Path(review.workspace_path),
            doi,
            metadata=metadata,
        )
    )
    if result.get("expanded") and result.get("authors"):
        await metadata_svc.cache_authors(db, review.id, doi, result["authors"])
    return result


async def _restage_after_manual_verdict(
    db: AsyncSession, review: Review, user: User
) -> Job | None:
    """Re-run staging so a manual include/exclude actually reaches extraction.

    `user_inclusions.json` / `user_exclusions.json` are honored by
    `stage_eligible.py`, but only when it next RUNS. Writing the file alone left
    the eligible_*/ buckets stale, so a force-included paper never reached pass 2
    and a manually excluded one was still extracted. Firing the (idempotent,
    resumable, one-job-guard-exempt) full-text screen here is the same idiom
    `upload_pdf` uses for a state change that invalidates a background artifact.

    Best-effort: the verdict is already durably on disk, so neither a queue
    problem nor an LLM-access refusal (the shared CLI is someone else's right
    now) may fail the user's click. The next screen run picks the change up.
    """
    try:
        await llm_access.ensure_allowed(db, review, requester=user)
        return await jobs_svc.enqueue(db, review, "fulltext_screen")
    except HTTPException as exc:
        logger.info("re-stage for review %s not enqueued: %s", review.id, exc.detail)
        return None
    except Exception:
        logger.warning("re-stage enqueue failed for review %s; the manual verdict "
                       "is saved and will apply on the next full-text screen",
                       review.id, exc_info=True)
        return None


@router.post("/evidence/exclude")
async def exclude_papers(
    payload: EvidenceExcludeIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually exclude papers from downstream processing.

    Writes a `user_exclusions.json` the pipeline's shared exclusion loader
    already reads (stage_eligible skips these before pass-2; export + meta honor
    the same file). Already-extracted rows stay on disk; the exclusion takes
    effect on the next extract run and greys the row in the table now.
    """
    ws = Path(review.workspace_path)

    def _write() -> int:
        n = evidence_svc.set_exclusions(ws, payload.dois, payload.reason, excluded=True)
        # A manual exclude also clears a stale force-include for the same DOI,
        # so the two files can never both claim a paper (exclusion is the
        # stronger verdict).
        evidence_svc.set_inclusions(ws, payload.dois, "", included=False)
        return n

    try:
        n = await anyio.to_thread.run_sync(_write)
    except evidence_svc.CorruptVerdictFile as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    job = await _restage_after_manual_verdict(db, review, user)
    return {"excluded": n, "restage_job_id": job.id if job else None}


@router.post("/evidence/include")
async def include_papers(
    payload: EvidenceExcludeIn,
    review: Review = Depends(owned_review),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually INCLUDE papers — a force-include the automatic gate can't override.

    Writes `user_inclusions.json` (the positive counterpart to the exclusion file)
    so `stage_eligible` stages the paper for extraction EVEN IF the full-text
    eligibility screen marked it ineligible or never ran for it — the user's call
    wins. Also clears any existing manual exclusion for the same DOIs, so the one
    button flips a paper cleanly from excluded to included.
    """
    ws = Path(review.workspace_path)

    def _write() -> int:
        # Clear an exclusion first (idempotent if none), then force-include.
        evidence_svc.set_exclusions(ws, payload.dois, "", excluded=False)
        return evidence_svc.set_inclusions(
            ws, payload.dois, payload.reason or "user_included", included=True)

    try:
        n = await anyio.to_thread.run_sync(_write)
    except evidence_svc.CorruptVerdictFile as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    job = await _restage_after_manual_verdict(db, review, user)
    return {"included": n, "restage_job_id": job.id if job else None}


@router.get("/feasibility")
async def get_feasibility(review: Review = Depends(owned_review)) -> dict:
    """Can this review's trials be pooled? A verdict the meta stage explains.

    Ungated and read live from the pass-2 records (same reader as the evidence
    table). Returns `assess_poolability`'s dict verbatim — the exact shape the
    shipped FeasibilityCard renders. NEVER blocks: meta-analysis stays optional
    and the user keeps the judgment.
    """
    return await anyio.to_thread.run_sync(
        feasibility_svc.build_feasibility, Path(review.workspace_path))


@router.get("/prisma")
async def get_prisma(review: Review = Depends(owned_review)) -> dict:
    """The PRISMA screening funnel for this review, from live artifacts.

    Ungated and tolerant of a partial pipeline: a review that has only searched
    yields the top of the funnel and nothing below. Returns
    `{main_flow, exclusions, sources, counts}`.
    """
    return await anyio.to_thread.run_sync(
        prisma_svc.build_prisma, Path(review.workspace_path))


async def _check_upload_quota(db: AsyncSession, owner_id: str, size: int) -> None:
    """413 when this upload would take the owner past their lifetime caps."""
    count, total = (await db.execute(
        select(func.count(PdfUpload.id), func.coalesce(func.sum(PdfUpload.size_bytes), 0))
        .where(PdfUpload.user_id == owner_id))).one()
    if count + 1 > settings.UPLOAD_MAX_FILES_PER_USER:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Upload limit reached: an account may upload at most "
            f"{settings.UPLOAD_MAX_FILES_PER_USER} PDFs.")
    if total + size > settings.UPLOAD_MAX_TOTAL_MB_PER_USER * 1024 * 1024:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Upload limit reached: an account may upload at most "
            f"{settings.UPLOAD_MAX_TOTAL_MB_PER_USER} MB of PDFs in total.")


@router.post("/evidence/upload-pdf", status_code=status.HTTP_202_ACCEPTED)
async def upload_pdf(
    doi: str = Form(..., max_length=255),
    file: UploadFile = File(...),
    review: Review = Depends(owned_review),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually file a PDF for one paper (for papers the auto-fetch couldn't get).

    Writes it into the paper's folder in the store, then enqueues the background
    convert job (`kind="pdf"`, resumable — it converts just the new file), so the
    paper gains full text and drops off the missing list. 400 on a non-PDF or a
    bad DOI; 413 when the file or the owner's upload quota is too large.
    """
    # Read in bounded chunks rather than `await file.read()`. The whole body ends
    # up in memory either way, so an uncapped read lets one request decide how
    # much of the host's RAM to consume. Streaming to the cap also means an
    # oversized upload is rejected without ever materializing in full — checking
    # Content-Length alone would not, since the header is client-supplied.
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"PDF is larger than the {settings.MAX_UPLOAD_MB} MB limit.",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")
    await _check_upload_quota(db, review.owner_id, len(data))
    try:
        dest = await anyio.to_thread.run_sync(
            evidence_svc.save_uploaded_pdf, Path(review.workspace_path), doi, data)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.add(PdfUpload(user_id=review.owner_id, review_id=review.id,
                     doi=doi[:255], size_bytes=len(data)))
    await db.commit()
    # Convert it (and any other unconverted PDFs) in the background.
    job = await jobs_svc.enqueue(db, review, "pdf")
    return {"stored": dest.name, "convert_job_id": job.id if job else None}
