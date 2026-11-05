"""A pipeline run must never write to the public website.

Publishing a dashboard is a two-party, permission-gated act. Until this change
it was the tail of an ordinary run: `13_export` was step 17 of
`canonical_steps()`, and the studio's DEFAULT run target ("Full review") ran to
the end with `to_num: undefined`. Clicking the most obvious button in the UI
wrote straight into the shared `metascience_observatory_website` repo. It was
also resume-reachable, so resuming an interrupted review could resume *into* a
publish.

These tests pin the two halves of the fix:
  * the pipeline builds into the review's own workspace (P0), and
  * `status`/`visibility` cannot be set through `PATCH /reviews/{id}` (P1),

because a permission check on the publish endpoint is worthless if a plain
field update can reach the same state.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import pipeline_bridge as pb  # noqa: E402
from app.services import stages as S  # noqa: E402


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    """A self-contained review workspace.

    This used to point at a hardcoded review UUID under ``server/_data/``, which
    is GITIGNORED — so every test below `pytest.skip`ped on any machine but the
    one that happened to have that review. A guard against accidentally
    publishing that silently evaporates in CI is worse than no guard, because it
    reports green. `canonical_steps_for` needs only a config.yaml, so build one.
    """
    ws = tmp_path_factory.mktemp("ws")
    (ws / "config.yaml").write_text(
        yaml.safe_dump({
            "topic": {"name": "Publish Guard", "synonyms": ["guard"],
                      "description": "d", "not_about": []},
            "article_types": ["Clinical Trial"],
            "intervention_categories": ["a", "other"],
            "outcome_domains": ["x", "other"],
            # Exercise BOTH pass-2 branches, so an observational-only leak is
            # caught too.
            "include_observational": True,
        }),
        encoding="utf-8",
    )
    return ws


@pytest.fixture(scope="module")
def steps(workspace):
    return pb.canonical_steps_for(workspace)


# --- P0: the run stays inside the workspace ----------------------------------

def test_the_publishing_step_is_gone_from_the_canonical_pipeline(steps) -> None:
    """`13_export` wrote into the public site as a normal step. Its replacement
    builds a bundle locally; publishing is not a pipeline step at all."""
    ids = [s.id for s in steps]
    assert "13_export" not in ids
    assert "13_build_bundle" in ids


def _website() -> Path:
    """The configured website checkout — or, since WEBSITE_DIR is unset by
    default, a stand-in path so the assertions below still check something."""
    return Path(settings.WEBSITE_DIR or (Path(settings.REPO_ROOT).parent / "website")).resolve()


def test_no_step_writes_anywhere_under_the_public_website(steps, workspace) -> None:
    """The load-bearing assertion: every declared output of every step lands in
    the review's workspace, so a full run cannot touch the shared site."""
    website = _website()
    ws = workspace.resolve()
    for step in steps:
        for produced in step.produces:
            p = Path(produced).resolve()
            assert not p.is_relative_to(website), f"{step.id} writes to {p}"
            assert p.is_relative_to(ws), f"{step.id} writes outside workspace: {p}"


def test_no_step_is_handed_the_website_directory_as_an_argument(steps) -> None:
    """`produces` only covers *verified* artifacts, so a step could still write
    to the site without declaring it. Nothing may even receive the path."""
    website = str(_website())
    for step in steps:
        for arg in step.args:
            assert website not in str(arg), f"{step.id} got --website-dir: {arg}"


def test_the_bundle_is_built_before_its_panels_are_resolved(steps) -> None:
    """Order matters: the generator reads the bundle the build step writes."""
    ids = [s.id for s in steps]
    assert ids.index("13_build_bundle") < ids.index("16_generate_dashboard")


def test_the_dashboard_stage_owns_both_local_build_steps() -> None:
    """The stage must keep both build steps — otherwise it reports no steps and
    silently looks complete.

    The slug was `export` and is now `view-dashboard`, matching what the page is.
    Slugs are normally permanent because `StageState.stage` stores them, so that
    rename ships with `_migrate_renamed_stage_slugs` (db/session.py), which
    rewrites stored approvals on startup.
    """
    stage = S.get_stage("view-dashboard")
    assert stage is not None
    assert stage.step_ids == ("13_build_bundle", "16_generate_dashboard")
    assert S.get_stage("export") is None, "old slug must not linger as a stage"


# --- P1: status/visibility are server-owned ----------------------------------

def test_a_review_patch_cannot_set_status_or_visibility() -> None:
    """`patch_review` blind-`setattr`s whatever the model accepts, so anything
    ReviewPatch admits is directly writable by the owner."""
    from pydantic import ValidationError

    from app.schemas import ReviewPatch

    fields = set(ReviewPatch.model_fields)
    assert "status" not in fields and "visibility" not in fields

    # And it must be a loud rejection, not a silent drop: a caller that thinks
    # it published needs to find out that it did not.
    for attempt in ({"status": "published"}, {"visibility": "public"}):
        with pytest.raises(ValidationError):
            ReviewPatch(**attempt)


def test_a_review_patch_still_accepts_the_fields_users_do_own() -> None:
    from app.schemas import ReviewPatch

    got = ReviewPatch(title="New title").model_dump(exclude_unset=True)
    assert got == {"title": "New title"}
