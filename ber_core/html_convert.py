"""Publisher HTML -> markdown, with tables kept as embedded HTML.

Why this exists: a paper whose only full text is HTML was INVISIBLE to
extraction. `utils.PaperRef` binds `md_path` / `xml_path` / `pdf_path` and
nothing else, so `discover_papers` and `read_best_text` never opened a
`.fulltext.html`. The download counted as a success, `has_fulltext` went true,
and the paper contributed nothing — 45 of one long COVID corpus's papers, and
10 in cerebrolysin before that.

Rather than thread a fourth path through PaperRef, read_best_text,
discover_papers and both extraction passes, this converts HTML to the
`body.md` those consumers already read. The paper becomes visible with no
downstream change at all.

**Tables stay as HTML.** Docling's default markdown export renders tables as
pipe tables, which cannot express `rowspan`/`colspan` — precisely the structure
that clinical results tables (arm x timepoint, grouped headers) depend on. A
pipe table silently flattens a merged header cell into the wrong column, which
is how an outcome gets attributed to the wrong arm. Embedding the original HTML
keeps the spans intact AND is *cheaper*: on a real trial paper the HTML-table
render was 116,330 chars against 121,802 for pipe tables, because pipe tables
pad every cell to a common column width.
"""
from __future__ import annotations

import re
from pathlib import Path

# docling_core's serializer protocol only (~0.3s, no ML stack). The heavy
# `docling` import stays inside html_to_markdown so merely importing this
# module -- which convert_pdfs does at startup -- costs nothing.
from docling_core.transforms.serializer.base import (BaseTableSerializer,
                                                     SerializationResult)

# The canonical serializer lives in pdf4llm, which uses it for the PDF path;
# import it so the two cannot drift. The local definition below is a fallback
# for a checkout without pdf4llm (this module needs nothing else from it).
try:
    from pdf4llm.table_serializer import HtmlTableSerializer as _Pdf4llmHtmlTableSerializer
    _SERIALIZER_SOURCE = "pdf4llm"
except ImportError:
    _Pdf4llmHtmlTableSerializer = None
    _SERIALIZER_SOURCE = "local"

#: `extraction_mode` written into provenance.json for papers converted here.
#: Registered in the `_TABLE_BEARING_MODES` sets in `convert_pdfs.py` and
#: `server/app/services/evidence.py`, or every HTML paper would be reported as
#: a degraded, table-free conversion.
HTML_EXTRACTION_MODE = "docling-html"

#: Below this, the "markdown" is a cookie wall, a paywall stub or a redirect
#: page rather than an article. Matches `utils._XML_MIN_CHARS`.
_MIN_CHARS = 200


class _LocalHtmlTableSerializer(BaseTableSerializer):
    """Serialize each table as raw HTML instead of a markdown pipe table.

    Subclasses the ABSTRACT `BaseTableSerializer`, not the concrete
    `MarkdownTableSerializer`: we replace pipe-table rendering wholesale, and
    inheriting the concrete class would silently pick its formatting back up if
    docling changed its base implementation. (The base class is also required —
    `MarkdownDocSerializer` is a pydantic model that validates the type.)
    """

    def serialize(self, *, item, doc_serializer=None, doc=None, **kwargs):
        try:
            html = item.export_to_html(doc=doc)
        except TypeError:          # older docling: no doc kwarg
            html = item.export_to_html()
        return SerializationResult(text="\n" + (html or "") + "\n")


HtmlTableSerializer = _Pdf4llmHtmlTableSerializer or _LocalHtmlTableSerializer


#: Publisher chrome that docling carries into the markdown. These are never
#: article content, and they are not cheap: measured over 45 converted papers,
#: author-search links were 9.0% of all body characters and Cloudflare's
#: obfuscated-email stubs another 6.3% -- tokens paid for on every extraction.
#:
#: Deliberately NOT "any line that is only a link": that rule would strip 12.2%
#: but cannot tell a navigation menu from a reference or a data-availability
#: link, and losing a real one is far worse than carrying some noise.
# NOTE: the link TEXT can itself contain brackets -- Cloudflare renders the
# stub as "[[email protected]](...)" -- so the text class stops at "(" rather
# than at "]", or the pattern fails to match exactly the case it exists for.
_NAV_EMAIL_RE = re.compile(
    r'\[[^(]*\]\([^)]*cdn-cgi/l/email-protection[^)]*\)', re.I)
_NAV_AUTHOR_SEARCH_RE = re.compile(
    r'\[([^(]*)\]\([^)]*search-results\?[^)]*\)', re.I)
_NAV_ONLY_LINE_RE = re.compile(
    r'^\s*\[[^\]]*\]\([^)]*/(?:login|register|subscribe|alerts?|rss|'
    r'issue/archive|lostPassword)\b[^)]*\)\s*$', re.I | re.M)


def strip_navigation(md: str) -> str:
    """Drop publisher chrome from converted markdown, keeping the prose.

    Author-search links keep their TEXT (the author's name is real content) and
    lose only the URL. Obfuscated-email stubs go entirely -- the visible text is
    "[email protected]" and the target is a hex blob, so nothing is lost.
    """
    if not md:
        return md
    md = _NAV_EMAIL_RE.sub("", md)
    md = _NAV_AUTHOR_SEARCH_RE.sub(r"\1", md)      # keep the name, drop the URL
    md = _NAV_ONLY_LINE_RE.sub("", md)
    return md


def html_to_markdown(html_path: Path) -> str | None:
    """Markdown for *html_path*, tables embedded as HTML. None if unusable.

    Returns None for anything under `_MIN_CHARS` of text — a publisher that
    served a consent wall or a "your institution does not have access" page
    produces a perfectly valid, perfectly useless document, and writing that as
    `body.md` would mark the paper as having full text.
    """
    from docling.document_converter import DocumentConverter
    from docling_core.transforms.serializer.markdown import (
        MarkdownDocSerializer, MarkdownParams)

    html_path = Path(html_path)
    if not html_path.exists():
        return None

    result = DocumentConverter().convert(html_path)
    doc = result.document

    serializer = MarkdownDocSerializer(
        doc=doc,
        table_serializer=HtmlTableSerializer(),
        # escape_html=False so the table markup we just inserted is emitted
        # literally instead of being entity-escaped into visible &lt;table&gt;.
        params=MarkdownParams(escape_html=False),
    )
    text = strip_navigation(serializer.serialize().text or "")
    return text if len(text.strip()) >= _MIN_CHARS else None


def count_tables(text: str) -> int:
    """Tables in a converted body, counting BOTH representations.

    The pipe-row heuristic used elsewhere reports zero for an HTML-table body,
    which would flag every HTML conversion as a degraded, table-free one. This
    is the shared definition so the convert-time report and the evidence row
    cannot drift.
    """
    if not text:
        return 0
    html_tables = text.lower().count("<table")
    pipe_rows = sum(1 for line in text.splitlines() if line.lstrip().startswith("|"))
    # A markdown table needs a header, a delimiter and a body row to be real;
    # the >3 threshold matches evidence.py's existing rule.
    return html_tables + (1 if pipe_rows > 3 else 0)


def find_html_only_papers(index: dict[str, Path]) -> list[tuple[str, Path, Path]]:
    """(doi, paper_dir, html_path) for papers with HTML and no better text.

    "Better" means `body.md`, publisher XML, or a PDF:

    * `body.md` — already converted, nothing to do.
    * XML — publisher JATS is richer than scraped HTML, and `xml_body_text`
      already passes its tables through as markup.
    * PDF — deliberately excluded, even though it is not obviously better than
      publisher HTML. Writing `body.md` here would make `convert_pdfs` skip
      that paper's PDF entirely (its work list is "has a PDF and no body.md"),
      silently preempting the merged GROBID+docling conversion. This function
      is for papers with NO other route to full text.
    """
    out: list[tuple[str, Path, Path]] = []
    for doi, folder in index.items():
        try:
            names = [n for n in folder.iterdir() if n.is_file()]
        except OSError:
            continue
        if any(n.name == "body.md" for n in names):
            continue
        if any(n.name.endswith((".xml", ".pdf")) and not n.name.endswith(".dup")
               for n in names):
            continue
        html = [n for n in names
                if n.name.endswith(".html") and not n.name.endswith(".dup")
                and not n.name.endswith(".landing.html")]
        if not html:
            continue
        # Prefer an explicit .fulltext.html over any other HTML artifact.
        html.sort(key=lambda p: (0 if p.name.endswith(".fulltext.html") else 1,
                                 -p.stat().st_size))
        out.append((doi, folder, html[0]))
    return out
