"""The studio's PRISMA service wrapper (`services/prisma_svc.py`).

`derive_counts`/`to_visualize_json` are tested in `test_meta_correctness.py`.
This proves the WIRING: the service resolves a workspace, never raises on a
partial or absent pipeline, and returns the exact `{main_flow, exclusions,
sources}` shape the diagram consumes — plus the raw `counts` for the honesty
signals (the non-dedup warning).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_prisma_"))
os.environ.setdefault("DATA_ROOT", str(_TMP / "_data"))

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import prisma_svc  # noqa: E402
from server.app.services import pipeline_bridge  # noqa: E402


class _FakeProject:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.project_dir = data_dir.parent
        self.slug = "t"


def _patch_project(monkeypatch, data_dir: Path) -> None:
    monkeypatch.setattr(
        pipeline_bridge, "build_project", lambda ws: _FakeProject(data_dir))


def test_no_config_returns_the_empty_funnel_shape(monkeypatch) -> None:
    """A half-created workspace (build_project raises) must yield the empty shape,
    not a 500 — the diagram then renders 'no funnel yet'."""
    def boom(ws):
        raise ValueError("no config")

    monkeypatch.setattr(pipeline_bridge, "build_project", boom)
    got = prisma_svc.build_prisma(Path("/nonexistent"))
    assert got == {"main_flow": [], "exclusions": [], "sources": [], "counts": {}}


def test_a_missing_data_dir_is_empty_not_an_error(tmp_path, monkeypatch) -> None:
    _patch_project(monkeypatch, tmp_path / "data")  # never created
    got = prisma_svc.build_prisma(tmp_path)
    assert got["main_flow"] == []
    assert got["counts"] == {}


def test_search_only_yields_the_top_of_the_funnel(tmp_path, monkeypatch) -> None:
    """A review that has only searched draws the identification rows and nothing
    below — a partial funnel, not a crash."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "search_raw.csv").write_text(
        "source_api\npubmed\npubmed\nopenalex\n", encoding="utf-8")
    (data / "search_dois.csv").write_text("doi\n10.1/a\n10.1/b\n", encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    labels = [m["label"] for m in got["main_flow"]]
    assert "Records identified" in labels
    assert "After deduplication" in labels
    # Nothing downstream of search has run.
    assert "Included in synthesis" not in labels
    # Sources are surfaced for the identification line.
    names = {s["name"] for s in got["sources"]}
    assert names == {"pubmed", "openalex"}


def _rec(pid: str, *, nct: str | None = None, design: bool = True) -> str:
    """One pass-2 line. `study_design` is what makes a record publishable —
    without it the export drops the record, so the funnel must too."""
    rec: dict = {"paper_id": pid}
    if design:
        rec["study_design"] = {"design_type": "rct"}
    if nct:
        rec["metadata"] = {"nct_numbers": [nct]}
    return json.dumps(rec) + "\n"


def test_a_real_duplicate_raises_a_warning_naming_it(tmp_path, monkeypatch) -> None:
    """The de-duplication warning is an honesty signal with no slot in the
    visualize shape, so the service carries the raw counts alongside. It fires
    only when the DATA really counts one trial twice — and then it must name the
    offenders, so the user can act instead of being told to run a no-op."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(
        _rec("10.1/a", nct="NCT01234567") + _rec("10.1/b", nct="NCT01234567"),
        encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    warning = got["counts"]["included_warning"]
    assert got["counts"]["included"] == 2  # still counted; we only flag it
    issues = json.dumps(got["counts"]["issues"])
    assert "NCT01234567" in issues
    assert "10.1/a" in issues and "10.1/b" in issues
    assert "reconciliation" in warning


def test_duplicate_free_data_raises_no_warning(tmp_path, monkeypatch) -> None:
    """The old behaviour warned whenever a `_deduped` file was absent — i.e.
    always, since nothing produces one. A clean corpus must be silent."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(
        _rec("10.1/a") + _rec("10.1/b"), encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    assert got["counts"]["included"] == 2
    assert not any(i["code"] == "possible_companion_reports" for i in got["counts"]["issues"])


def test_duplicate_dois_json_collapses_the_pair_and_clears_the_warning(
        tmp_path, monkeypatch) -> None:
    """The live de-dup mechanism: mapping the duplicate onto its canonical
    counts them once — the same file the export and meta-analysis honor."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(
        _rec("10.1/a", nct="NCT01234567") + _rec("10.1/b", nct="NCT01234567"),
        encoding="utf-8")
    (data / "duplicate_dois.json").write_text(
        json.dumps({"10.1/b": "10.1/a"}), encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    assert got["counts"]["included"] == 1
    assert not any(i["code"] == "possible_companion_reports" for i in got["counts"]["issues"])


def test_a_duplicate_with_no_canonical_present_is_kept(tmp_path, monkeypatch) -> None:
    """Dropping it would lose the study entirely: the "duplicate" is the only
    copy in the feed. Same guard the export's _union_records applies."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(_rec("10.1/b"),
                                                        encoding="utf-8")
    (data / "duplicate_dois.json").write_text(
        json.dumps({"10.1/b": "10.1/a"}), encoding="utf-8")
    _patch_project(monkeypatch, data)

    assert prisma_svc.build_prisma(tmp_path)["counts"]["included"] == 1


def test_failed_and_empty_records_are_not_counted_as_included(
        tmp_path, monkeypatch) -> None:
    """The export drops these from the dashboard feed, so counting them here
    would make "Included in synthesis" exceed the trials the dashboard shows."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(
        _rec("10.1/a")
        + _rec("10.1/empty", design=False)          # status-ok but nothing to show
        + json.dumps({"paper_id": "10.1/failed", "_status": "error",
                      "study_design": {"design_type": "rct"}}) + "\n",
        encoding="utf-8")
    _patch_project(monkeypatch, data)

    assert prisma_svc.build_prisma(tmp_path)["counts"]["included"] == 1


def test_included_matches_what_the_export_would_publish(tmp_path, monkeypatch) -> None:
    """The invariant this whole rule exists for: PRISMA's "Included in
    synthesis" equals the record count the export's own filters keep, so the
    funnel and the dashboard's trial count can never disagree."""
    import importlib.util

    data = tmp_path / "data"
    data.mkdir()
    lines = (
        _rec("10.1/a", nct="NCT01")
        + _rec("10.1/b", nct="NCT01")               # duplicate of a
        + _rec("10.1/c")
        + _rec("10.1/empty", design=False)          # dropped: nothing displayable
    )
    (data / "trial_extractions_pass2.jsonl").write_text(lines, encoding="utf-8")
    (data / "duplicate_dois.json").write_text(
        json.dumps({"10.1/b": "10.1/a"}), encoding="utf-8")
    _patch_project(monkeypatch, data)

    # What the export keeps, via its own _union_records.
    spec = importlib.util.spec_from_file_location(
        "export_dash", REPO_ROOT / "steps/export_dashboard.py")
    export = importlib.util.module_from_spec(spec)
    sys.modules["export_dash"] = export
    spec.loader.exec_module(export)
    records = [json.loads(l) for l in lines.splitlines() if l.strip()]
    dup_map = export._load_duplicate_dois(data / "duplicate_dois.json")
    kept, _stats = export._union_records(records, [], dup_map)

    assert prisma_svc.build_prisma(tmp_path)["counts"]["included"] == len(kept) == 2


def test_pass1_file_is_never_counted_as_included(tmp_path, monkeypatch) -> None:
    """trial_extractions.jsonl (pass-1) is an ELIGIBILITY file — one record per
    screened paper incl. ineligibles — not synthesis output. It must NOT drive the
    'included' count, and must not fire the de-dup warning (the background full-
    text screen always writes it, so a pre-extraction review would otherwise show
    every pass-1 record as 'Included in synthesis')."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions.jsonl").write_text(
        '{"paper_id":"a","screening":{"eligible_for_pass2":true}}\n'
        '{"paper_id":"b","screening":{"eligible_for_pass2":false}}\n',
        encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    # No pass-2 yet → no "included" count, no box, no warning.
    assert got["counts"].get("included", 0) == 0
    assert not any(i["code"] == "possible_companion_reports" for i in got["counts"]["issues"])
    assert not any(b["label"] == "Included in synthesis" for b in got["main_flow"])


def test_manual_exclusion_removes_from_included_and_is_tallied(tmp_path, monkeypatch) -> None:
    """A DOI in user_exclusions.json is dropped from the `included` count and
    surfaces as a 'Manually excluded' reason — so the funnel the reviewer sees
    on the triage page moves the instant they toggle a paper out."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_extractions_pass2.jsonl").write_text(
        _rec("10.1/a") + _rec("10.1/b") + _rec("10.1/c"), encoding="utf-8")
    (data / "user_exclusions.json").write_text(
        json.dumps({"10.1/b": "off-topic"}), encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    assert got["counts"]["included"] == 2  # 3 extracted − 1 manual
    assert got["counts"]["exclusion_reasons"].get("Manually excluded") == 1


def test_manual_exclusion_removes_from_eligible_before_extraction(tmp_path, monkeypatch) -> None:
    """At the triage stage (no pass-2 yet) the exclusion comes off `eligible`,
    counting only papers that were still screened-in — not ones the pipeline had
    already excluded."""
    import json

    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_screening.csv").write_text(
        "doi,is_excluded,exclusion_reason\n"
        "10.1/a,no,\n"
        "10.1/b,no,\n"
        "10.1/c,yes,review\n",  # already excluded upstream
        encoding="utf-8")
    # User excludes one screened-in paper AND (redundantly) one already excluded.
    (data / "user_exclusions.json").write_text(
        json.dumps({"10.1/a": "user", "10.1/c": "user"}), encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    counts = got["counts"]
    # Base eligible = 2 (a, b); one manual removal of a screened-in paper → 1.
    assert counts["eligible"] == 1
    # Only the screened-in removal is tallied; the already-excluded c is not
    # double-counted.
    assert counts["exclusion_reasons"].get("Manually excluded") == 2
    assert counts["excluded_screening"] == 2


def test_eligible_excludes_off_topic_papers_of_the_right_type(tmp_path, monkeypatch) -> None:
    """The bug this fixes: a paper of an eligible TYPE (an RCT) but off-topic
    (is_relevant=no) must NOT count as eligible. `is_excluded` carries the type
    decision, so keying eligibility on it alone let VNS-for-depression trials
    pass in a VNS-for-insomnia review. Eligible = screened IN on relevance."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_screening.csv").write_text(
        "doi,is_relevant,is_excluded,exclusion_reason\n"
        "10.1/a,yes,no,\n"          # relevant, kept → eligible
        "10.1/b,no,no,\n"           # off-topic RCT, not type-excluded → NOT eligible
        "10.1/c,no,no,\n"           # another off-topic one
        "10.1/d,yes,yes,protocol\n",  # relevant but a protocol → excluded
        encoding="utf-8")
    _patch_project(monkeypatch, data)

    counts = prisma_svc.build_prisma(tmp_path)["counts"]
    assert counts["eligible"] == 1  # only 10.1/a
    assert counts["screened_for_eligibility"] == 1
    # Off-topic papers are tallied under a relevance reason, protocols under theirs.
    reasons = counts["exclusion_reasons"]
    assert reasons.get("Not relevant to the review topic") == 2
    assert reasons.get("protocol") == 1


def test_merged_screen_classifications_drive_the_funnel_with_one_exclusion_branch(
    tmp_path, monkeypatch,
) -> None:
    """Current-schema reviews have no relevance.csv/trial_screening.csv — the
    merged `quick` screen writes relevance + keep/drop into classifications.csv.
    PRISMA must source screened + eligible from THAT file, and emit ONE exclusion
    branch (not double-count the same papers as both 'excluded at screening' and
    'excluded at eligibility')."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "search_dois.csv").write_text(
        "doi\n10.1/a\n10.1/b\n10.1/c\n10.1/d\n", encoding="utf-8")
    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type,is_excluded,exclusion_reason\n"
        "10.1/a,yes,Clinical Trial,no,\n"          # kept → eligible
        "10.1/b,no,Clinical Trial,yes,\n"          # off-topic (blank reason)
        "10.1/c,yes,Narrative Review,yes,review\n"  # relevant but a review
        "10.1/d,unknown,no_abstract,no,\n",         # undecided → not eligible
        encoding="utf-8")
    _patch_project(monkeypatch, data)

    counts = prisma_svc.build_prisma(tmp_path)["counts"]
    assert counts["screened"] == 3
    assert counts["screening_pending"] == 1
    assert counts["eligible"] == 1  # only 10.1/a
    # Off-topic (blank reason) AND the undecided/unknown paper both count as not
    # relevant — a blank exclusion reason is labelled by relevance, not
    # "unspecified". The relevant-but-a-review paper keeps its type reason.
    reasons = counts["exclusion_reasons"]
    assert reasons.get("Not relevant to the review topic") == 1  # unknown is pending
    assert reasons.get("review") == 1  # 10.1/c

    viz = prisma_svc.build_prisma(tmp_path)
    labels = [e["label"] for e in viz["exclusions"]]
    # Exactly one screening/eligibility exclusion branch — no double count.
    assert labels.count("Excluded at screening") == 1
    assert labels.count("Excluded at eligibility") == 0


def test_the_eligibility_box_is_labelled_found_eligible_with_the_eligible_count(
    tmp_path, monkeypatch,
) -> None:
    """The spine box reads 'Found eligible' and shows the screened-IN count, not
    the number that entered assessment — the funnel must shrink at every step."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trial_screening.csv").write_text(
        "doi,is_relevant,is_excluded,exclusion_reason\n"
        "10.1/a,yes,no,\n"
        "10.1/b,no,no,\n"
        "10.1/c,no,no,\n",
        encoding="utf-8")
    _patch_project(monkeypatch, data)

    flow = prisma_svc.build_prisma(tmp_path)["main_flow"]
    box = next(b for b in flow if b["stage"] == "eligibility")
    assert box["label"] == "Passed title/abstract screening"
    assert box["n"] == 1  # not 3


def test_the_shape_matches_to_visualize_json(tmp_path, monkeypatch) -> None:
    """The service must not reshape the diagram fields — only ADD `counts`."""
    from ber_core.prisma import derive_counts, to_visualize_json

    data = tmp_path / "data"
    data.mkdir()
    (data / "search_dois.csv").write_text("doi\n10.1/a\n", encoding="utf-8")
    _patch_project(monkeypatch, data)

    got = prisma_svc.build_prisma(tmp_path)
    expected = to_visualize_json(derive_counts(_FakeProject(data)))
    for key in ("main_flow", "exclusions", "sources"):
        assert got[key] == expected[key]
