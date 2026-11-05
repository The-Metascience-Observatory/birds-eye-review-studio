"""Store integrity: one home per DOI, the configured root everywhere, a lock
around folder moves, and no piles of ``.dup`` fetch records."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from ber_core import migrate_to_paper_store
from ber_core import paper_store
from ber_core.utils import discover_papers

ROOT = Path(__file__).resolve().parents[1]


def make_paper(store: Path, bucket: str, stem: str, *, pdf=True, body=False) -> Path:
    d = store / bucket / stem
    d.mkdir(parents=True, exist_ok=True)
    if pdf:
        (d / f"{stem}.pdf").write_bytes(b"%PDF-1.4 " + stem.encode())
    if body:
        (d / "body.md").write_text("body", encoding="utf-8")
    return d


@pytest.fixture()
def project(tmp_path):
    p = tmp_path / "projects" / "demo"
    p.mkdir(parents=True)
    return p


# ── B7: parse_error and invented labels never mint a bucket ────────────────

def test_parse_error_routes_to_unclassified():
    assert paper_store.type_folder("parse_error") == paper_store.UNCLASSIFIED


def test_invalid_type_routes_to_unclassified_when_vocab_given():
    vocab = ["Clinical Trial", "Review"]
    assert paper_store.type_folder("Clinical Trial", vocab) == "Clinical_Trial"
    assert paper_store.type_folder("Made Up Type", vocab) == paper_store.UNCLASSIFIED


def test_refile_empties_a_stray_parse_error_bucket(project):
    store = project / "papers"
    make_paper(store, "parse_error", "10.1--a")
    make_paper(store, "parse_error", "10.1--b")
    paper_store.refile(project, {"10.1/a": "Clinical Trial"}, execute=True)
    assert (store / "Clinical_Trial" / "10.1--a").is_dir()
    assert (store / paper_store.UNCLASSIFIED / "10.1--b").is_dir()
    assert not (store / "parse_error").exists()


# ── B5: _incoming is not a home; collisions prefer text ────────────────────

def test_build_index_skips_incoming_and_prefers_body(project, capsys):
    store = project / "papers"
    make_paper(store, paper_store.INCOMING, "10.1--a")
    rich = make_paper(store, "Review", "10.1--a", body=True)
    make_paper(store, "_unclassified", "10.1--a")
    idx = paper_store.build_index(project, legacy=False)
    assert idx["10.1/a"] == rich
    assert "more than one type bucket" in capsys.readouterr().err


def test_discover_papers_prefers_folder_with_text(project):
    store = project / "papers"
    make_paper(store, paper_store.INCOMING, "10.1--a")
    make_paper(store, "Review", "10.1--a", body=True)
    (ref,) = discover_papers(store)
    assert ref.md_path is not None and ref.dir.parent.name == "Review"


def test_refile_merges_second_folder_instead_of_filing_beside(project):
    store = project / "papers"
    home = make_paper(store, "Review", "10.1--a", body=True)
    staged = make_paper(store, paper_store.INCOMING, "10.1--A", pdf=True)
    (staged / "extra.json").write_text("{}")
    paper_store.refile(project, {"10.1/a": "Review"}, execute=True)
    assert not staged.exists()
    assert (home / "extra.json").exists()
    assert [p.name for p in (store / "Review").iterdir()] == ["10.1--a"]


def test_refile_collapses_duplicate_folders_across_buckets(project):
    store = project / "papers"
    make_paper(store, "_unclassified", "10.1--a")
    make_paper(store, "Review", "10.1--a", body=True)
    paper_store.refile(project, {"10.1/a": "Clinical Trial"}, execute=True)
    folders = [p for p in store.rglob("10.1--a") if p.is_dir()]
    assert folders == [store / "Clinical_Trial" / "10.1--a"]
    assert (folders[0] / "body.md").exists()
    assert paper_store.refile(project, {"10.1/a": "Clinical Trial"}, execute=True) == []


def test_ensure_and_refile_share_one_stem_encoder(project):
    d = paper_store.ensure_paper_dir(project, "10.1/ABC(1)", "Review")
    assert d.name == paper_store.stem_for("10.1/abc(1)")


# ── B6: re-fetched provenance keeps the newer record ───────────────────────

def _prov(path: Path, completed: str) -> None:
    path.write_text(json.dumps({"completed_at": completed}), encoding="utf-8")


def test_refetched_provenance_keeps_newer_not_dup(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _prov(dst / "10.1--a.provenance.json", "2026-01-01T00:00:00Z")
    _prov(src / "10.1--a.provenance.json", "2026-06-01T00:00:00Z")
    assert paper_store.merge_into(src, dst) == []
    assert json.loads((dst / "10.1--a.provenance.json").read_text())["completed_at"].startswith("2026-06")
    assert not list(dst.glob("*.dup*"))
    # An OLDER incoming record is dropped, not kept as .dup.
    src.mkdir()
    _prov(src / "10.1--a.provenance.json", "2025-01-01T00:00:00Z")
    paper_store.merge_into(src, dst)
    assert json.loads((dst / "10.1--a.provenance.json").read_text())["completed_at"].startswith("2026-06")
    assert not list(dst.glob("*.dup*"))


def test_differing_pdfs_still_kept_as_dup(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "x.pdf").write_bytes(b"one")
    (dst / "x.pdf").write_bytes(b"two")
    assert [p.name for p in paper_store.merge_into(src, dst)] == ["x.pdf.dup"]


def test_cleanup_provenance_dups_script(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "_cleanup", ROOT / "scripts" / "maintenance" / "cleanup_provenance_dups.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    folder = tmp_path / "papers" / "Review" / "10.1--a"
    folder.mkdir(parents=True)
    _prov(folder / "10.1--a.provenance.json", "2026-01-01T00:00:00Z")
    _prov(folder / "10.1--a.provenance.json.dup", "2026-03-01T00:00:00Z")
    _prov(folder / "10.1--a.provenance.json.dup2", "2026-02-01T00:00:00Z")
    assert mod.main(["--store", str(tmp_path / "papers")]) == 0
    assert len(list(folder.iterdir())) == 3  # dry run touches nothing
    assert mod.main(["--store", str(tmp_path / "papers"), "--execute"]) == 0
    assert [p.name for p in folder.iterdir()] == ["10.1--a.provenance.json"]
    assert "2026-03" in (folder / "10.1--a.provenance.json").read_text()


# ── B4: the configured root is honoured everywhere ─────────────────────────

def test_relink_buckets_with_configured_store(project, tmp_path):
    external = tmp_path / "external_store"
    cfg = {"dirs": {"base_pdf_dir": str(external)}}
    old = make_paper(external, "_unclassified", "10.1--a", body=True)
    bucket = project / "eligible_rct"
    bucket.mkdir()
    (bucket / "10.1--a").symlink_to(os.path.relpath(old, bucket), target_is_directory=True)
    paper_store.refile(project, {"10.1/a": "Clinical Trial"}, execute=True, config=cfg)
    assert not (bucket / "10.1--a").exists()  # dangling until relinked
    assert paper_store.relink_buckets(project, config=cfg) == 1
    assert (bucket / "10.1--a" / "body.md").exists()


def test_remember_root_records_configured_store(project, tmp_path, monkeypatch):
    monkeypatch.delenv("BER_MANAGED_RUN", raising=False)
    external = tmp_path / "ext"
    cfg = {"dirs": {"base_pdf_dir": str(external)}}
    paper_store.remember_root(project, cfg)
    assert paper_store.papers_root(project) == external.resolve()


def test_remember_root_is_server_owned_in_managed_runs(project, tmp_path, monkeypatch):
    monkeypatch.setenv("BER_MANAGED_RUN", "1")
    paper_store.remember_root(project, {"dirs": {"base_pdf_dir": str(tmp_path / "x")}})
    assert not (project / ".paper-store.json").exists()


def test_migration_honours_base_pdf_dir(project, tmp_path):
    external = tmp_path / "ext"
    legacy = project / "converted" / "10.1--a"
    legacy.mkdir(parents=True)
    (legacy / "body.md").write_text("x")
    cfg = {"dirs": {"base_pdf_dir": str(external)}}
    plan = migrate_to_paper_store.plan_migration(project, config=cfg)
    migrate_to_paper_store.apply_migration(plan)
    assert (external / paper_store.UNCLASSIFIED / "10.1--a" / "body.md").exists()
    assert not (project / "papers").exists()


# ── B32: folder moves wait for (or skip past) concurrent writers ───────────

def test_refile_skips_while_a_writer_holds_the_store(project, capsys):
    store = project / "papers"
    make_paper(store, paper_store.INCOMING, "10.1--a")
    with paper_store.store_lock(store, exclusive=False) as taken:
        assert taken
        moves = paper_store.refile(project, {"10.1/a": "Review"}, execute=True,
                                   lock_timeout=0)
    assert moves == []
    assert (store / paper_store.INCOMING / "10.1--a").is_dir()
    assert "busy" in capsys.readouterr().err
    assert paper_store.refile(project, {"10.1/a": "Review"}, execute=True)
    assert (store / "Review" / "10.1--a").is_dir()


def test_shared_locks_do_not_block_each_other(project):
    store = project / "papers"
    with paper_store.store_lock(store, exclusive=False) as a:
        with paper_store.store_lock(store, exclusive=False, timeout=0) as b:
            assert a and b
