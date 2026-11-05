"""Tests for ``migrate_to_paper_store.py``.

The contract: after migrating, every paper the pipeline could previously find is
still findable, with the same artifacts, and every ``eligible_*`` symlink still
resolves. Nothing may be lost, and running it twice must be a no-op.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from ber_core import migrate_to_paper_store as mig
from ber_core import paper_store
from ber_core.utils import discover_papers


# --------------------------------------------------------------------------
# A synthetic legacy project
# --------------------------------------------------------------------------

@pytest.fixture()
def legacy(tmp_path: Path) -> Path:
    """Build a project in the OLD layout, covering every case that occurs:

    - a converted paper (PDF moved into converted/ by convert_pdfs)
    - a downloaded-but-unconverted paper (flat files in pdfs/)
    - a paper with both a converted folder AND stray flat artifacts
    - a paper with no classifications row
    - an eligible_rct symlink into converted/
    - run-level reports that belong to no paper
    """
    project = tmp_path / "projects" / "demo"
    (project / "data").mkdir(parents=True)
    pdfs = project / "pdfs"
    pdfs.mkdir()
    converted = project / "converted"
    converted.mkdir()

    # 1. converted paper, PDF already moved in
    d = converted / "10.1000--converted"
    d.mkdir()
    (d / "body.md").write_text("converted body", encoding="utf-8")
    (d / "provenance.json").write_text('{"extraction_mode": "merged-grobid-docling"}',
                                       encoding="utf-8")
    (d / "10.1000--converted.pdf").write_bytes(b"%PDF converted")

    # 2. downloaded but not converted — flat artifacts
    (pdfs / "10.1000--fetched.pdf").write_bytes(b"%PDF fetched")
    (pdfs / "10.1000--fetched.xml").write_text("<article/>", encoding="utf-8")
    (pdfs / "10.1000--fetched_supplementary_info.json").write_text("{}", encoding="utf-8")

    # 3. converted AND leftover flat artifacts (the common half-state)
    d2 = converted / "10.1000--both"
    d2.mkdir()
    (d2 / "body.md").write_text("both body", encoding="utf-8")
    (pdfs / "10.1000--both.xml").write_text("<article/>", encoding="utf-8")

    # 4. no classifications row
    (pdfs / "10.1000--unscreened.pdf").write_bytes(b"%PDF unscreened")

    # 5. bucket symlink into converted/
    bucket = project / "eligible_rct"
    bucket.mkdir()
    (bucket / "10.1000--converted").symlink_to(
        Path("..") / "converted" / "10.1000--converted", target_is_directory=True)

    # 6. run-level reports
    (pdfs / "missing_pdfs.html").write_text("<html/>", encoding="utf-8")
    (pdfs / "failed_dois.csv").write_text("doi\n", encoding="utf-8")

    with open(project / "data" / "classifications.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["doi", "article_type"])
        w.writeheader()
        w.writerow({"doi": "10.1000--converted", "article_type": "Clinical Trial"})
        w.writerow({"doi": "10.1000--fetched", "article_type": "Original Research"})
        w.writerow({"doi": "10.1000--both", "article_type": "Case Report/Series"})

    return project


def run(project: Path) -> mig.MigrationPlan:
    plan = mig.plan_migration(project)
    mig.apply_migration(plan)
    paper_store.relink_buckets(project)
    return plan


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def test_plan_finds_every_paper_across_both_legacy_trees(legacy):
    plan = mig.plan_migration(legacy)
    assert {pp.stem for pp in plan.papers} == {
        "10.1000--converted", "10.1000--fetched",
        "10.1000--both", "10.1000--unscreened",
    }


def test_plan_routes_papers_to_their_classified_type(legacy):
    plan = mig.plan_migration(legacy)
    dest = {pp.stem: pp.dest.parent.name for pp in plan.papers}
    assert dest["10.1000--converted"] == "Clinical_Trial"
    assert dest["10.1000--fetched"] == "Original_Research"
    assert dest["10.1000--both"] == "Case_Report_Series"
    assert dest["10.1000--unscreened"] == paper_store.UNCLASSIFIED


def test_dry_run_touches_nothing(legacy):
    mig.plan_migration(legacy)
    assert (legacy / "converted" / "10.1000--converted" / "body.md").is_file()
    assert not (legacy / "papers").exists()


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def test_migration_preserves_every_artifact(legacy):
    run(legacy)

    ct = legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
    assert (ct / "body.md").read_text() == "converted body"
    assert (ct / "provenance.json").is_file()
    assert (ct / "10.1000--converted.pdf").is_file()

    orig = legacy / "papers" / "Original_Research" / "10.1000--fetched"
    assert {p.name for p in orig.iterdir()} == {
        "10.1000--fetched.pdf", "10.1000--fetched.xml",
        "10.1000--fetched_supplementary_info.json",
    }


def test_converted_folder_and_stray_flat_files_merge_into_one_folder(legacy):
    """The half-state — a converted paper whose XML never got installed."""
    run(legacy)

    d = legacy / "papers" / "Case_Report_Series" / "10.1000--both"
    assert (d / "body.md").is_file()
    assert (d / "10.1000--both.xml").is_file()


def test_migration_empties_the_legacy_trees(legacy):
    run(legacy)

    leftover_pdfs = [p.name for p in (legacy / "pdfs").iterdir()]
    assert (legacy / "converted").exists() and not any((legacy / "converted").iterdir())
    assert leftover_pdfs == [], f"paper artifacts left behind: {leftover_pdfs}"


def test_run_level_reports_move_to_staging(legacy):
    """missing_pdfs.html belongs to no paper but must stay reachable."""
    run(legacy)

    staging = legacy / "papers" / paper_store.INCOMING
    assert (staging / "missing_pdfs.html").is_file()
    assert (staging / "failed_dois.csv").is_file()


def test_bucket_symlinks_still_resolve(legacy):
    """eligible_*/ links are pass-2's input; a dangling one silently drops a
    paper from extraction."""
    run(legacy)

    link = legacy / "eligible_rct" / "10.1000--converted"
    assert link.is_symlink()
    assert link.resolve().is_dir()
    assert (link / "body.md").read_text() == "converted body"
    assert not os.path.isabs(os.readlink(link))


def test_discover_papers_sees_the_same_corpus_after_migration(legacy):
    """The real equivalence check: what the pipeline reads must not change."""
    before = {r.stem for r in discover_papers(legacy / "converted")}
    before |= {"10.1000--fetched", "10.1000--unscreened"}  # flat, undiscoverable before

    run(legacy)
    after = {r.stem for r in discover_papers(legacy / "papers")}

    assert after == before


def test_migration_is_idempotent(legacy):
    run(legacy)
    second = mig.plan_migration(legacy)
    assert second.is_empty


def test_verification_passes_on_a_clean_migration(legacy):
    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)
    assert mig.verify_migration(plan) == []


def test_hardlinked_pdfs_survive_migration(legacy, tmp_path):
    """Corpora seeded by hardlinking must not be copied (disk) or broken."""
    source = tmp_path / "seed.pdf"
    source.write_bytes(b"%PDF seed")
    os.link(source, legacy / "pdfs" / "10.1000--linked.pdf")

    run(legacy)

    moved = (legacy / "papers" / paper_store.UNCLASSIFIED
             / "10.1000--linked" / "10.1000--linked.pdf")
    assert moved.stat().st_ino == source.stat().st_ino


def test_needs_migration_flips_after_running(legacy):
    assert mig.needs_migration(legacy)
    run(legacy)
    assert not mig.needs_migration(legacy)


def test_remove_legacy_only_deletes_empty_dirs(legacy):
    run(legacy)
    assert set(mig.remove_legacy_dirs(legacy)) == {"pdfs", "converted"}
    assert not (legacy / "pdfs").exists()


def test_remove_legacy_works_on_an_already_migrated_project(legacy, monkeypatch, capsys):
    """An already-migrated project is exactly when --remove-legacy is useful, so
    the "nothing to migrate" early return must not skip it."""
    run(legacy)
    monkeypatch.setattr("sys.argv", ["migrate", "--project-dir", str(legacy),
                                     "--execute", "--remove-legacy"])

    assert mig.main() == 0

    assert not (legacy / "converted").exists()
    assert "Removed empty legacy dir" in capsys.readouterr().out


def test_remove_legacy_says_why_a_dir_was_kept(legacy, monkeypatch, capsys):
    (legacy / "pdfs" / "16548370.provenance.json").write_text("{}", encoding="utf-8")
    run(legacy)
    monkeypatch.setattr("sys.argv", ["migrate", "--project-dir", str(legacy),
                                     "--execute", "--remove-legacy"])

    mig.main()

    out = capsys.readouterr().out
    assert "Kept pdfs/" in out
    assert (legacy / "pdfs" / "16548370.provenance.json").is_file()


def test_remove_legacy_keeps_a_dir_with_unknown_files(legacy):
    (legacy / "pdfs" / "notes.txt").write_text("mine", encoding="utf-8")
    run(legacy)

    removed = mig.remove_legacy_dirs(legacy)

    assert "pdfs" not in removed
    assert (legacy / "pdfs" / "notes.txt").is_file()


def test_migrate_in_place_is_a_one_call_entry_point(legacy):
    mig.migrate_in_place(legacy, quiet=True)
    assert (legacy / "papers" / "Clinical_Trial" / "10.1000--converted").is_dir()


# --------------------------------------------------------------------------
# Half-migrated states
# --------------------------------------------------------------------------

def test_papers_already_in_the_store_are_left_alone(legacy):
    run(legacy)
    ct = legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
    (ct / "body.md").write_text("EDITED", encoding="utf-8")

    run(legacy)

    assert (ct / "body.md").read_text() == "EDITED"


def test_stray_flat_artifacts_join_an_already_migrated_paper(legacy):
    """A fetch that ran mid-migration writes flat files for a filed paper."""
    run(legacy)
    (legacy / "pdfs").mkdir(exist_ok=True)
    (legacy / "pdfs" / "10.1000--converted.xml").write_text("<a/>", encoding="utf-8")

    run(legacy)

    assert (legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
            / "10.1000--converted.xml").is_file()


def test_existing_destination_file_wins_over_a_flat_duplicate(legacy):
    """converted/'s copy is the pipeline's resumable state; the flat one is a
    leftover from before the convert moved it."""
    (legacy / "pdfs" / "10.1000--converted.pdf").write_bytes(b"%PDF stale")

    run(legacy)

    kept = (legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
            / "10.1000--converted.pdf")
    assert kept.read_bytes() == b"%PDF converted"


# --- collisions must never destroy distinct content -------------------------

def test_an_identical_colliding_file_is_dropped(legacy):
    """A true duplicate costs nothing to delete and keeps the folder clean."""
    (legacy / "pdfs" / "10.1000--converted.pdf").write_bytes(b"%PDF converted")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    d = legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
    assert [p.name for p in d.glob("*.pdf")] == ["10.1000--converted.pdf"]
    assert plan.duplicates == []


def test_a_differing_colliding_file_is_kept_not_deleted(legacy):
    """Two distinct PDFs for one DOI is a real state (a re-download that
    resolved elsewhere). The migration must not be what decides one is junk."""
    (legacy / "pdfs" / "10.1000--converted.pdf").write_bytes(b"%PDF a different file")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    d = legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
    assert (d / "10.1000--converted.pdf").read_bytes() == b"%PDF converted"
    assert (d / "10.1000--converted.pdf.dup").read_bytes() == b"%PDF a different file"
    assert plan.duplicates == [d / "10.1000--converted.pdf.dup"]


def test_a_dup_name_that_is_taken_gets_a_counter(legacy):
    d = legacy / "converted" / "10.1000--converted"
    (d / "10.1000--converted.pdf.dup").write_bytes(b"%PDF earlier dup")
    (legacy / "pdfs" / "10.1000--converted.pdf").write_bytes(b"%PDF another")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    out = legacy / "papers" / "Clinical_Trial" / "10.1000--converted"
    assert (out / "10.1000--converted.pdf.dup").read_bytes() == b"%PDF earlier dup"
    assert (out / "10.1000--converted.pdf.dup2").read_bytes() == b"%PDF another"


def test_same_size_flat_collision_keeps_both_pdfs(legacy):
    original = b"%PDF converted"
    incoming = b"%PDF different"
    assert len(original) == len(incoming)
    (legacy / "pdfs/10.1000--converted.pdf").write_bytes(incoming)

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    folder = paper_store.paper_dir(legacy, "10.1000/converted")
    assert (folder / "10.1000--converted.pdf").read_bytes() == original
    assert (folder / "10.1000--converted.pdf.dup").read_bytes() == incoming


def test_partial_migration_merges_remaining_converted_artifacts(legacy):
    filed = legacy / "papers/Clinical_Trial/10.1000--converted"
    filed.mkdir(parents=True)
    (filed / "body.md").write_text("filed body")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    assert (filed / "body.md").read_text() == "filed body"
    assert (filed / "body.md.dup").read_text() == "converted body"
    assert (filed / "provenance.json").is_file()
    assert mig.verify_migration(plan) == []


def test_migration_preserves_conflicting_run_reports(legacy):
    incoming = paper_store.incoming_dir(legacy)
    (incoming / "missing_pdfs.html").write_text("filed report")
    (legacy / "pdfs/missing_pdfs.html").write_text("legacy report")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    assert (incoming / "missing_pdfs.html").read_text() == "filed report"
    assert (incoming / "missing_pdfs.html.dup").read_text() == "legacy report"


# --- files with no identity are reported, never silently skipped ------------

def test_unidentifiable_files_are_reported_and_left_alone(legacy):
    """Older corpora hold sidecars named by a BARE numeric PMID, which is
    neither a DOI nor a `pmid_` stem. They belong to no paper, so they cannot be
    filed — but "migration complete" must not quietly mean "except these"."""
    (legacy / "pdfs" / "16548370.provenance.json").write_text("{}", encoding="utf-8")
    (legacy / "pdfs" / "20968198.xml").write_text("<a/>", encoding="utf-8")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    assert {p.name for p in plan.unidentified} == {
        "16548370.provenance.json", "20968198.xml"}
    assert (legacy / "pdfs" / "16548370.provenance.json").is_file(), \
        "left in place, not deleted"


def test_run_level_reports_are_not_counted_as_unidentifiable(legacy):
    plan = mig.plan_migration(legacy)
    assert plan.unidentified == []


def test_grey_literature_dirs_are_reported_and_left_alone(legacy):
    """Hand-added entries like `grey:miller-2023` have no derivable DOI stem, so
    they cannot be filed — but they are still linked from eligible_* buckets and
    must keep working."""
    grey = legacy / "converted" / "grey:miller-2023-thai-prevention"
    grey.mkdir()
    (grey / "body.md").write_text("grey lit", encoding="utf-8")

    plan = mig.plan_migration(legacy)
    mig.apply_migration(plan)

    assert [p.name for p in plan.unidentified] == ["grey:miller-2023-thai-prevention"]
    assert (grey / "body.md").read_text() == "grey lit"


def test_the_converter_checkpoint_is_not_reported_as_unidentifiable(legacy):
    (legacy / "converted" / ".pdf4llm_checkpoint.json").write_text("{}",
                                                                  encoding="utf-8")
    plan = mig.plan_migration(legacy)
    assert plan.unidentified == []


def test_the_converter_checkpoint_moves_into_the_store(legacy):
    (legacy / "converted" / ".pdf4llm_checkpoint.json").write_text(
        '{"processed": []}', encoding="utf-8")

    run(legacy)

    assert (legacy / "papers" / ".pdf4llm_checkpoint.json").is_file()
