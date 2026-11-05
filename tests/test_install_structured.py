"""Tests for install_structured.py.

The script no longer moves artifacts BETWEEN trees — the fetch writes them into
each paper's own folder in the store. What is left is tidying inside that folder,
and almost every way it can go wrong is silent:

* a differently-named XML in the right directory is simply invisible to
  ``utils.discover_papers`` (it looks for exactly ``<stem>.xml``);
* an ``extraction_mode`` value the table-quality gates don't recognize reads as
  DEGRADED, painting an amber "no tables captured" warning on the best source in
  the corpus.

The old "a structured-only dir masks a later PDF" hazard is gone by construction:
every paper has a folder from the moment it is fetched, and both pdf4llm's
``skip_existing`` and the convert step's work list key on ``body.md``, which
structured text never produces.

So the assertions here are mostly "the thing that reads this later can find it".
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from ber_core.utils import discover_papers, doi_to_filename


def _load():
    spec = importlib.util.spec_from_file_location("install_structured",
                                                  "steps/install_structured.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ins():
    return _load()


def test_equal_length_changed_source_is_installed_with_original_archived(ins, tmp_path):
    old = tmp_path / 'supplementary' / 'table.xml'
    old.parent.mkdir()
    old.write_bytes(b'<td>12</td>')
    incoming = tmp_path / 'table.xml'
    incoming.write_bytes(b'<td>21</td>')
    assert not ins._same_file(incoming,old)
    history = tmp_path / 'source_history'
    ins._place_artifact(incoming,old,history_dir=history)
    assert old.read_bytes() == b'<td>21</td>'
    assert [p.read_bytes() for p in history.glob('*/table.xml')] == [b'<td>12</td>']
    incoming.write_bytes(old.read_bytes())
    assert ins._same_file(incoming,old)


@pytest.fixture
def papers(tmp_path):
    """The store root of a project."""
    d = tmp_path / "projects" / "demo" / "papers"
    d.mkdir(parents=True)
    return d


JATS = (
    '<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">'
    "<front><article-meta><title-group><article-title>T</article-title>"
    "</title-group></article-meta></front>"
    "<body><sec><p>Methods and results with enough prose to matter.</p></sec></body>"
    "</article>"
)


def _paper(papers, stem, *, bucket="Clinical_Trial", converted=True,
           mode="merged-grobid-docling"):
    """A paper folder, optionally already PDF-converted."""
    d = papers / bucket / stem
    d.mkdir(parents=True)
    if converted:
        (d / "body.md").write_text("# Paper\n\nSome converted text.\n")
        (d / "provenance.json").write_text(json.dumps({"extraction_mode": mode}))
    return d


# --- indexing ---------------------------------------------------------------

def test_index_structured_groups_a_papers_artifacts(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / "10.1--a.xml").write_text(JATS)
    (d / "10.1--a.provenance.json").write_text('{"source": "europepmc_xml"}')
    (d / "10.1--a_supplementary_info.json").write_text("{}")
    (d / "10.1--a_supplementary_info_1_tableS1.xlsx").write_text("x")
    (d / "10.1--a.pdf").write_text("%PDF-")

    found = ins.index_structured(d)
    assert set(found) == {"10.1/a"}
    entry = found["10.1/a"]
    assert entry["xml"].name == "10.1--a.xml"
    assert entry["manifest"].name == "10.1--a_supplementary_info.json"
    assert [p.name for p in entry["supplements"]] == \
           ["10.1--a_supplementary_info_1_tableS1.xlsx"]


def test_index_structured_ignores_pdf_only_papers(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / "10.1--a.pdf").write_text("%PDF-")
    assert ins.index_structured(d) == {}


def test_index_store_walks_every_type_bucket(papers, ins):
    for bucket, stem in (("Clinical_Trial", "10.1--a"),
                         ("Original_Research", "10.1--b")):
        d = _paper(papers, stem, bucket=bucket)
        (d / f"{stem}.xml").write_text(JATS)

    entries = ins.index_store(papers)

    assert {p.name for p, _ in entries} == {"10.1--a", "10.1--b"}


def test_index_store_skips_already_filed_supplements(papers, ins):
    """A `supplementary/` subdir must not be re-scanned as new work, or every
    run would report the same paper as changed forever."""
    d = _paper(papers, "10.1--a")
    (d / "10.1--a.xml").write_text(JATS)
    sup = d / ins.SUPPLEMENT_DIRNAME
    sup.mkdir()
    (sup / "10.1--a_supplementary_info_1_tableS1.xlsx").write_text("x")

    entries = ins.index_store(papers)

    assert entries[0][1]["supplements"] == []


# --- the encoder mismatch ---------------------------------------------------

def test_xml_is_renamed_to_the_folders_own_stem(papers, ins):
    """fetchpdf named the XML; the migration named the folder.

    For a DOI containing ":" the two stems differ, so an XML sitting in the
    right folder under the wrong name is invisible to discover_papers.
    """
    doi = "10.1023/A:1234567"
    ours = doi_to_filename(doi)                 # 10.1023--A:1234567
    theirs = "10.1023--A~1234567"               # fetchpdf's encoding
    assert ours != theirs                       # guard: the case is real

    d = _paper(papers, ours)
    (d / f"{theirs}.xml").write_text(JATS)

    ins.install_one(d, ins.index_structured(d)["10.1023/a:1234567"], execute=True)

    assert (d / f"{ours}.xml").exists()
    assert not (d / f"{theirs}.xml").exists(), "the old name must not linger"


def test_renaming_does_not_duplicate_the_file(papers, ins):
    """A copy would double the folder's size and show two names for one file —
    exactly the clutter the store exists to remove."""
    d = _paper(papers, "10.1--a")
    (d / "10.1--other.xml").write_text(JATS)

    ins.install_one(d, ins.index_structured(d)["10.1/other"], execute=True)

    assert sorted(p.name for p in d.glob("*.xml")) == ["10.1--a.xml"]


def test_discover_papers_finds_the_installed_xml(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / "10.1--a.xml").write_text(JATS)

    ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    refs = discover_papers(papers)
    assert len(refs) == 1
    assert refs[0].xml_path is not None and refs[0].xml_path.exists()
    assert refs[0].md_path is not None          # the markdown fallback survives


# --- provenance -------------------------------------------------------------

def test_provenance_gains_structured_keys_and_keeps_extraction_mode(papers, ins):
    d = _paper(papers, "10.1--a", mode="grobid-only")
    (d / "10.1--a.xml").write_text(JATS)
    (d / "10.1--a.provenance.json").write_text('{"source": "europepmc_xml"}')

    ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    prov = json.loads((d / "provenance.json").read_text())
    assert prov["structured_source"] == "xml"
    assert prov["structured_format"] == "jats"
    assert prov["structured_file"] == "10.1--a.xml"
    assert prov["structured_provider"] == "europepmc_xml"
    assert prov["has_pdf_conversion"] is True
    # The PDF conversion's own verdict must survive untouched.
    assert prov["extraction_mode"] == "grobid-only"


def test_xml_only_paper_gets_no_extraction_mode(papers, ins):
    """A value the table-quality gates don't know reads as DEGRADED.

    So a paper whose only full text is XML must carry NO extraction_mode at all,
    not an invented one — then _mode_of returns None and every gate skips it.
    """
    d = _paper(papers, "10.1--a", converted=False)
    (d / "10.1--a.xml").write_text(JATS)

    outcome = ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    assert outcome == "installed"
    prov = json.loads((d / "provenance.json").read_text())
    assert "extraction_mode" not in prov
    assert prov["structured_source"] == "xml"
    assert prov["has_pdf_conversion"] is False


def test_xml_only_paper_is_visible_to_extraction(papers, ins):
    """The unlock: a paywalled PDF with a free JATS copy still reaches
    extraction, with no PDF ever converted."""
    d = _paper(papers, "10.1--a", converted=False)
    (d / "10.1--a.xml").write_text(JATS)

    ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    refs = discover_papers(papers)
    assert [r.paper_id for r in refs] == ["10.1/a"]
    assert refs[0].xml_path is not None
    assert refs[0].md_path is None              # no PDF was ever converted


def test_an_xml_only_paper_still_converts_when_its_pdf_arrives(papers, ins):
    """The hazard the old reconcile phase existed to undo, now structural: the
    convert step's work list keys on body.md, which XML never produces."""
    d = _paper(papers, "10.1--a", converted=False)
    (d / "10.1--a.xml").write_text(JATS)
    ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    (d / "10.1--a.pdf").write_text("%PDF-")     # arrives on a later fetch

    ref = discover_papers(papers)[0]
    assert ref.pdf_path is not None and ref.md_path is None, \
        "still counts as unconverted work"


# --- supplements ------------------------------------------------------------

def test_supplements_are_filed_and_counted(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / "10.1--a.xml").write_text(JATS)
    (d / "10.1--a_supplementary_info.json").write_text('{"status": "ok"}')
    (d / "10.1--a_supplementary_info_1_tableS1.xlsx").write_text("x")
    (d / "10.1--a_supplementary_info_2_protocol.pdf").write_text("y")

    ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    sup = d / ins.SUPPLEMENT_DIRNAME
    names = sorted(p.name for p in sup.iterdir())
    assert names == ["10.1--a_supplementary_info.json",
                     "10.1--a_supplementary_info_1_tableS1.xlsx",
                     "10.1--a_supplementary_info_2_protocol.pdf"]
    prov = json.loads((d / "provenance.json").read_text())
    assert prov["supplementary_files"] == 2      # the manifest is not a file
    assert not list(d.glob("*_supplementary_info_*")), \
        "supplements must leave the top level of the folder"


# --- idempotency + dry run --------------------------------------------------

def test_dry_run_writes_nothing(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / "10.1--other.xml").write_text(JATS)

    ins.install_one(d, ins.index_structured(d)["10.1/other"], execute=False)

    assert not (d / "10.1--a.xml").exists()
    assert (d / "10.1--other.xml").exists()
    prov = json.loads((d / "provenance.json").read_text())
    assert "structured_source" not in prov


def test_second_run_is_a_no_op(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / "10.1--a.xml").write_text(JATS)

    first = ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)
    second = ins.install_one(d, ins.index_structured(d)["10.1/a"], execute=True)

    assert first == "installed"
    assert second == "unchanged"


# --- stale markers from migrated projects -----------------------------------

def test_stale_structured_only_marker_is_cleared_once_a_pdf_exists(papers, ins):
    d = _paper(papers, "10.1--a")
    (d / ins.STRUCTURED_ONLY_MARKER).write_text("legacy\n")
    (d / "10.1--a.pdf").write_text("%PDF-")

    assert ins.clear_stale_stubs(papers, execute=True) == 1
    assert not (d / ins.STRUCTURED_ONLY_MARKER).exists()
    assert (d / "body.md").exists(), "the conversion itself is untouched"


def test_marker_is_kept_while_the_paper_still_has_no_pdf(papers, ins):
    d = _paper(papers, "10.1--a", converted=False)
    (d / ins.STRUCTURED_ONLY_MARKER).write_text("legacy\n")
    (d / "10.1--a.xml").write_text(JATS)

    assert ins.clear_stale_stubs(papers, execute=True) == 0
    assert (d / ins.STRUCTURED_ONLY_MARKER).exists()


# --- flavor sniffing --------------------------------------------------------

@pytest.mark.parametrize("head,flavor", [
    (JATS, "jats"),
    ('<full-text-retrieval-response xmlns:ja="http://www.elsevier.com/xml/ja/dtd">'
     "<ja:body><p>x</p></ja:body></full-text-retrieval-response>", "elsevier"),
    ("<doc><body><p>plain</p></body></doc>", "generic"),
])
def test_xml_flavor_detection(papers, ins, head, flavor):
    path = papers / "10.1--a.xml"
    path.write_text(head)
    assert ins._xml_flavor(path) == flavor
