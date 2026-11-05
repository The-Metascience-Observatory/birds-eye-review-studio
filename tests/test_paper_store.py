"""Tests for ``paper_store.py`` — the ``papers/<Type>/<stem>/`` artifact store.

The invariants these lock down are the ones the rest of the pipeline leans on:
a paper is found by STEM regardless of which type bucket it sits in, refiling is
idempotent, and a type change relocates the folder without losing artifacts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ber_core import paper_store
from ber_core.utils import discover_papers


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def make_paper(project: Path, bucket: str, stem: str,
               pdf: bool = True, body: bool = False) -> Path:
    """Create a paper folder with the artifacts requested."""
    d = project / "papers" / bucket / stem
    d.mkdir(parents=True, exist_ok=True)
    if pdf:
        (d / f"{stem}.pdf").write_bytes(b"%PDF-1.4 fake")
    if body:
        (d / "body.md").write_text("# Title\n\nBody text.\n", encoding="utf-8")
    return d


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    p = tmp_path / "projects" / "demo"
    p.mkdir(parents=True)
    return p


# --------------------------------------------------------------------------
# type_folder
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Original Research", "Original_Research"),
    ("Clinical Trial", "Clinical_Trial"),
    ("Case Report/Series", "Case_Report_Series"),
    ("Cross-Sectional Study", "Cross-Sectional_Study"),
])
def test_type_folder_sanitizes(raw, expected):
    assert paper_store.type_folder(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "no_abstract", "unknown", "N/A"])
def test_type_folder_routes_empty_types_to_unclassified(raw):
    """A paper with no usable type still gets a home — never a folder literally
    named ``no_abstract``, which reads as a real article type in the tree."""
    assert paper_store.type_folder(raw) == paper_store.UNCLASSIFIED


def test_type_folder_never_collides_with_a_reserved_name():
    """A type that sanitizes to the staging dir must not hijack it."""
    assert paper_store.type_folder("_incoming") == paper_store.UNCLASSIFIED


# --------------------------------------------------------------------------
# build_index / paper_dir
# --------------------------------------------------------------------------

def test_index_finds_papers_in_any_bucket(project):
    make_paper(project, "Clinical_Trial", "10.1000--a")
    make_paper(project, "Original_Research", "10.1000--b")
    make_paper(project, paper_store.UNCLASSIFIED, "10.1000--c")

    idx = paper_store.build_index(project)

    assert set(idx) == {"10.1000/a", "10.1000/b", "10.1000/c"}
    assert idx["10.1000/a"].parent.name == "Clinical_Trial"


def test_paper_is_found_by_stem_not_by_type(project):
    """The type bucket is not a lookup key: a caller that knows only the DOI
    must resolve the paper wherever it currently sits."""
    make_paper(project, "Clinical_Trial", "10.1000--a")

    found = paper_store.paper_dir(project, "10.1000/a")

    assert found is not None and found.name == "10.1000--a"


def test_index_is_case_insensitive_on_doi(project):
    make_paper(project, "Clinical_Trial", "10.1000--MixedCase")
    assert paper_store.paper_dir(project, "10.1000/mixedcase") is not None


def test_index_falls_back_to_legacy_converted_dir(project):
    """An un-migrated project must keep resolving while the rollout proceeds."""
    legacy = project / "converted" / "10.1000--old"
    legacy.mkdir(parents=True)
    (legacy / "body.md").write_text("text", encoding="utf-8")

    idx = paper_store.build_index(project)

    assert idx["10.1000/old"] == legacy


def test_new_store_wins_over_legacy_for_the_same_paper(project):
    """A half-migrated project prefers the new tree — otherwise a stale
    converted/ folder would shadow the migrated one."""
    (project / "converted" / "10.1000--a").mkdir(parents=True)
    new = make_paper(project, "Clinical_Trial", "10.1000--a")

    assert paper_store.build_index(project)["10.1000/a"] == new


def test_legacy_fallback_can_be_disabled(project):
    (project / "converted" / "10.1000--old").mkdir(parents=True)
    assert paper_store.build_index(project, legacy=False) == {}


# --------------------------------------------------------------------------
# ensure_paper_dir
# --------------------------------------------------------------------------

def test_ensure_creates_under_the_type_bucket(project):
    d = paper_store.ensure_paper_dir(project, "10.1000/new", "Clinical Trial")

    assert d.is_dir()
    assert d.parent.name == "Clinical_Trial"
    assert d.name == "10.1000--new"


def test_ensure_returns_an_existing_folder_without_moving_it(project):
    """A read must never relocate a paper — that is refile()'s job alone."""
    existing = make_paper(project, "Original_Research", "10.1000--a")

    d = paper_store.ensure_paper_dir(project, "10.1000/a", "Clinical Trial")

    assert d == existing
    assert d.parent.name == "Original_Research"


# --------------------------------------------------------------------------
# refile
# --------------------------------------------------------------------------

def test_refile_moves_a_paper_whose_type_changed(project):
    make_paper(project, "Original_Research", "10.1000--a", body=True)

    moves = paper_store.refile(project, {"10.1000/a": "Clinical Trial"},
                               execute=True)

    assert len(moves) == 1
    dest = project / "papers" / "Clinical_Trial" / "10.1000--a"
    assert (dest / "10.1000--a.pdf").is_file()
    assert (dest / "body.md").is_file()
    assert not (project / "papers" / "Original_Research").exists()


def test_refile_is_idempotent(project):
    make_paper(project, "Original_Research", "10.1000--a")
    types = {"10.1000/a": "Clinical Trial"}

    paper_store.refile(project, types, execute=True)
    second = paper_store.refile(project, types, execute=True)

    assert second == []


def test_refile_dry_run_plans_without_touching_disk(project):
    src = make_paper(project, "Original_Research", "10.1000--a")

    moves = paper_store.refile(project, {"10.1000/a": "Clinical Trial"})

    assert len(moves) == 1
    assert src.is_dir(), "dry run must not move anything"
    assert not (project / "papers" / "Clinical_Trial").exists()


def test_refile_files_staged_downloads(project):
    """fetchpdf can only nest one level, so downloads land in _incoming/ and
    refile is what gives them their type bucket."""
    make_paper(project, paper_store.INCOMING, "10.1000--a")

    paper_store.refile(project, {"10.1000/a": "Clinical Trial"}, execute=True)

    assert (project / "papers" / "Clinical_Trial" / "10.1000--a"
            / "10.1000--a.pdf").is_file()


def test_staged_paper_with_no_classification_lands_in_unclassified(project):
    make_paper(project, paper_store.INCOMING, "10.1000--a")

    paper_store.refile(project, {}, execute=True)

    assert (project / "papers" / paper_store.UNCLASSIFIED
            / "10.1000--a").is_dir()


def test_refile_leaves_unclassified_papers_alone(project):
    """The classifications CSV is a SUBSET of the corpus (papers with no
    abstract are absent). Demoting every absent paper to _unclassified on each
    refile would thrash the tree and undo manual filing."""
    make_paper(project, "Clinical_Trial", "10.1000--a")

    moves = paper_store.refile(project, {"10.1000/other": "Case Report"})

    assert moves == []


def test_refile_merges_into_an_existing_folder_keeping_filed_artifacts(project):
    """A re-download of a paper we already converted must not clobber body.md."""
    filed = make_paper(project, "Clinical_Trial", "10.1000--a", body=True)
    (filed / "body.md").write_text("REAL", encoding="utf-8")
    staged = make_paper(project, paper_store.INCOMING, "10.1000--a", body=True)
    (staged / "body.md").write_text("STALE", encoding="utf-8")
    (staged / "10.1000--a.xml").write_text("<article/>", encoding="utf-8")

    paper_store.refile(project, {"10.1000/a": "Clinical Trial"}, execute=True)

    assert (filed / "body.md").read_text() == "REAL"
    assert (filed / "10.1000--a.xml").is_file(), "new artifacts still arrive"
    assert not staged.exists()


def test_refile_preserves_hardlinks(project, tmp_path):
    """Corpora are seeded by hardlinking PDFs in from other projects; a refile
    must not break that (rename keeps the link, a copy would double the disk)."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    d = project / "papers" / "Original_Research" / "10.1000--a"
    d.mkdir(parents=True)
    os.link(source, d / "10.1000--a.pdf")

    paper_store.refile(project, {"10.1000/a": "Clinical Trial"}, execute=True)

    moved = project / "papers" / "Clinical_Trial" / "10.1000--a" / "10.1000--a.pdf"
    assert moved.stat().st_ino == source.stat().st_ino


def test_merge_preserves_same_size_different_content(tmp_path):
    src, dst = tmp_path / "incoming", tmp_path / "filed"
    src.mkdir(); dst.mkdir()
    (src / "body.md").write_text("new")
    (dst / "body.md").write_text("old")

    duplicates = paper_store.merge_into(src, dst)

    assert (dst / "body.md").read_text() == "old"
    assert (dst / "body.md.dup").read_text() == "new"
    assert duplicates == [dst / "body.md.dup"]
    assert not src.exists()


def test_merge_preserves_nested_supplements(tmp_path):
    src, dst = tmp_path / "incoming", tmp_path / "filed"
    for folder in (src, dst):
        (folder / "supplementary").mkdir(parents=True)
    (src / "supplementary/table.csv").write_text("new")
    (dst / "supplementary/table.csv").write_text("old")
    (src / "supplementary/extra.csv").write_text("extra")

    duplicates = paper_store.merge_into(src, dst)

    assert (dst / "supplementary/table.csv").read_text() == "old"
    assert (dst / "supplementary/table.csv.dup").read_text() == "new"
    assert (dst / "supplementary/extra.csv").read_text() == "extra"
    assert duplicates == [dst / "supplementary/table.csv.dup"]


def test_merge_preserves_directory_file_collisions_and_dangling_links(tmp_path):
    src, dst = tmp_path / "incoming", tmp_path / "filed"
    src.mkdir(); dst.mkdir()
    (src / "supplementary").mkdir()
    (src / "supplementary/table.csv").write_text("table")
    (dst / "supplementary").write_text("manifest")
    (dst / "supplementary.dup").symlink_to("missing")

    duplicates = paper_store.merge_into(src, dst)

    assert (dst / "supplementary").read_text() == "manifest"
    assert (dst / "supplementary.dup").is_symlink()
    assert (dst / "supplementary.dup2/table.csv").read_text() == "table"
    assert duplicates == [dst / "supplementary.dup2"]


def test_merge_across_filesystems_preserves_artifacts(tmp_path, monkeypatch):
    import errno
    src, dst = tmp_path / "incoming", tmp_path / "filed"
    src.mkdir(); dst.mkdir()
    (src / "body.md").write_text("new")

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(paper_store.os, "replace", cross_device)
    monkeypatch.setattr(paper_store.os, "rename", cross_device)
    paper_store.merge_into(src, dst)

    assert (dst / "body.md").read_text() == "new"
    assert not src.exists()


# --------------------------------------------------------------------------
# discover_papers over the store (the two-level pass)
# --------------------------------------------------------------------------

def test_discover_papers_walks_the_typed_layout(project):
    make_paper(project, "Clinical_Trial", "10.1000--a", body=True)
    make_paper(project, "Original_Research", "10.1000--b", body=True)

    refs = discover_papers(project / "papers")

    assert [r.stem for r in refs] == ["10.1000--a", "10.1000--b"]
    assert {r.layout for r in refs} == {"typed"}
    assert all(r.md_path is not None and r.pdf_path is not None for r in refs)


def test_discover_papers_still_handles_the_nested_layout(project):
    """The legacy converted/<stem>/ shape must keep working unchanged."""
    d = project / "converted" / "10.1000--a"
    d.mkdir(parents=True)
    (d / "body.md").write_text("text", encoding="utf-8")

    refs = discover_papers(project / "converted")

    assert [r.stem for r in refs] == ["10.1000--a"]
    assert refs[0].layout == "nested"


def test_discover_papers_does_not_recurse_below_one_level(project):
    """One level only — a deep tree must never turn discovery into a full walk."""
    deep = project / "papers" / "Clinical_Trial" / "subdir" / "10.1000--deep"
    deep.mkdir(parents=True)
    (deep / "body.md").write_text("text", encoding="utf-8")

    assert discover_papers(project / "papers") == []


def test_discover_papers_ignores_a_folder_with_no_artifacts(project):
    (project / "papers" / "Clinical_Trial" / "10.1000--empty").mkdir(parents=True)
    assert discover_papers(project / "papers") == []


# --------------------------------------------------------------------------
# iter_papers / legacy_flat_artifacts
# --------------------------------------------------------------------------

def test_iter_papers_falls_back_to_converted_when_no_store(project):
    d = project / "converted" / "10.1000--a"
    d.mkdir(parents=True)
    (d / "body.md").write_text("text", encoding="utf-8")

    assert [r.stem for r in paper_store.iter_papers(project)] == ["10.1000--a"]


def test_legacy_flat_artifacts_groups_multipart_suffixes(project):
    """fetchpdf's ``.fulltext.html`` and ``_supplementary_info.json`` must group
    with their own PDF — Path.stem gets both wrong."""
    pdfs = project / "pdfs"
    pdfs.mkdir()
    for name in ("10.1000--a.pdf", "10.1000--a.xml",
                 "10.1000--a.fulltext.html",
                 "10.1000--a_supplementary_info.json"):
        (pdfs / name).write_text("x", encoding="utf-8")
    (pdfs / "missing_pdfs.html").write_text("report", encoding="utf-8")

    grouped = paper_store.legacy_flat_artifacts(pdfs)

    assert set(grouped) == {"10.1000/a"}, "run-level reports are not papers"
    assert len(grouped["10.1000/a"]) == 4


# --------------------------------------------------------------------------
# relink_buckets
# --------------------------------------------------------------------------

def test_relink_repoints_bucket_symlinks_at_the_new_location(project):
    """eligible_*/ links are what pass-2 reads; a refile must not strand them."""
    paper = make_paper(project, "Clinical_Trial", "10.1000--a", body=True)
    bucket = project / "eligible_rct"
    bucket.mkdir()
    (bucket / "10.1000--a").symlink_to(
        Path("..") / "converted" / "10.1000--a", target_is_directory=True)

    fixed = paper_store.relink_buckets(project)

    link = bucket / "10.1000--a"
    assert fixed == 1
    assert link.resolve() == paper.resolve()
    assert not os.path.isabs(os.readlink(link)), "target must stay relative"


def test_relink_is_idempotent(project):
    make_paper(project, "Clinical_Trial", "10.1000--a")
    bucket = project / "eligible_rct"
    bucket.mkdir()
    (bucket / "10.1000--a").symlink_to(Path("..") / "converted" / "10.1000--a",
                                       target_is_directory=True)

    paper_store.relink_buckets(project)

    assert paper_store.relink_buckets(project) == 0


def test_relink_leaves_real_directories_alone(project):
    """A real directory in a bucket is a user's own file, not ours to rewrite."""
    make_paper(project, "Clinical_Trial", "10.1000--a")
    bucket = project / "eligible_rct"
    (bucket / "10.1000--a").mkdir(parents=True)

    assert paper_store.relink_buckets(project) == 0
    assert (bucket / "10.1000--a").is_dir()
