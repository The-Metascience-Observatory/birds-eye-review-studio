"""Tests for download_pdfs' artifact-class resume logic.

The burn case this guards: the old predicate skipped a paper whose ``.pdf``
existed, BEFORE fetchpdf was ever called. So once a corpus had its PDFs, no
later run could fetch that corpus's XML or supplementary material — turning on
either toggle looked like it did nothing at all, forever, with no error.

The second thing under test is the encoder mismatch. fetchpdf names files with
``doi_to_safe_filename`` and this repo names directories with
``doi_to_filename``; they agree on ordinary DOIs and diverge on any DOI with a
":" or a literal "--". A ``.xml`` fetchpdf just wrote can therefore sit directly
beside a ``.pdf`` we named and never join to it.
"""
from __future__ import annotations

import importlib.util

import pytest

from ber_core.utils import decode_artifact_stem, doi_to_filename, norm_doi, strip_artifact_suffix


def _load_download():
    pytest.importorskip("fetchpdf", reason="Install the download extra for artifact integration tests")
    spec = importlib.util.spec_from_file_location("download_pdfs",
                                                  "steps/download_pdfs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dl():
    return _load_download()


@pytest.mark.parametrize("selection_exists", [False, True])
def test_missing_or_empty_allowlist_never_starts_download(dl, tmp_path, monkeypatch, selection_exists):
    from types import SimpleNamespace

    classifications = tmp_path / "classifications.csv"
    classifications.write_text("doi,article_type,is_relevant\n10.1000/x,Clinical Trial,yes\n")
    selection = tmp_path / "only.csv"
    if selection_exists:
        selection.write_text("doi\n")
    monkeypatch.setattr(dl, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(dl, "load_config", lambda _: {"topic": {"name": "Fixture"}})
    monkeypatch.setattr(dl, "batch_fetch_pdfs", lambda *a, **kw: pytest.fail("Must not fetch any paper"))
    args = SimpleNamespace(config="unused", input=str(classifications), output_dir=None,
        no_auto_migrate=True, types=None, relevant_only=False, structured=False,
        supplementary=False, refresh_supplementary=False, data_artifacts=False,
        max_si_mb=None, only_dois=str(selection))

    if selection_exists:
        dl.main(args)
    else:
        with pytest.raises(SystemExit) as error:
            dl.main(args)
        assert error.value.code == 2


# --- the cross-encoder decoder ---------------------------------------------


def test_default_download_filter_keeps_original_observational_labels(dl, tmp_path, monkeypatch):
    from types import SimpleNamespace

    classifications = tmp_path / 'classifications.csv'
    classifications.write_text(
        'doi,article_type,is_relevant\n'
        '10.1000/survey,Survey Study,yes\n'
        '10.1000/qualitative,Qualitative Study,yes\n'
        '10.1000/mixed,Mixed Methods Study,yes\n'
        '10.1000/review,Narrative Review,yes\n'
        '10.1000/offtopic,Survey Study,no\n')
    monkeypatch.setattr(dl, 'PROJECTS_DIR', tmp_path / 'projects')
    monkeypatch.setattr(dl, 'load_config', lambda _: {'topic': {'name': 'Fixture'}})
    monkeypatch.setattr(dl, 'load_extra_sources', lambda **_: [])
    monkeypatch.setattr(dl, '_import_fetchpdf', lambda: None)
    fetched = []
    monkeypatch.setattr(dl, '_fetch', lambda args, dois, *rest: fetched.extend(dois))
    monkeypatch.setattr(dl, '_file_and_report', lambda *args: None)
    args = SimpleNamespace(config='unused', input=str(classifications),
        output_dir=str(tmp_path/'papers'), no_auto_migrate=True, types=None,
        relevant_only=True, structured=False, supplementary=False,
        refresh_supplementary=False, data_artifacts=False, max_si_mb=None,
        only_dois=None, limit=0, no_extra_sources=True, workers=1, delay=0)
    dl.main(args)
    assert fetched == ['10.1000/survey', '10.1000/qualitative', '10.1000/mixed']

# (doi, fetchpdf's stem) — the encoder pairs that actually diverge.
ENCODER_CASES = [
    ("10.1371/journal.pone.0000308", "10.1371--journal.pone.0000308"),   # agree
    ("10.1023/A:1234567", "10.1023--A~1234567"),                          # ":"
    ("10.18260/1-2--47556", "10.18260--1-2~2d~~2d~47556"),                # "--"
    ("10.1002/(SICI)1097-0258(19970228)16:4<385::AID-SIM380>3.0.CO;2-3",
     "10.1002--(SICI)1097-0258(19970228)16~4~3c~385~~AID-SIM380~3e~3.0.CO;2-3"),
]


@pytest.mark.parametrize("doi,fetchpdf_stem", ENCODER_CASES)
def test_both_encoders_decode_to_the_same_join_key(doi, fetchpdf_stem):
    """Whichever encoder wrote the file, it must land on norm_doi's key."""
    assert decode_artifact_stem(fetchpdf_stem) == norm_doi(doi)
    assert decode_artifact_stem(doi_to_filename(doi)) == norm_doi(doi)


def test_decoding_is_case_insensitive():
    # classifications.csv stores whatever casing the source gave; disk stems
    # preserve it. Matching on raw strings (the old behaviour) silently missed
    # every paper whose casing differed.
    assert decode_artifact_stem("10.1001--JAMA.2020.1") == \
           decode_artifact_stem("10.1001--jama.2020.1")


@pytest.mark.parametrize("name,stem", [
    ("10.1--x.pdf", "10.1--x"),
    ("10.1--x.xml", "10.1--x"),
    # Path.stem gets this one wrong ("10.1--x.fulltext"), which would key the
    # HTML separately from its own PDF.
    ("10.1--x.fulltext.html", "10.1--x"),
    ("10.1--x_supplementary_info.json", "10.1--x"),
    ("10.1--x.provenance.json", "10.1--x"),
    ("10.1--x", "10.1--x"),
])
def test_strip_artifact_suffix_handles_multipart_suffixes(name, stem):
    assert strip_artifact_suffix(name) == stem


# --- artifact_index --------------------------------------------------------

def _project(tmp_path, files=(), converted=(), eligible=(), legacy_flat=()):
    """Build a project in the CURRENT layout. Returns (papers_dir, project_dir).

    *files* are artifact filenames, each filed into its own paper folder under
    ``papers/<Type>/<stem>/`` the way the store holds them. *converted*,
    *eligible* and *legacy_flat* build the legacy shapes the index must still
    recognise so an un-migrated project is not re-downloaded wholesale.
    """
    project_dir = tmp_path / "projects" / "demo"
    papers_dir = project_dir / "papers"
    papers_dir.mkdir(parents=True)
    for name in files:
        stem = strip_artifact_suffix(name)
        d = papers_dir / "Clinical_Trial" / stem
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("x")
    for name in legacy_flat:
        d = project_dir / "pdfs"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("x")
    for stem, names in converted:
        d = project_dir / "converted" / stem
        d.mkdir(parents=True)
        for name in names:
            (d / name).write_text("x")
    for bucket, stem, names in eligible:
        d = project_dir / f"eligible_{bucket}" / stem
        d.mkdir(parents=True)
        for name in names:
            (d / name).write_text("x")
    return papers_dir, project_dir


def test_index_classifies_each_artifact(tmp_path, dl):
    pdfs, proj = _project(tmp_path, files=[
        "10.1--a.pdf",
        "10.1--b.xml",
        "10.1--c.fulltext.html",
        "10.1--d_supplementary_info.json",
    ])
    idx = dl.artifact_index(pdfs, proj)
    assert idx["10.1/a"] == {dl.PDF}
    assert idx["10.1/b"] == {dl.STRUCTURED}
    assert idx["10.1/c"] == {dl.STRUCTURED}
    assert idx["10.1/d"] == {dl.SI}


def test_index_joins_artifacts_written_by_different_encoders(tmp_path, dl):
    """The whole point of decode_artifact_stem: one paper, two encoders."""
    doi = "10.1023/A:1234567"
    pdfs, proj = _project(tmp_path, files=[
        f"{doi_to_filename(doi)}.pdf",          # ours
        "10.1023--A~1234567.xml",               # fetchpdf's
    ])
    idx = dl.artifact_index(pdfs, proj)
    assert idx[norm_doi(doi)] == {dl.PDF, dl.STRUCTURED}


def test_index_finds_pdfs_in_the_legacy_layout(tmp_path, dl):
    # An un-migrated project still keeps its PDFs in converted/<stem>/ (where
    # the old convert step moved them) and linked under eligible_*/. Both must
    # still count as "have the PDF" or the first run re-fetches the corpus.
    papers, proj = _project(
        tmp_path,
        converted=[("10.1--a", ["10.1--a.pdf", "body.md"])],
        eligible=[("rct", "10.1--b", ["10.1--b.pdf"])],
    )
    idx = dl.artifact_index(papers, proj)
    assert dl.PDF in idx["10.1/a"]
    assert dl.PDF in idx["10.1/b"]


def test_index_finds_legacy_flat_artifacts(tmp_path, dl):
    """A project whose pdfs/ dump has not been migrated yet."""
    papers, proj = _project(tmp_path, legacy_flat=["10.1--a.pdf", "10.1--a.xml"])
    assert dl.artifact_index(papers, proj)["10.1/a"] == {dl.PDF, dl.STRUCTURED}


def test_index_finds_installed_xml(tmp_path, dl):
    # install_structured copies the XML into the paper's folder. Without this,
    # cleaning the store would make the corpus look unfetched and re-download it.
    papers, proj = _project(
        tmp_path, converted=[("10.1--a", ["10.1--a.xml", "body.md"])])
    assert dl.artifact_index(papers, proj)["10.1/a"] == {dl.STRUCTURED}


def test_index_finds_artifacts_across_type_buckets(tmp_path, dl):
    """The bucket a paper sits in must not affect whether it is seen."""
    papers, proj = _project(tmp_path)
    for bucket, stem in (("Clinical_Trial", "10.1--a"),
                         ("Original_Research", "10.1--b"),
                         ("_unclassified", "10.1--c")):
        d = papers / bucket / stem
        d.mkdir(parents=True)
        (d / f"{stem}.pdf").write_text("x")

    idx = dl.artifact_index(papers, proj)

    assert {"10.1/a", "10.1/b", "10.1/c"} <= set(idx)


def test_index_sees_staged_downloads(tmp_path, dl):
    """A batch that died before filing must not be re-downloaded."""
    papers, proj = _project(tmp_path)
    d = papers / "_incoming" / "10.1--a"
    d.mkdir(parents=True)
    (d / "10.1--a.pdf").write_text("x")

    assert dl.PDF in dl.artifact_index(papers, proj)["10.1/a"]


# --- staging seeding -------------------------------------------------------

def test_seed_staging_links_existing_artifacts_for_a_backfill(tmp_path, dl):
    """fetchpdf pre-fills satisfied goals from its own output dir. Without the
    seed, a supplementary backfill would re-download every PDF in the corpus."""
    papers, proj = _project(tmp_path, files=["10.1--a.pdf"])
    staging = papers / "_incoming"
    staging.mkdir()

    n = dl.seed_staging(staging, ["10.1/a"], proj, papers)

    linked = staging / "10.1--a" / "10.1--a.pdf"
    assert n == 1
    assert linked.is_file()
    original = papers / "Clinical_Trial" / "10.1--a" / "10.1--a.pdf"
    assert linked.stat().st_ino == original.stat().st_ino, "must be a hardlink"


def test_seed_staging_skips_papers_with_nothing_on_disk(tmp_path, dl):
    papers, proj = _project(tmp_path)
    staging = papers / "_incoming"
    staging.mkdir()

    assert dl.seed_staging(staging, ["10.1/absent"], proj, papers) == 0


def test_seed_staging_uses_fetchpdfs_encoder_for_the_folder_name(tmp_path, dl):
    """The folder must match the stems fetchpdf writes inside it, or fetchpdf
    creates a second folder and the pre-fill is missed anyway."""
    doi = "10.1023/A:1234567"
    papers, proj = _project(tmp_path, files=["10.1023--A~1234567.pdf"])
    staging = papers / "_incoming"
    staging.mkdir()

    dl.seed_staging(staging, [doi], proj, papers)

    assert (staging / "10.1023--A~1234567" / "10.1023--A~1234567.pdf").is_file()


# --- the resume predicate --------------------------------------------------

def _wanted(dl, structured=False, supplementary=False):
    w = {dl.PDF}
    if structured:
        w.add(dl.STRUCTURED)
    if supplementary:
        w.add(dl.SI)
    return w


def test_pdf_only_paper_is_skipped_when_only_a_pdf_is_wanted(tmp_path, dl):
    pdfs, proj = _project(tmp_path, files=["10.1--a.pdf"])
    have = dl.artifact_index(pdfs, proj)
    assert _wanted(dl) <= have["10.1/a"]


def test_pdf_only_paper_is_readmitted_when_structured_is_requested(tmp_path, dl):
    """The regression that motivated this whole predicate."""
    pdfs, proj = _project(tmp_path, files=["10.1--a.pdf"])
    have = dl.artifact_index(pdfs, proj)
    assert not _wanted(dl, structured=True) <= have["10.1/a"]


def test_pdf_only_paper_is_readmitted_when_supplements_are_requested(tmp_path, dl):
    pdfs, proj = _project(tmp_path, files=["10.1--a.pdf"])
    have = dl.artifact_index(pdfs, proj)
    assert not _wanted(dl, supplementary=True) <= have["10.1/a"]


def test_a_satisfied_paper_is_skipped_again_next_run(tmp_path, dl):
    """Re-runs must stay cheap once the backfill has happened.

    Note the SI manifest, not the SI files, is the satisfaction signal —
    fetchpdf writes it even when a paper genuinely has no supplements, which is
    what stops a supplement-less paper being retried forever.
    """
    pdfs, proj = _project(tmp_path, files=[
        "10.1--a.pdf", "10.1--a.xml", "10.1--a_supplementary_info.json",
    ])
    have = dl.artifact_index(pdfs, proj)
    assert _wanted(dl, structured=True, supplementary=True) <= have["10.1/a"]


def test_unknown_paper_has_no_classes(tmp_path, dl):
    pdfs, proj = _project(tmp_path, files=["10.1--a.pdf"])
    have = dl.artifact_index(pdfs, proj)
    assert have.get("10.1/nonexistent", set()) == set()
