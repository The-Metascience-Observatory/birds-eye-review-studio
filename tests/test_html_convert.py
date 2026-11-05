"""Publisher HTML -> body.md, tables embedded as HTML.

A paper whose only full text was HTML was invisible to extraction: PaperRef
binds md/xml/pdf and nothing opened a .fulltext.html. 45 papers in one long
COVID corpus. Converting to body.md makes them visible with no change to
discover_papers, read_best_text or either extraction pass.
"""
from pathlib import Path

import pytest

pytest.importorskip("docling_core", reason="Install the conversion extra for HTML conversion tests")

from ber_core import html_convert
from ber_core.html_convert import count_tables, find_html_only_papers


def _paper(root: Path, bucket: str, stem: str, files: dict[str, str]) -> Path:
    d = root / bucket / stem
    d.mkdir(parents=True)
    for name, content in files.items():
        (d / name).write_text(content)
    return d


class TestCountTables:
    """Shared definition -- the convert report and the evidence row must agree."""

    def test_counts_embedded_html_tables(self):
        assert count_tables("text\n<table><tr><td>a</td></tr></table>\n") == 1
        assert count_tables("<table>x</table><table>y</table>") == 2

    def test_counts_a_real_pipe_table(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
        assert count_tables(md) == 1

    def test_ignores_a_stray_pipe_line(self):
        """Fewer than 4 pipe rows is not a table (matches evidence.py)."""
        assert count_tables("| not a table |\n") == 0

    def test_case_insensitive_and_empty(self):
        assert count_tables("<TABLE><TR><TD>a</TD></TR></TABLE>") == 1
        assert count_tables("") == 0
        assert count_tables(None) == 0


class TestFindHtmlOnlyPapers:
    def test_selects_a_paper_whose_only_text_is_html(self, tmp_path):
        d = _paper(tmp_path, "Clinical_Trial", "10.1--a",
                   {"10.1--a.fulltext.html": "<html><body>x</body></html>"})
        got = find_html_only_papers({"10.1/a": d})
        assert [(doi, p.name) for doi, _, p in got] == [("10.1/a", "10.1--a.fulltext.html")]

    def test_skips_when_body_md_exists(self, tmp_path):
        d = _paper(tmp_path, "Clinical_Trial", "10.1--b",
                   {"10.1--b.fulltext.html": "<html/>", "body.md": "# already"})
        assert find_html_only_papers({"10.1/b": d}) == []

    def test_skips_when_xml_exists(self, tmp_path):
        """Publisher JATS is richer, and xml_body_text already keeps its tables."""
        d = _paper(tmp_path, "Clinical_Trial", "10.1--c",
                   {"10.1--c.fulltext.html": "<html/>", "10.1--c.xml": "<article/>"})
        assert find_html_only_papers({"10.1/c": d}) == []

    def test_skips_when_a_pdf_exists(self, tmp_path):
        """Regression: converting here would make convert_pdfs skip the PDF.

        Its work list is "has a PDF and no body.md", so writing body.md from
        HTML would silently preempt the merged GROBID+docling conversion.
        """
        d = _paper(tmp_path, "Clinical_Trial", "10.1--d",
                   {"10.1--d.fulltext.html": "<html/>", "10.1--d.pdf": "%PDF-1.7"})
        assert find_html_only_papers({"10.1/d": d}) == []

    def test_ignores_landing_pages_and_dups(self, tmp_path):
        d = _paper(tmp_path, "Clinical_Trial", "10.1--e",
                   {"10.1--e.landing.html": "<html/>",
                    "10.1--e.fulltext.html.dup": "<html/>"})
        assert find_html_only_papers({"10.1/e": d}) == []

    def test_prefers_fulltext_html_over_other_html(self, tmp_path):
        d = _paper(tmp_path, "Clinical_Trial", "10.1--f",
                   {"10.1--f.html": "<html>" + "x" * 5000 + "</html>",
                    "10.1--f.fulltext.html": "<html>small</html>"})
        got = find_html_only_papers({"10.1/f": d})
        assert got[0][2].name == "10.1--f.fulltext.html"


class TestHtmlTableSerializer:
    def test_is_a_real_base_subclass(self):
        """MarkdownDocSerializer is a pydantic model that validates the type."""
        from docling_core.transforms.serializer.base import BaseTableSerializer
        assert issubclass(html_convert.HtmlTableSerializer, BaseTableSerializer)


class TestHtmlToMarkdown:
    def test_missing_file_returns_none(self, tmp_path):
        assert html_convert.html_to_markdown(tmp_path / "nope.html") is None

    def test_converts_and_keeps_table_markup(self, tmp_path):
        """End to end through docling. Tables must survive as HTML."""
        src = tmp_path / "p.html"
        src.write_text(
            "<html><body><h1>Trial</h1><p>" + ("Prose. " * 100) + "</p>"
            "<table><tr><th colspan='2'>Arm</th></tr>"
            "<tr><td>Drug</td><td>10</td></tr></table></body></html>")
        md = html_convert.html_to_markdown(src)
        assert md is not None
        assert "<table" in md.lower()
        assert "&lt;table" not in md          # must not be entity-escaped
        assert count_tables(md) >= 1

    def test_boilerplate_below_min_chars_is_rejected(self, tmp_path):
        """A consent wall must not be written as body.md."""
        src = tmp_path / "wall.html"
        src.write_text("<html><body><p>Accept cookies</p></body></html>")
        assert html_convert.html_to_markdown(src) is None


class TestIsGarbledIgnoresMarkup:
    """is_garbled gates EVERY paper, so both directions matter.

    It counts tokens of 41+ non-space chars as run-on words. Publisher HTML
    converted to markdown is full of legitimately long unspaced tokens --
    embedded <table> markup, absolute URLs, and relative navigation targets
    like "](/jid/search-results?f_Authors=Jared+C+Durieux)". One 16k body had
    76 of them against a threshold of 10, so all 45 HTML papers were discovered,
    bound to an md_path, then silently dropped by read_best_text.
    """

    def test_embedded_html_tables_do_not_trip_it(self):
        from ber_core.utils import is_garbled
        body = "Prose about the trial. " * 40
        table = ("<table><tbody>" + "".join(
            f"<tr><td>Arm {i}</td><td><p>value {i}</p></td></tr>" for i in range(40)
        ) + "</tbody></table>")
        assert is_garbled(body + table) is False

    def test_absolute_and_relative_links_do_not_trip_it(self):
        from ber_core.utils import is_garbled
        body = "Real prose here. " * 40
        links = "\n".join(
            f"[Academic](/jid/search-results?f_Authors=Author+Name+Number{i})"
            for i in range(30))
        urls = "\n".join(
            f"[Archive](https://medbio.ejournal.by/jour/issue/archive/{i}/long/path)"
            for i in range(30))
        assert is_garbled(body + links + urls) is False

    def test_real_ligature_corruption_is_still_caught(self):
        """The failure mode the check exists for must survive the fix."""
        from ber_core.utils import is_garbled
        assert is_garbled("text /uniFB01 " * 10) is True

    def test_real_run_on_corruption_is_still_caught(self):
        """Mojibake: a real corpus paper decoded Cyrillic as MacRoman."""
        from ber_core.utils import is_garbled
        garbled = " ".join(["-í-æ-∑-º-æ-∂-Ω-æ-Å-Ç-∏-ø-µ-Ä-Å-æ-Ω-∏-Ñ-∏-Ü-∏-Ä-æ-≤"] * 20)
        assert is_garbled(garbled) is True

    def test_ordinary_markdown_is_unaffected(self):
        from ber_core.utils import is_garbled
        assert is_garbled("# Title\n\nNormal prose with short words.\n") is False


class TestStripNavigation:
    """Publisher chrome is 4.3% of converted body text -- tokens paid on every
    extraction. Stripping it must never touch content: verified across 45 real
    papers that zero tables and zero real numbers are lost (all 145 numbers that
    disappeared came from nav URLs, e.g. AcademicSubjects/MED00160)."""

    def test_author_search_link_keeps_the_name(self):
        from ber_core.html_convert import strip_navigation
        md = "By [Jared C Durieux](/jid/search-results?f_Authors=Jared+C+Durieux) et al."
        out = strip_navigation(md)
        assert "Jared C Durieux" in out          # the name is real content
        assert "search-results" not in out       # the URL is not

    def test_obfuscated_email_stub_is_removed(self):
        from ber_core.html_convert import strip_navigation
        md = "Correspondence [[email protected]](/cdn-cgi/l/email-protection#2e5c4f5f) here."
        out = strip_navigation(md)
        assert "cdn-cgi" not in out and "2e5c4f5f" not in out

    def test_nav_only_lines_go_but_prose_stays(self):
        from ber_core.html_convert import strip_navigation
        md = "Real prose.\n[Archives](https://x.org/jour/issue/archive)\nMore prose.\n"
        out = strip_navigation(md)
        assert "Real prose." in out and "More prose." in out
        assert "issue/archive" not in out

    def test_tables_and_real_links_survive(self):
        """A reference or data-availability link must NOT be stripped."""
        from ber_core.html_convert import strip_navigation
        md = ("<table><tr><td>12.5</td></tr></table>\n"
              "Data at [Zenodo](https://zenodo.org/record/123456).\n"
              "See [Smith 2024](https://doi.org/10.1000/xyz).\n")
        out = strip_navigation(md)
        assert "<table" in out and "12.5" in out
        assert "zenodo.org/record/123456" in out
        assert "10.1000/xyz" in out

    def test_empty_input(self):
        from ber_core.html_convert import strip_navigation
        assert strip_navigation("") == ""
        assert strip_navigation(None) is None
