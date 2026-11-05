"""The server-side dashboard deploy — the ONE place that writes to the public site.

Publishing a Bird's Eye Review means copying its locally-built data bundle into
a website checkout's data dir (`WEBSITE_DIR/data/birds_eye_reviews/<slug>/`),
which the website's own hand-built pages read. This repo generates the DATA; the
website owns the pages — so deploy is a data-feed swap, not a page generator.
`WEBSITE_DIR` is unset by default; an instance without it cannot publish, and
every entry point here says so with a ValueError (422/409 at the API).

Two properties the old CLI publish lacked, and the reasons they matter here:

* **Atomic.** The CLI wrote file-by-file into the live dir, so a crash mid-copy
  left a half-updated dashboard serving a broken mix of old and new files. This
  stages the whole bundle into `<target>.publishing.<token>/`, then `os.replace`s
  the directory into place after moving the old tree aside. Failed swaps restore
  the old tree; approval additionally protects the DB commit. Replacement also
  correctly REMOVES a stale `meta_analysis.json` (the new bundle simply doesn't
  contain one), where the file-by-file copy left it behind and the site's
  `existsSync` gate kept serving a forest plot the data no longer supports.

* **Shrink-guarded.** `_diff_vs_deployed` (ported by import from the CLI) flags a
  publish that would REMOVE trials or shrink a metric. In the CLI this was a
  `sys.exit` buried in a job log; here the diff is surfaced in preflight where a
  human sees it before approving, and the deploy refuses a shrink unless the
  approval explicitly allows it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

from ..config import settings
from . import bundle as bundle_svc

from ._pipeline_path import REPO_ROOT


def _cli():
    """The CLI export module (a step script, not a package module ⇒ import by spec).

    We reuse its `_load_deployed` / `_diff_vs_deployed` / `_chart_metrics` rather
    than reimplement the shrink diff, so the studio's guard and the CLI's guard
    can never disagree about what counts as information loss.
    """
    spec = importlib.util.spec_from_file_location(
        "_export_dashboard_cli", str(REPO_ROOT / "steps" / "export_dashboard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PublishingDisabled(ValueError):
    """No website checkout is configured, so there is nowhere to publish to."""


def public_data_dir() -> Path:
    if not settings.WEBSITE_DIR:
        raise PublishingDisabled(
            "Publishing is not configured on this instance: set WEBSITE_DIR in "
            "server/.env to a checkout of the website repository.")
    return Path(settings.WEBSITE_DIR) / "data" / "birds_eye_reviews"


def deployed_dir(slug: str) -> Path:
    return public_data_dir() / slug


def route_for(slug: str) -> str:
    """The public URL segment. The website's routes hyphenate the underscore
    slug (`long_covid` → `long-covid`)."""
    return slug.replace("_", "-")


def diff_against_live(workspace: Path, slug: str) -> dict | None:
    """Diff the built bundle vs. what is currently deployed for `slug`.

    None when there is no bundle to publish OR nothing is deployed yet (a first
    publish has no diff). Otherwise the CLI's `{removed_ids, added_ids,
    old_metrics, new_metrics, shrunk_metrics}` shape.
    """
    bdir = bundle_svc.bundle_dir(workspace)
    if bdir is None:
        return None
    feed = bdir / "trial_extractions.jsonl"
    if not feed.exists():
        return None

    cli = _cli()
    deployed = cli._load_deployed(deployed_dir(slug), slug)
    if deployed is None:
        return None  # first publish — no prior feed to diff against
    new_records = cli._read_jsonl(feed)
    return cli._diff_vs_deployed(new_records, deployed)


def confirm_token(workspace: Path, slug: str, diff: dict | None) -> str:
    """A hash of the bundle state + diff the admin reviewed.

    Approve carries this back; if the bundle was rebuilt (or the live feed
    changed) between preflight and approve, the recomputed token differs and the
    approval is refused — an admin cannot review one diff and publish another.
    """
    from ber_core.bundles import approval_digest
    return approval_digest(bundle_svc.bundle_dir(workspace), deployed_dir(slug), slug)


def preflight(workspace: Path, slug: str, route: str, *,
              route_available: bool, route_taken_by: str | None) -> dict:
    """The evidence packet for an admin's publish decision. Writes nothing.

    `route_available`/`route_taken_by` come from the DB (the caller checks the
    PublishedDashboard table); everything else is read from the built bundle.
    """
    state = bundle_svc.bundle_state(workspace)
    manifest = bundle_svc.read_manifest(workspace) or {}
    diff = diff_against_live(workspace, slug)
    shrink = bool(diff and (diff.get("removed_ids") or diff.get("shrunk_metrics")))

    return {
        "bundle_exists": bool(state.get("exists")),
        "built_at": state.get("built_at"),
        "stale": bool(state.get("stale")),
        "stale_reason": state.get("reason"),
        "route": route,
        "route_available": route_available,
        "route_taken_by": route_taken_by,
        "first_publish": diff is None,
        "n_trials": state.get("n_trials"),
        "feasibility_overall": (manifest.get("feasibility") or {}).get("overall"),
        "panels_rendered": manifest.get("panels_rendered") or [],
        "panels_skipped": manifest.get("panels_skipped") or [],
        "diff": None if diff is None else {
            "removed_ids": diff.get("removed_ids") or [],
            "added_ids": diff.get("added_ids") or [],
            "shrunk_metrics": {k: list(v) for k, v in
                               (diff.get("shrunk_metrics") or {}).items()},
        },
        "shrink_detected": shrink,
        "confirm_token": confirm_token(workspace, slug, diff),
    }


def deploy(workspace: Path, slug: str, *, jobtag: str, allow_shrink: bool = False, expected_token: str | None = None) -> dict:
    """Atomically publish the built bundle to the public site for `slug`.

    Raises ValueError on a missing bundle or a refused shrink; both are conditions
    preflight already surfaces, re-checked here so a direct call can't bypass them.
    Returns `{route, slug, n_trials, feasibility_overall, shrink_overridden}`.
    """
    bdir = bundle_svc.bundle_dir(workspace)
    if bdir is None:
        raise ValueError("No dashboard bundle has been built for this review.")

    diff = diff_against_live(workspace, slug)
    shrink = bool(diff and (diff.get("removed_ids") or diff.get("shrunk_metrics")))
    if shrink and not allow_shrink:
        raise ValueError(
            "Refusing to publish: this would remove trials or shrink a metric "
            "vs. the live dashboard. Approve with allow_shrink to override.")

    target = deployed_dir(slug)
    staging = target.parent / f"{target.name}.publishing.{jobtag}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        shutil.rmtree(staging)
    # Copy the whole bundle into staging, then swap. copytree gives us a complete
    # new tree; the swap replaces the old one wholesale, so a stale file present
    # in the old dashboard but absent from the new bundle (e.g. meta_analysis.json)
    # is correctly gone after the swap.
    shutil.copytree(bdir, staging)
    from ber_core.bundles import approval_digest
    if expected_token is not None and approval_digest(staging, target, slug) != expected_token:
        shutil.rmtree(staging)
        raise ValueError("Dashboard content changed since approval; run preflight again.")

    try:
        summary = json.loads((staging / bundle_svc.SUMMARY).read_text())
        if not isinstance(summary, dict):
            raise ValueError("summary must be an object")
    except (OSError, ValueError) as exc:
        shutil.rmtree(staging)
        raise ValueError("Cannot publish a bundle without a valid summary") from exc

    backup = None
    if target.exists():
        backup = target.parent / f"{target.name}.replaced.{jobtag}"
        os.replace(target, backup)  # atomic move-aside
    try:
        os.replace(staging, target)  # atomic swap-in
    except OSError:
        # Roll back to the moved-aside version if the swap-in fails.
        if backup is not None and not target.exists():
            os.replace(backup, target)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)

    return {
        "route": route_for(slug),
        "slug": slug,
        "n_trials": (summary.get("totals") or {}).get("n_trials"),
        "feasibility_overall": (summary.get("feasibility") or {}).get("overall"),
        "shrink_overridden": bool(shrink and allow_shrink),
    }
