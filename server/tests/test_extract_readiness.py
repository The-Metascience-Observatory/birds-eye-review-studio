"""The Extract stage's data-readiness preflight.

Pass 2 is the expensive step (one full-text Sonnet call per paper), and the run
endpoint blocks on preflight ERRORS — so these checks are what stands between a
click on Run and paying to extract an unstaged, half-screened corpus:

* no pass-1 verdict file        -> error   (`no_fulltext_verdicts`, blocks)
* verdicts but nothing staged   -> warning (`nothing_staged`)
* staged but papers still awaiting their full-text verdict
                                -> warning (`pass1_incomplete` — extraction is
                                   resumable, so an eager partial run is legal;
                                   the user just has to know it's partial)

Deliberately NOT an approval gate: `extract1` is hidden with
`requires_approval=False`, and test_stages.py pins that a hidden stage can never
block the walkthrough. Readiness lives in preflight instead.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_readiness_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import pipeline_bridge as pb  # noqa: E402
from server.app.services import workspace as ws  # noqa: E402

_counter = itertools.count()

DOI_A = "10.1000/ready.a"
DOI_B = "10.1000/ready.b"


def _workspace() -> tuple[Path, Path]:
    """A review workspace whose project has two screened-in papers with text."""
    n = next(_counter)
    cfg = ws.build_config_dict(
        topic_name=f"Readiness Test {n}",
        synonyms=["thing"],
        description="test",
        not_about=[],
        include_observational=False,
    )
    workspace = ws.create_review_workspace(f"readiness{n}", cfg)
    proj = pb.build_project(workspace)
    data = proj.data_dir
    data.mkdir(parents=True, exist_ok=True)

    (data / "search_dois.csv").write_text(
        f"doi,title\n{DOI_A},Paper A\n{DOI_B},Paper B\n", encoding="utf-8")
    (data / "classifications.csv").write_text(
        "doi,is_relevant,relevance_confidence,studies_treatment,article_type,"
        "type_confidence,is_excluded,exclusion_reason,certainty,organism\n"
        f"{DOI_A},yes,high,yes,Clinical Trial,high,no,,high,human\n"
        f"{DOI_B},yes,high,yes,Clinical Trial,high,no,,high,human\n",
        encoding="utf-8")
    for doi in (DOI_A, DOI_B):
        stem = doi.replace("/", "--")
        conv = proj.project_dir / "converted" / stem
        conv.mkdir(parents=True, exist_ok=True)
        (conv / "body.md").write_text("# Trial\n" + "text " * 100,
                                      encoding="utf-8")
    return workspace, proj.project_dir


def _codes(workspace: Path, stage: str | None = "extract") -> dict[str, str]:
    return {i["code"]: i["level"] for i in pb.preflight(workspace, stage_slug=stage)}


def _pass1_record(doi: str, eligible: bool) -> str:
    return json.dumps({
        "paper_id": doi, "_status": "ok",
        "screening": {"eligible_for_pass2": eligible},
    }) + "\n"


def test_missing_verdict_file_is_a_blocking_error() -> None:
    workspace, _ = _workspace()
    codes = _codes(workspace)
    assert codes.get("no_fulltext_verdicts") == "error"


def test_verdicts_but_empty_buckets_warns_nothing_staged() -> None:
    workspace, proj_dir = _workspace()
    (proj_dir / "data" / "trial_extractions.jsonl").write_text(
        _pass1_record(DOI_A, False) + _pass1_record(DOI_B, False),
        encoding="utf-8")
    (proj_dir / "eligible_rct").mkdir()
    (proj_dir / "eligible_observational").mkdir()

    codes = _codes(workspace)
    assert "no_fulltext_verdicts" not in codes
    assert codes.get("nothing_staged") == "warning"
    # Both papers have verdicts, so coverage is complete.
    assert "pass1_incomplete" not in codes


def test_unscreened_fulltext_papers_warn_pass1_incomplete() -> None:
    """Paper B has full text on disk but no pass-1 verdict yet: the extract run
    would silently miss it. A warning (never an error — extraction is
    resumable) has to say so."""
    workspace, proj_dir = _workspace()
    (proj_dir / "data" / "trial_extractions.jsonl").write_text(
        _pass1_record(DOI_A, True), encoding="utf-8")
    bucket = proj_dir / "eligible_rct"
    bucket.mkdir()
    stem = DOI_A.replace("/", "--")
    (bucket / stem).symlink_to(Path("..") / "converted" / stem,
                               target_is_directory=True)

    codes = _codes(workspace)
    assert "no_fulltext_verdicts" not in codes
    assert "nothing_staged" not in codes
    assert codes.get("pass1_incomplete") == "warning"


def test_full_coverage_is_clean() -> None:
    workspace, proj_dir = _workspace()
    (proj_dir / "data" / "trial_extractions.jsonl").write_text(
        _pass1_record(DOI_A, True) + _pass1_record(DOI_B, False),
        encoding="utf-8")
    bucket = proj_dir / "eligible_rct"
    bucket.mkdir()
    stem = DOI_A.replace("/", "--")
    (bucket / stem).symlink_to(Path("..") / "converted" / stem,
                               target_is_directory=True)

    codes = _codes(workspace)
    for code in ("no_fulltext_verdicts", "nothing_staged", "pass1_incomplete"):
        assert code not in codes, code


def test_readiness_is_scoped_to_the_extract_stage() -> None:
    """A brand-new review must not be blocked from SEARCHING because it has no
    pass-1 file — that is true of every review by definition."""
    workspace, _ = _workspace()
    for stage in (None, "search", "classify"):
        codes = _codes(workspace, stage)
        assert "no_fulltext_verdicts" not in codes, stage
