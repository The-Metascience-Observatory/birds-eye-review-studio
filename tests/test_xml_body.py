"""Tests for utils.xml_body_text — the publisher-XML reader.

Why this reader exists: publisher XML is the only source that carries tables as
real markup. The PDF->markdown path renders them as pipe rows (spans lost) or,
under a `grobid-only` degradation, drops them entirely — which cost this project
1,708 degraded conversions and a trial whose two co-primary outcomes lived only
in Table 2.

Two ways the old implementation defeated that, both silent:

1. It looked for `{http://www.elsevier.com/xml/ja/dtd}body` by exact namespace,
   so EVERY JATS document — i.e. nearly all the XML we fetch — returned None.
2. Where it did match, `" ".join(body.itertext())` whitespace-joined every cell
   into one run-on line, which is strictly worse than the pipe tables it was
   supposed to improve on.

So the assertions below are mostly "the table structure is still there".
"""
from __future__ import annotations

import pytest

from ber_core.utils import (
    PaperRef, XML_MIN_BODY_CHARS, detect_xml_flavor, parse_xml_body,
    read_best_text, xml_body_text, xml_plain_text,
)

PROSE = ("Patients were randomised to treatment or placebo. "
         "Methods, results and conclusion are reported in full below. ") * 8


def _jats(body_inner: str, back: str = "") -> str:
    return (
        '<article xmlns:xlink="http://www.w3.org/1999/xlink" '
        'article-type="research-article">'
        "<front><journal-meta><journal-title>J</journal-title></journal-meta>"
        "<article-meta><contrib-group><contrib><name>Author</name></contrib>"
        "</contrib-group><aff>Somewhere</aff></article-meta></front>"
        f"<body>{body_inner}</body>"
        f"<back>{back}</back>"
        "</article>"
    )


XHTML_TABLE = (
    "<table-wrap id='T1'><label>Table 1</label>"
    "<caption><p>Primary outcome by arm</p></caption>"
    "<table><thead><tr>"
    '<th rowspan="2">Outcome</th><th colspan="2">Cerebrolysin</th>'
    "</tr><tr><th>Mean</th><th>SD</th></tr></thead>"
    "<tbody><tr><td>NIHSS</td><td>4.2</td><td>1.1</td></tr></tbody></table>"
    "</table-wrap>"
)

# The CALS model. fetchpdf's JATS->Markdown converter REFUSES these rather than
# flatten them, which is the single strongest argument for feeding raw markup.
CALS_TABLE = (
    "<table-wrap id='T2'><label>Table 2</label>"
    "<table frame='all'><tgroup cols='3'>"
    '<thead><row><entry namest="c1" nameend="c3">Adverse events</entry></row></thead>'
    "<tbody><row><entry>Nausea</entry><entry>3</entry><entry>1</entry></row>"
    "</tbody></tgroup></table></table-wrap>"
)


def _write(tmp_path, text, name="10.1--a.xml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- the namespace regression ----------------------------------------------

def test_jats_body_is_found_despite_the_namespace(tmp_path):
    """The regression test for the hardcoded Elsevier lookup."""
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>"))
    out = xml_body_text(path)
    assert out is not None
    assert "randomised" in xml_plain_text(out)


def test_namespaced_jats_is_found(tmp_path):
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>").replace(
        "<article ", '<article xmlns="http://jats.nlm.nih.gov" '))
    assert xml_body_text(path) is not None


def test_elsevier_still_parses(tmp_path):
    """No regression for the one flavor the old code did handle."""
    path = _write(tmp_path,
                  '<full-text-retrieval-response '
                  'xmlns:ja="http://www.elsevier.com/xml/ja/dtd">'
                  f"<ja:body><ja:sections><p>{PROSE}</p></ja:sections></ja:body>"
                  "</full-text-retrieval-response>")
    out = xml_body_text(path)
    assert out is not None and "randomised" in xml_plain_text(out)


def test_pmc_articleset_wrapper_works(tmp_path):
    path = _write(tmp_path,
                  f"<pmc-articleset>{_jats(f'<sec><p>{PROSE}</p></sec>')}"
                  "</pmc-articleset>")
    assert xml_body_text(path) is not None


def test_document_with_no_body_falls_back_to_the_root(tmp_path):
    path = _write(tmp_path, f"<doc><section><p>{PROSE}</p></section></doc>")
    out = xml_body_text(path)
    assert out is not None and "randomised" in xml_plain_text(out)


@pytest.mark.parametrize("xml,flavor", [
    (_jats("<p>x</p>"), "jats"),
    ('<r xmlns:ja="http://www.elsevier.com/xml/ja/dtd"><ja:body/></r>', "elsevier"),
    ("<doc><body><p>x</p></body></doc>", "generic"),
])
def test_flavor_detection(tmp_path, xml, flavor):
    import xml.etree.ElementTree as ET
    assert detect_xml_flavor(ET.fromstring(xml)) == flavor


# --- tables survive, which is the whole point -------------------------------

def test_xhtml_table_spans_survive_verbatim(tmp_path):
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>{XHTML_TABLE}"))
    out = xml_body_text(path)
    assert 'colspan="2"' in out
    assert 'rowspan="2"' in out
    assert "<td>4.2</td>" in out


def test_cals_table_survives_verbatim(tmp_path):
    """Raw markup keeps what the JATS->Markdown converter throws away.

    fetchpdf's normalize.jats_table_to_canonical refuses the CALS model rather
    than flatten a span, so routing XML through it would lose these tables
    entirely — the reason this reader serializes instead of converting.
    """
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>{CALS_TABLE}"))
    out = xml_body_text(path)
    assert 'namest="c1"' in out
    assert 'nameend="c3"' in out
    assert "<entry>Nausea</entry>" in out


def test_table_captions_and_labels_are_kept_with_the_table(tmp_path):
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>{XHTML_TABLE}"))
    out = xml_body_text(path)
    assert "Table 1" in out
    assert "Primary outcome by arm" in out


def test_include_tables_false_drops_them_but_keeps_prose(tmp_path):
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p>{XHTML_TABLE}</sec>"))
    out = xml_body_text(path, include_tables=False)
    assert "table-wrap" not in out
    assert "colspan" not in out
    assert "randomised" in xml_plain_text(out)


# --- pruning ----------------------------------------------------------------

def test_references_and_front_matter_are_pruned(tmp_path):
    back = ("<ref-list><ref id='R1'><mixed-citation>Smith et al, "
            "Journal of Things, 2019</mixed-citation></ref></ref-list>")
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>", back=back))
    out = xml_body_text(path)
    assert "Smith et al" not in out
    assert "ref-list" not in out
    assert "randomised" in xml_plain_text(out)


def test_jats_floats_group_tables_are_lifted_into_the_output(tmp_path):
    """Tables outside <body> must still reach the model.

    Both major flavors park them there — JATS as <floats-group>, Elsevier as
    <floats> — so a reader that walks only <body> returns a document with NO
    tables. Measured on a real Elsevier corpus: 9 of 9 full-text files keep
    every table in <floats> and none inside <body>, so without this the whole
    structured-text chain is a no-op for that publisher.
    """
    xml = _jats(f"<sec><p>{PROSE}</p></sec>").replace(
        "<back>", f"<floats-group>{XHTML_TABLE}</floats-group><back>")
    out = xml_body_text(_write(tmp_path, xml))
    assert 'colspan="2"' in out
    assert "<td>4.2</td>" in out


def test_elsevier_floats_tables_are_lifted(tmp_path):
    """The real-world shape: <floats> is a SIBLING of <ja:body>."""
    path = _write(tmp_path,
                  '<full-text-retrieval-response '
                  'xmlns:ja="http://www.elsevier.com/xml/ja/dtd" '
                  'xmlns:ce="http://www.elsevier.com/xml/common/dtd">'
                  f"<ja:article><ja:body><ce:sections><ce:para>{PROSE}"
                  "</ce:para></ce:sections></ja:body>"
                  f"<ce:floats>{CALS_TABLE}</ce:floats></ja:article>"
                  "</full-text-retrieval-response>")
    out = xml_body_text(path)
    assert out is not None
    assert 'namest="c1"' in out and 'nameend="c3"' in out
    assert "Nausea" in out


def test_floats_figures_are_not_lifted(tmp_path):
    """A <fig> only names an image the XML never contains — pure token cost."""
    xml = _jats(f"<sec><p>{PROSE}</p></sec>").replace(
        "<back>",
        '<floats-group><fig id="F1"><label>Figure 1</label>'
        '<graphic xlink:href="fx1.jpg"/></fig></floats-group><back>')
    out = xml_body_text(_write(tmp_path, xml))
    assert "fx1.jpg" not in out


def test_floats_tables_are_dropped_when_tables_are_excluded(tmp_path):
    xml = _jats(f"<sec><p>{PROSE}</p></sec>").replace(
        "<back>", f"<floats-group>{XHTML_TABLE}</floats-group><back>")
    out = xml_body_text(_write(tmp_path, xml), include_tables=False)
    assert "colspan" not in out


# --- the budget -------------------------------------------------------------

def test_budget_never_cuts_a_table_in_half(tmp_path):
    """A plain text[:max_chars] slice hands the model half a table with no
    signal that it is half — the worst failure mode for a table-first source."""
    body = "".join(f"<sec><p>{PROSE}</p></sec>{XHTML_TABLE}" for _ in range(12))
    path = _write(tmp_path, _jats(body))

    full = xml_body_text(path)
    out = xml_body_text(path, max_chars=len(full) // 3)

    assert len(out) <= len(full)
    assert out.count("<table-wrap") == out.count("</table-wrap>")
    assert "truncated" in out


def test_budget_prefers_tables_over_prose(tmp_path):
    body = "".join(f"<sec><p>{PROSE * 4}</p></sec>" for _ in range(8)) + XHTML_TABLE
    path = _write(tmp_path, _jats(body))
    out = xml_body_text(path, max_chars=len(PROSE) * 6)
    # The numbers are the point of the exercise; prose is what gets dropped.
    assert "colspan" in out
    assert "truncated" in out


def test_no_budget_returns_everything(tmp_path):
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>{XHTML_TABLE}"))
    out = xml_body_text(path, max_chars=None)
    assert "truncated" not in out


# --- floors and failure modes ----------------------------------------------

def test_too_little_text_is_none(tmp_path):
    path = _write(tmp_path, _jats("<sec><p>Short.</p></sec>"))
    assert xml_body_text(path) is None


def test_a_tag_only_document_is_none(tmp_path):
    """The floor measures TEXT content, not serialized length — otherwise a
    document that is all markup and no words would sail past it."""
    path = _write(tmp_path, _jats("<sec>" + "<p></p>" * 400 + "</sec>"))
    assert xml_body_text(path) is None


def test_malformed_xml_warns_and_returns_none(tmp_path, capsys):
    path = _write(tmp_path, "<article><body><p>unclosed")
    assert xml_body_text(path) is None
    assert "XML parse failed" in capsys.readouterr().err


def test_missing_file_is_none(tmp_path):
    assert xml_body_text(tmp_path / "nope.xml") is None


def test_parse_xml_body_is_still_the_public_name(tmp_path):
    path = _write(tmp_path, _jats(f"<sec><p>{PROSE}</p></sec>"))
    assert parse_xml_body(path) == xml_body_text(path)


# --- xml_plain_text ---------------------------------------------------------

def test_plain_text_strips_tags_for_quote_verification(tmp_path):
    """A quote taken from a rendered table is contiguous there but split by
    `</td><td>` in the markup, so verification needs a stripped haystack."""
    out = xml_plain_text("<table><tr><td>NIHSS score</td><td>4.2</td></tr></table>")
    assert "NIHSS score" in out
    assert "<td>" not in out


def test_plain_text_survives_a_non_wellformed_excerpt():
    # A budgeted excerpt is a concatenation of blocks, not one document.
    out = xml_plain_text("<p>one</p>\n<p>two</p>")
    assert "one" in out and "two" in out


# --- read_best_text ordering ------------------------------------------------

def _ref(tmp_path, md=None, xml=None, pdf=None):
    paths = {}
    for name, text in (("md", md), ("xml", xml)):
        if text is None:
            continue
        p = tmp_path / ("body.md" if name == "md" else "10.1--a.xml")
        p.write_text(text, encoding="utf-8")
        paths[name] = p
    return PaperRef(paper_id="10.1/a", stem="10.1--a", layout="nested",
                    md_path=paths.get("md"), xml_path=paths.get("xml"),
                    pdf_path=pdf, dir=tmp_path)


def test_xml_first_prefers_xml_when_both_exist(tmp_path):
    ref = _ref(tmp_path, md=f"# Paper\n\n{PROSE}", xml=_jats(
        f"<sec><p>{PROSE * 4}</p></sec>{XHTML_TABLE}"))
    text, source = read_best_text(ref)
    assert source == "xml"
    assert "colspan" in text


def test_md_first_is_still_available(tmp_path):
    ref = _ref(tmp_path, md=f"# Paper\n\n{PROSE}", xml=_jats(
        f"<sec><p>{PROSE * 4}</p></sec>"))
    _, source = read_best_text(ref, xml_first=False)
    assert source == "md"


def test_an_abstract_only_stub_loses_to_a_full_body(tmp_path):
    """A JATS record carrying only an abstract is ~1-2k chars and would
    otherwise beat a complete body.md purely by being first in the order."""
    stub = _jats(f"<sec><p>{PROSE}</p></sec>")
    ref = _ref(tmp_path, md=f"# Paper\n\n{PROSE * 30}", xml=stub)
    assert len(xml_plain_text(stub)) < XML_MIN_BODY_CHARS
    _, source = read_best_text(ref, xml_first=True)
    assert source == "md"


def test_a_stub_still_wins_when_it_is_the_only_source(tmp_path):
    """A stub beats nothing — an XML-only paper must still be extractable."""
    ref = _ref(tmp_path, xml=_jats(f"<sec><p>{PROSE}</p></sec>"))
    text, source = read_best_text(ref, xml_first=True)
    assert source == "xml" and text


def test_xml_tables_flag_reaches_the_reader(tmp_path):
    ref = _ref(tmp_path, xml=_jats(f"<sec><p>{PROSE * 4}</p>{XHTML_TABLE}</sec>"))
    text, source = read_best_text(ref, xml_first=True, xml_tables=False)
    assert source == "xml"
    assert "colspan" not in text


def test_xml_gets_its_own_budget(tmp_path):
    body = "".join(f"<sec><p>{PROSE}</p></sec>" for _ in range(20))
    ref = _ref(tmp_path, xml=_jats(body))
    long_text, _ = read_best_text(ref, xml_first=True, xml_max_chars=200_000)
    short_text, _ = read_best_text(ref, xml_first=True, max_chars=200_000,
                                   xml_max_chars=2_000)
    assert len(short_text) < len(long_text)
