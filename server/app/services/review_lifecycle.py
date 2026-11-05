"""Deleting a review completely — rows, running work, and workspace.

The old endpoint deleted only the ``reviews`` row (with a TODO): every Job,
approval, transcript turn and metadata row stayed behind pointing at a review
that no longer existed, an active job kept running against the workspace, and
the workspace itself stayed on disk forever with nothing referencing it.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .._time import utcnow
from ..config import settings
from ..db.models import (
    AgentMessage,
    Job,
    PaperMetadata,
    PdfUpload,
    PublishedDashboard,
    PublishRequest,
    Review,
    ReviewAutomation,
    StageState,
)
from . import jobs as jobs_svc

logger = logging.getLogger(__name__)


def deleted_dir() -> Path:
    """Where deleted workspaces are moved (``DATA_ROOT/_deleted``)."""
    return Path(settings.DATA_ROOT) / "_deleted"


async def delete_review(db: AsyncSession, review: Review) -> Path | None:
    """Stop *review*'s jobs, delete its rows, move its workspace aside.

    Returns where the workspace went (None if it had none on disk).

    * **Jobs** that are queued or running are cancelled exactly like the Stop
      button (cancel flag + arq abort) BEFORE the workspace moves, so a step is
      not left writing into a directory that is about to disappear.
    * **Dependent rows** are deleted explicitly. The schema declares foreign
      keys but SQLite does not enforce them, so nothing else would.
    * **A live dashboard** keeps its ``published_dashboards`` row, detached
      (``review_id=NULL``, ``source="orphaned"``): its feed is still on the
      public site, and the row is what stops another review publishing over
      that route. Taking it down is a deliberate admin act, not a side effect.
    * **The workspace** is MOVED to ``DATA_ROOT/_deleted/<id>-<timestamp>``,
      not removed: an accidental delete of months of extraction must be
      recoverable by an operator. Purging that folder is a manual step.
    """
    active = (await db.execute(select(Job).where(
        Job.review_id == review.id, Job.state.in_(jobs_svc.ACTIVE_STATES)))).scalars().all()
    for job in active:
        await jobs_svc.stop_job(db, job, "Cancelled: the review was deleted.")

    rid = review.id
    for model in (Job, StageState, AgentMessage, PaperMetadata, PublishRequest,
                  ReviewAutomation, PdfUpload):
        await db.execute(delete(model).where(model.review_id == rid))
    await db.execute(
        update(PublishedDashboard)
        .where(PublishedDashboard.review_id == rid)
        .values(review_id=None, source="orphaned")
    )
    workspace = Path(review.workspace_path) if review.workspace_path else None
    await db.delete(review)  # ReviewConfig goes with it (ORM cascade)
    await db.commit()

    if workspace is None or not workspace.exists():
        return None
    target = deleted_dir() / f"{rid}-{utcnow().strftime('%Y%m%dT%H%M%S')}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(workspace), str(target))
    except OSError:
        # The rows are already gone; a workspace that cannot be moved is an
        # operator cleanup, not a reason to fail the request.
        logger.exception("could not move deleted review %s workspace to %s", rid, target)
        return None
    return target
