"""Tests for convert-failure handling (convert_pdfs.py, P12).

The trap: a failed conversion used to become a silent stub holding only the raw
PDF — counted as "already have" forever, invisible in the report, exit code 0.
Failures now carry a ``conversion_failed.marker`` in the paper's own folder and
``--retry-failed`` clears them for another attempt.

Since conversion happens IN PLACE inside the paper store, a retry no longer
moves anything: the paper is already in its folder with its PDF, which is all a
re-attempt needs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("pdf4llm")

from ber_core.utils import discover_papers


@pytest.fixture(scope="module")
def conv():
    spec = importlib.util.spec_from_file_location("convert_pdfs",
                                                  "steps/convert_pdfs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _paper(project: Path, stem: str, *, bucket="Clinical_Trial",
           marker=True, md=False) -> Path:
    d = project / "papers" / bucket / stem
    d.mkdir(parents=True)
    (d / f"{stem}.pdf").write_bytes(b"%PDF-1.4 fake")
    if marker:
        (d / "conversion_failed.marker").write_text("failed\n")
    if md:
        (d / "body.md").write_text("# converted\n" * 20)
    return d


def _refs(project: Path):
    return discover_papers(project / "papers")


def test_marker_is_cleared_for_a_retry(conv, tmp_path):
    d = _paper(tmp_path, "10.1000--fail")

    n = conv._clear_failed_markers(_refs(tmp_path))

    assert n == 1
    assert not (d / "conversion_failed.marker").exists()
    assert (d / "10.1000--fail.pdf").exists(), "the PDF must stay put"


def test_clearing_markers_does_not_move_anything(conv, tmp_path):
    """The whole point of the in-place store: a retry is not a relocation."""
    d = _paper(tmp_path, "10.1000--fail")

    conv._clear_failed_markers(_refs(tmp_path))

    assert d.is_dir() and d.parent.name == "Clinical_Trial"


def test_successful_conversion_has_no_marker_to_clear(conv, tmp_path):
    good = _paper(tmp_path, "10.1000--good", marker=False, md=True)

    assert conv._clear_failed_markers(_refs(tmp_path)) == 0
    assert (good / "body.md").exists()


def test_stale_marker_on_a_converted_paper_is_cleared(conv, tmp_path):
    """Converted since the marker was written — the marker must not persist and
    make a good paper look failed."""
    d = _paper(tmp_path, "10.1000--fixed", marker=True, md=True)

    assert conv._clear_failed_markers(_refs(tmp_path)) == 1
    assert not (d / "conversion_failed.marker").exists()
    assert (d / "body.md").exists()


def test_empty_store_is_safe(conv, tmp_path):
    assert conv._clear_failed_markers([]) == 0


def test_marked_papers_are_skipped_without_retry_failed(tmp_path):
    """A marked failure must not be re-attempted on every ordinary run — that
    is the loop --retry-failed exists to break deliberately."""
    _paper(tmp_path, "10.1000--fail", marker=True)
    _paper(tmp_path, "10.1000--fresh", marker=False)

    refs = _refs(tmp_path)
    todo = [r for r in refs if r.pdf_path is not None and r.md_path is None]
    without_retry = [r for r in todo
                     if not (r.dir / "conversion_failed.marker").exists()]

    assert {r.stem for r in without_retry} == {"10.1000--fresh"}


def test_conversion_targets_are_grouped_by_bucket(tmp_path):
    """pdf4llm writes to output_dir/<stem>/, so an in-place convert has to run
    once per type bucket — grouping is what keeps output beside its PDF."""
    _paper(tmp_path, "10.1000--a", bucket="Clinical_Trial", marker=False)
    _paper(tmp_path, "10.1000--b", bucket="Clinical_Trial", marker=False)
    _paper(tmp_path, "10.1000--c", bucket="Original_Research", marker=False)

    buckets = {r.dir.parent.name for r in _refs(tmp_path)}

    assert buckets == {"Clinical_Trial", "Original_Research"}


def test_report_conversion_modes_reads_each_papers_provenance(conv, tmp_path, capsys):
    """provenance.json is the only durable record of whether tables survived."""
    d = _paper(tmp_path, "10.1000--degraded", marker=False, md=True)
    (d / "provenance.json").write_text('{"extraction_mode": "grobid-only"}')

    conv._report_conversion_modes({"10.1000--degraded": d})

    out = capsys.readouterr().out
    assert "NO TABLES" in out
    assert "grobid-only" in out


def test_table_bearing_mode_is_not_flagged(conv, tmp_path, capsys):
    d = _paper(tmp_path, "10.1000--good", marker=False, md=True)
    (d / "provenance.json").write_text(
        '{"extraction_mode": "merged-grobid-docling"}')

    conv._report_conversion_modes({"10.1000--good": d})

    assert "NO TABLES" not in capsys.readouterr().out
