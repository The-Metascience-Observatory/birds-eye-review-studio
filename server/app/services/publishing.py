"""Publish orchestration: the DB side of request → approve → deploy.

`publish_deploy.py` owns the filesystem (diff, atomic swap); this module owns the
records — PublishRequest and PublishedDashboard — and the rules that tie them to
the deploy: route uniqueness, the confirm-token check, and stamping the live-site
snapshot. Keeping the two apart means the deploy can be unit-tested against a temp
dir with no DB, and the record logic tested with no filesystem.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from .._time import utcnow
from ..db.models import PublishedDashboard, PublishRequest, Review, User
from . import publish_deploy as deploy_svc
from . import pipeline_bridge


async def route_holder(db: AsyncSession, route: str) -> PublishedDashboard | None:
    """The dashboard currently occupying `route`, if any."""
    return await db.get(PublishedDashboard, route)


async def route_available_for(
    db: AsyncSession, route: str, review: Review, *, reveal_holder: bool = False
) -> tuple[bool, str | None]:
    """Is `route` free for THIS review to publish to?

    Available when unoccupied, or occupied by this same review (a republish).
    Returns (available, taken_by). ``taken_by`` names the holder only for an
    admin (*reveal_holder*): another user's review id is not the owner's to
    learn, so they are told only that a different review holds it.
    """
    holder = await route_holder(db, route)
    if holder is None:
        return True, None
    if holder.review_id == review.id:
        return True, None  # republishing our own route
    if not reveal_holder:
        return False, "another dashboard"
    label = holder.slug if holder.review_id is None else f"review:{holder.review_id}"
    return False, label


async def build_preflight(
    db: AsyncSession, review: Review, route: str, *, reveal_holder: bool = False
) -> dict:
    """Assemble the preflight packet: DB route check + filesystem bundle read.

    Adds publication staleness when republishing an already-live route: the live
    snapshot vs. what would go out now. A changed feasibility verdict is flagged
    explicitly, because then the live site is making a different CLAIM than the
    data supports — worse than merely being out of date.
    """
    slug = pipeline_bridge.project_slug(Path(review.workspace_path))
    if route != deploy_svc.route_for(slug):
        raise ValueError("The publish route must match the review’s canonical topic route")
    available, taken_by = await route_available_for(
        db, route, review, reveal_holder=reveal_holder)
    workspace = Path(review.workspace_path)
    slug = pipeline_bridge.project_slug(workspace)
    packet = deploy_svc.preflight(
        workspace, slug, route,
        route_available=available, route_taken_by=taken_by)

    holder = await route_holder(db, route)
    if holder is not None and holder.review_id == review.id:
        packet["live_n_trials"] = holder.n_trials
        packet["live_feasibility_overall"] = holder.feasibility_overall
        now_verdict = packet.get("feasibility_overall")
        packet["feasibility_changed"] = bool(
            holder.feasibility_overall
            and now_verdict
            and holder.feasibility_overall != now_verdict)
    return packet


async def approve_and_deploy(db, req, admin, *, confirm_token, route, allow_shrink):
    from ber_core.bundles import directory_lock
    review = await db.get(Review, req.review_id)
    if review is None:
        raise ValueError("The review no longer exists.")
    slug = pipeline_bridge.project_slug(Path(review.workspace_path))
    import shutil
    import tempfile
    from ber_core.bundles import replace_directory
    target = deploy_svc.deployed_dir(slug)
    with directory_lock(target):
        backup = Path(tempfile.mkdtemp(prefix=".publish-rollback.", dir=target.parent))
        previous = backup / "previous"
        had_live = target.exists()
        if had_live:
            shutil.copytree(target, previous)
        try:
            result = await _approve_and_deploy_locked(db, req, admin, confirm_token=confirm_token,
                                                    route=route, allow_shrink=allow_shrink)
            await db.commit()
            return result
        except BaseException:
            if had_live:
                replace_directory(previous, target)
            elif target.exists():
                shutil.rmtree(target)
            await db.rollback()
            raise
        finally:
            shutil.rmtree(backup, ignore_errors=True)


async def _approve_and_deploy_locked(
    db: AsyncSession, req: PublishRequest, admin: User, *,
    confirm_token: str, route: str | None, allow_shrink: bool,
) -> PublishedDashboard:
    """Run the deploy for an approved request, then record it. Raises ValueError
    on a stale token, an occupied route, or a refused shrink — the router maps
    these to 409."""
    review = await db.get(Review, req.review_id)
    if review is None:
        raise ValueError("The review no longer exists.")

    final_route = route or req.requested_route
    workspace = Path(review.workspace_path)
    slug = pipeline_bridge.project_slug(workspace)

    if final_route != deploy_svc.route_for(slug):
        raise ValueError("The publish route must match the review’s canonical topic route")

    # The token must still match: a rebuild or a live-feed change since preflight
    # invalidates it, so the admin cannot approve one diff and publish another.
    diff = deploy_svc.diff_against_live(workspace, slug)
    if deploy_svc.confirm_token(workspace, slug, diff) != confirm_token:
        raise ValueError(
            "The dashboard changed since preflight — re-run preflight and approve "
            "again.")

    available, _ = await route_available_for(db, final_route, review)
    if not available:
        raise ValueError(f"Route {final_route!r} is already taken.")

    # Filesystem deploy (atomic; re-checks the shrink guard itself).
    result = deploy_svc.deploy(
        workspace, slug, jobtag=req.id, allow_shrink=allow_shrink, expected_token=confirm_token)

    # Record: upsert the PublishedDashboard row keyed on the route.
    pub = await db.get(PublishedDashboard, final_route)
    if pub is None:
        pub = PublishedDashboard(route=final_route)
        db.add(pub)
    pub.slug = slug
    pub.review_id = review.id
    pub.published_by = admin.id
    pub.n_trials = result.get("n_trials")
    pub.feasibility_overall = result.get("feasibility_overall")
    pub.built_at = utcnow()
    pub.source = "studio"

    # Stamp the request as approved, Decision-shaped.
    req.state = "approved"
    req.decided_by = admin.id
    req.decided_at = utcnow()
    req.decision = {
        "route": final_route,
        "confirm_token": confirm_token,
        "shrink_overridden": result.get("shrink_overridden", False),
        "n_trials": result.get("n_trials"),
    }
    # The review's own status follows the publish.
    review.status = "published"
    review.visibility = "public"
    return pub
