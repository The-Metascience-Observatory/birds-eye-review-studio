"""Shared utilities for the literature review pipeline."""

from __future__ import annotations

import csv
import html
import json
import threading
import os
import re
import sys
import time
from pathlib import Path

# ANSI colours
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

# --- Secret redaction ---
# API keys travel as query parameters for the APIs that only accept them that
# way (OpenAlex `api_key`, NCBI E-utilities `api_key`), and `requests` puts the
# full URL in its exception text — so a logged "Request error: {e}" printed the
# key into job logs the studio shows its users. Redact at every log sink.

#: Environment variables whose VALUES must never appear in a log line.
SECRET_ENV_VARS = (
    "OPENALEXAPIKEY", "ENTREZ_EUTILS_API_KEY", "NCBI_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY", "SCOPUS_API_KEY", "COREAPIKEY",
    "DIMENSIONS_API_KEY", "CROSSREF_API_KEY", "OPENROUTER_API_KEY",
    "PUBPEER_DEVKEY", "ELSEVIER_TDM_API_KEY", "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
)

_SECRET_PARAM_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|apikey|access[_-]?token|token|devkey|insttoken|key)=)"
    r"[^&\s'\"<>]+")
_SECRET_HEADER_RE = re.compile(
    r"(?i)((?:authorization|x-api-key|x-els-apikey|crossref-plus-api-token)['\"]?\s*[:=]\s*['\"]?)"
    r"(?:bearer\s+)?[^\s'\",}]+")


def redact_secrets(text) -> str:
    """*text* with API keys masked: ``key=`` style URL parameters, auth
    headers, and the value of any :data:`SECRET_ENV_VARS` set in this process."""
    out = _SECRET_PARAM_RE.sub(r"\1REDACTED", str(text))
    out = _SECRET_HEADER_RE.sub(r"\1REDACTED", out)
    for var in SECRET_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        if len(value) >= 8:
            out = out.replace(value, "REDACTED")
    return out


class SecretRedactingFilter:
    """``logging`` filter applying :func:`redact_secrets` to every record."""

    def filter(self, record) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging itself fail
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


# --- DOI helpers ---
# Three distinct concepts, do not conflate them:
#   doi_to_filename  : canonical DOI -> filesystem-safe stem ("/" -> "--"). No
#                      lowercasing (filenames preserve the DOI's original case).
#   filename_to_doi  : the inverse — strips known file suffixes, then "--" -> "/".
#   norm_doi         : a case-insensitive JOIN KEY (lowercased, stripped, "--" ->
#                      "/"). Use this, NOT filename_to_doi, when matching DOIs
#                      across files whose casing may differ.
def doi_to_filename(doi: str) -> str:
    """Convert DOI to filesystem-safe name: 10.1001/jama → 10.1001--jama"""
    return doi.replace("/", "--")

def filename_to_doi(filename: str) -> str:
    """Convert a filename (or stem) back to its DOI, case preserved.

    Decodes BOTH stem encoders — this repo's ``--`` for "/" and fetchpdf's
    ``~XX~`` / ``~`` escapes — so ``10.1023--a~1022`` is ``10.1023/a:1022``,
    not the ``10.1023/a~1022`` a bare ``--`` replacement produced.
    """
    from ber_core.identity import stem_to_doi
    stem = filename
    for suffix in ("_abstract.md", ".md", ".xml", ".pdf"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    return stem_to_doi(stem)

#: DOI prefixes that register DATASETS and figures, never journal articles.
#: A search that returns these is not wrong -- the deposits really do mention
#: the topic -- but they have no full text to fetch, screen or extract, so they
#: burn a download slot and land in the evidence table with a blank title.
DATA_REPOSITORY_PREFIXES = (
    "10.5281/",    # Zenodo
    "10.6084/",    # figshare
    "10.5283/",    # OPARU / university repositories
    "10.22032/",   # Thuringian university repository
)

#: Publishers that mint supplementary material as its own COMPONENT DOI,
#: formed by appending ``.sNNN`` to the article's DOI. Crossref types these
#: "component", not "journal-article".
#:
#: An explicit allow-list, because the suffix alone means different things at
#: different publishers and guessing is destructive in both directions:
#:   * Dove Press (10.2147) uses ``.sNNNNN`` in its ORDINARY article DOIs --
#:     10.2147/amep.s342386 is a real paper.
#:   * Several smaller journals (10.5546, 10.5747, 10.1590, 10.48087, 10.29271,
#:     10.6938, 10.18006) use ``.sNNN`` as a supplement-ISSUE page number, again
#:     on real articles: 10.5546/aap.2021.s198 is Crossref "journal-article".
#: A 40-DOI Crossref audit of a pattern-only rule mislabelled 4 real articles as
#: supplements. Unknown publishers are therefore treated as articles: a wasted
#: fetch costs seconds, a dropped paper corrupts the review.
SUPPLEMENT_COMPONENT_PREFIXES = (
    "10.1371/",    # PLOS -- journal.pone.0330793.s008
    "10.1021/",    # ACS  -- acsomega.5c04858.s003
    "10.3389/",    # Frontiers -- fams.2026.1841713.s001
)

_SUPPLEMENT_COMPONENT_RE = re.compile(
    r"^(?P<parent>10\.\d{4,9}/.+?)\.s\d{3,}$", re.IGNORECASE)


def is_supplement_doi(doi: str) -> bool:
    """True for a supplementary-material component DOI.

    Requires BOTH a known supplement-minting publisher
    (:data:`SUPPLEMENT_COMPONENT_PREFIXES`) and a parent DOI that still carries
    an article number. Verified against Crossref's ``type`` field; see the note
    on the prefix list for why a pattern-only test is unsafe.
    """
    d = (doi or "").strip()
    if not d.lower().startswith(SUPPLEMENT_COMPONENT_PREFIXES):
        return False
    m = _SUPPLEMENT_COMPONENT_RE.match(d)
    if not m:
        return False
    suffix = m.group("parent").split("/", 1)[1]
    return any(ch.isdigit() for ch in suffix)


def is_fetchable_article(doi: str) -> bool:
    """False for DOIs that can never yield an article full text.

    Data-repository deposits and supplementary components. Use this to scope
    downloads and extraction; it is deliberately conservative -- when in doubt
    a DOI is treated as an article, because dropping a real paper is far worse
    than one wasted fetch.
    """
    d = (doi or "").strip().lower()
    if not d:
        return False
    if d.startswith(DATA_REPOSITORY_PREFIXES):
        return False
    return not is_supplement_doi(d)


from ber_core.evidence import norm_doi


from ber_core.evidence import norm_paper_id


def dashboard_layout_digest(dashboard_cfg: dict | None) -> str:
    """Stable fingerprint of a config's ``dashboard`` subtree.

    Stamped into ``dashboard.manifest.json`` by ``generate_dashboard.py`` and
    recomputed from ``config.yaml`` by the server's ``bundle_state``; a mismatch
    means the layout was edited after the manifest was written, so the (cheap)
    manifest step needs a re-run. Digests the WHOLE subtree, not just the panel
    list — title/noun/columns also land in the manifest.
    """
    import hashlib
    canon = json.dumps(dashboard_cfg or {}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


from ber_core.evidence import load_duplicate_dois


from ber_core.evidence import is_unpublishable_record


from ber_core.evidence import drop_duplicate_records


def find_duplicate_candidates(records: list) -> list:
    """Probable duplicate groups still present in ``records``, each as a list of
    paper_ids. Two shapes, both real in this corpus:

    - two records sharing a trial registration id (``metadata.nct_numbers``, or
      the legacy scalar ``nct_number``) — companion reports of one trial;
    - a bioRxiv/medRxiv preprint (``10.1101/``) alongside any non-preprint.

    Used to decide whether the funnel's de-duplication warning is TRUE for this
    data, rather than asserting it unconditionally. Returns [] when the records
    carry no metadata to judge by — silence beats a claim we cannot support.
    """
    groups: list = []

    by_nct: dict = {}
    for rec in records:
        md = rec.get("metadata")
        if not isinstance(md, dict):
            continue
        ncts = md.get("nct_numbers") or (
            [md["nct_number"]] if md.get("nct_number") else [])
        for nct in ncts:
            key = str(nct).strip().upper()
            if key:
                by_nct.setdefault(key, []).append(rec.get("paper_id"))
    for nct, pids in sorted(by_nct.items()):
        uniq = sorted({p for p in pids if p})
        if len(uniq) > 1:
            groups.append({"kind": "shared_trial_id", "key": nct, "paper_ids": uniq})

    preprints = sorted({r.get("paper_id") for r in records
                        if str(r.get("paper_id") or "").startswith("10.1101/")})
    if preprints and any(
            not str(r.get("paper_id") or "").startswith("10.1101/")
            for r in records):
        already = {p for g in groups for p in g["paper_ids"]}
        rest = [p for p in preprints if p not in already]
        if rest:
            groups.append({"kind": "preprint", "key": None, "paper_ids": rest})

    return groups


from ber_core.evidence import load_exclusion_dois


from ber_core.evidence import load_inclusion_dois

# --- Text reading with MD/XML/PDF fallback ---
_BASE64_PATTERN = re.compile(r"!\[.*?\]\(data:image/[^)]+\)", re.DOTALL)
_LONG_WORD = re.compile(r"\b\S{41,}\b")

def preprocess_md(text: str) -> str:
    """Clean MD: strip base64 images, decode HTML entities, normalize symbols."""
    text = _BASE64_PATTERN.sub("[image removed]", text)
    text = html.unescape(text)
    text = text.replace("§", "±")
    return text

#: Stripped before the run-on-word probe below: HTML markup and URLs are both
#: legitimately long, unspaced tokens that say nothing about text corruption.
_HTML_TAG_RE = re.compile(r"<[^>]{1,400}>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
#: A markdown link TARGET -- "](...)" -- absolute or relative. Publisher HTML
#: is dense with navigation ("](/jid/search-results?f_Authors=Jared+C+Durieux)"),
#: and a relative target has no scheme for _URL_RE to match on.
_MD_LINK_TARGET_RE = re.compile(r"\]\([^)]{0,400}\)")


def is_garbled(text: str) -> bool:
    """Detect garbled MD: ligature artifacts or excessive run-on words.

    HTML markup AND urls are stripped before the run-on-word test. A body
    converted from publisher HTML (see ``html_convert``) embeds tables as
    literal ``<table>`` markup and carries the site's navigation links, e.g.
    ``[Архивы](https://medbio.ejournal.by/jour/issue/archive)`` and relative ones
    like ``[Academic](/jid/search-results?f_Authors=Jared+C+Durieux)``. All are long
    unspaced tokens, and ``_LONG_WORD`` (41+ non-space chars) counts them as
    run-on words: 76 "long words" in one 16k body, against a threshold of 10.
    Every one of 45 HTML-converted papers was rejected this way -- discovered,
    bound to an md_path, then silently dropped by ``read_best_text``. The XML
    branch documents the same misfire on attribute values and ``<ext-link>``
    DOIs.

    Both strips are no-ops for PDF-derived markdown, which carries neither, so
    genuine ligature corruption and run-on text are still caught.
    """
    probe = text
    if "<" in probe:
        probe = _HTML_TAG_RE.sub(" ", probe)
    if "](" in probe:
        probe = _MD_LINK_TARGET_RE.sub("] ", probe)
    if "://" in probe or "www." in probe:
        probe = _URL_RE.sub(" ", probe)
    return text.count("/uniFB") > 5 or len(_LONG_WORD.findall(probe)) > 10


# Download-stamp / paywall / cover-page phrasing. Matched against the START of
# the text only — a real paper can legitimately cite "terms of use" mid-body.
_BOILERPLATE_MARKERS = (
    "downloaded from", "downloaded by", "date of download", "terms of use",
    "all rights reserved", "this content downloaded", "cover page",
    "dropbox", "institutional login", "access provided by",
)
# Any of these anywhere in the text means there IS article content.
_SUBSTANTIVE_MARKERS = (
    "method", "results", "patients", "participants", "study", "abstract",
    "conclusion", "background",
)


def looks_like_boilerplate(text: str) -> bool:
    """True when text is ONLY library/paywall boilerplate, no article content.

    The burn case (seen on a real run): a converted "paper" that is five
    repetitions of a library download stamp — > 200 chars, not garbled, so it
    sailed past every guard and cost full-price LLM calls that could only
    conclude "the paper content is empty".

    A known publisher permissions-guide heading is also rejected. That guide
    contains words such as "Abstract" describing reuse rights, not study data.
    Otherwise, two conservative conditions are both required:
    * >= 2 distinct boilerplate markers in the first ~1500 chars, AND
    * NO substantive marker (method/results/patients/…) anywhere in the text.
    A short letter or abstract has substantive words, so it passes.
    """
    head = text[:1500].lower()
    heading = re.sub(r"[#\s]+", " ", text[:350].lower()).strip()
    if (heading.startswith(("lippincott journal portfolio author permission guidelines",
                            "body lippincott journal portfolio author permission guidelines"))
            and "permissions@lww.com" in head):
        return True
    n_boiler = sum(1 for m in _BOILERPLATE_MARKERS if m in head)
    if n_boiler < 2:
        return False
    body = text.lower()
    return not any(m in body for m in _SUBSTANTIVE_MARKERS)

# --- Publisher XML ----------------------------------------------------------
# The reason the whole structured-text chain exists: publisher XML carries
# tables as REAL MARKUP, in either of two models --
#
#   XHTML  <td colspan="2">        the common one
#   CALS   <entry namest="c1" nameend="c3">   older, and still in wide use
#
# -- whereas the PDF -> markdown path renders them as pipe rows (spans lost) or,
# under a `grobid-only` degradation, drops them entirely. So this reader hands
# the model the MARKUP, not a flattening of it. The previous implementation did
# `" ".join(body.itertext())`, which whitespace-joined every cell into one
# run-on line: strictly worse than the pipe tables it was meant to improve on.

#: Elements that are pure overhead in an extraction prompt. <back>/<ref-list>
#: alone are typically 30-40% of a JATS record's bytes.
_XML_DROP_TAGS = frozenset({
    "back", "ref-list", "ref", "ack", "fn-group", "glossary", "app-group",
    "author-notes", "history", "permissions", "contrib-group", "aff",
    "kwd-group", "funding-group", "journal-meta", "processing-meta",
})
#: Table containers, kept whole under the budget below.
_XML_TABLE_TAGS = frozenset({"table-wrap", "table", "array"})
#: Elements that hold tables and figures OUTSIDE <body>. Both major flavors do
#: this -- JATS as <floats-group>, Elsevier as <floats> -- so a reader that only
#: walks <body> returns a document with no tables at all. Measured on a real
#: Elsevier corpus: every one of its tables lives here, none inside <body>.
_XML_FLOAT_TAGS = frozenset({"floats", "floats-group", "floats-wrap"})
#: Minimum real text content before XML counts as usable at all.
_XML_MIN_CHARS = 200
#: Below this, a JATS record is an abstract-only stub (~1-2k chars) and a full
#: `body.md` is the better source. An XML-only paper still uses its stub.
XML_MIN_BODY_CHARS = 3000
#: Share of the character budget reserved for tables before prose gets any.
_XML_TABLE_BUDGET_SHARE = 0.6


def _localname(tag) -> str:
    """'{ns}body' -> 'body'.

    Namespace-agnostic on purpose: JATS, Elsevier's ja/dtd, PMC efetch's
    <pmc-articleset> wrapper and un-namespaced publisher dumps all reach one
    code path. The old hardcoded `{http://www.elsevier.com/xml/ja/dtd}body`
    lookup returned None for every JATS document -- i.e. for nearly all of the
    XML we now fetch -- so the XML branch was dead for the common case.
    """
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def detect_xml_flavor(root) -> str:
    """'jats' | 'elsevier' | 'generic'."""
    for el in root.iter():
        if isinstance(el.tag, str) and "elsevier.com/xml/ja/dtd" in el.tag:
            return "elsevier"
    tags = {_localname(el.tag) for el in root.iter()}
    if {"article-meta", "journal-meta", "pmc-articleset"} & tags:
        return "jats"
    return "generic"


def _find_xml_body(root):
    """The <body> element in any flavor, or None."""
    if _localname(root.tag) == "body":
        return root
    for el in root.iter():
        if _localname(el.tag) == "body":
            return el
    return None


def _prune_xml(element) -> None:
    """Drop reference lists and front/back matter, in place."""
    for parent in element.iter():
        for child in list(parent):
            if _localname(child.tag) in _XML_DROP_TAGS:
                parent.remove(child)


def _collect_xml_floats(root, body) -> list:
    """Table elements that live OUTSIDE <body>, in a floats container.

    Without this the reader is a no-op on the publisher whose XML it most needs
    to read: Elsevier files put every table in `<floats>`, a SIBLING of `<body>`,
    so walking the body alone yields a document with no tables — the exact loss
    the whole structured-text chain exists to prevent. JATS does the same with
    `<floats-group>`.

    Only tables are lifted, not figures: a `<fig>` is a reference to an image the
    XML never contains, so carrying it costs tokens and adds nothing.
    """
    inside = {id(el) for el in body.iter()}
    found = []
    for container in root.iter():
        if _localname(container.tag) not in _XML_FLOAT_TAGS:
            continue
        for el in container:
            if id(el) in inside:
                continue
            if _localname(el.tag) in _XML_TABLE_TAGS or any(
                    _localname(sub.tag) in _XML_TABLE_TAGS for sub in el.iter()):
                found.append(el)
    return found


def _drop_xml_tables(element) -> None:
    """Remove table containers in place, keeping the prose around them.

    Used by the eligibility screen, which never needs a table: pass 1 runs on
    every screened-in paper, so carrying tables corpus-wide is pure cost.
    """
    for parent in element.iter():
        for child in list(parent):
            if _localname(child.tag) in _XML_TABLE_TAGS:
                parent.remove(child)


def xml_plain_text(xml_text: str) -> str:
    """Tag-stripped rendering of an XML string, for quote verification.

    A quote the model took from a rendered table is contiguous in the table but
    split by `</td><td>` in the raw markup, so checking it against the markup
    alone would score a legitimate quote as a fabrication.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # A budgeted excerpt is not always a single well-formed document.
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml_text)).strip()
    return re.sub(r"\s+", " ", " ".join(root.itertext())).strip()


def xml_body_text(xml_path: Path, max_chars: int | None = None,
                  include_tables: bool = True) -> str | None:
    """Serialized XML body for an extraction prompt, tables intact.

    Returns the pruned ``<body>`` subtree as XML text -- NOT flattened prose --
    so both table models survive verbatim. Returns None when the document has
    under ``_XML_MIN_CHARS`` of real text, or cannot be parsed.

    Budgeting when the result exceeds *max_chars*: tables are reserved FIRST (up
    to 60% of the budget), prose blocks fill the remainder in document order, and
    an explicit ``<!-- truncated -->`` marker says what was dropped. A plain
    ``text[:max_chars]`` slice can cut mid-``<table-wrap>``, handing the model
    half a table with no signal that it is half -- the worst possible failure for
    the one source we adopted BECAUSE of its tables.

    ``include_tables=False`` drops tables entirely (pass 1 screens eligibility
    and never needs one; paying for tables across the whole corpus is waste).
    """
    import xml.etree.ElementTree as ET

    if not xml_path.exists():
        return None
    try:
        root = ET.parse(xml_path).getroot()
        body = _find_xml_body(root)
        if body is None:
            # Some publisher dumps have no <body>; use the root and let the
            # pruning strip the front/back matter rather than yielding nothing.
            body = root
        _prune_xml(body)

        floats = [] if body is root else _collect_xml_floats(root, body)
        if not include_tables:
            _drop_xml_tables(body)
            floats = []

        blocks: list[tuple[bool, str]] = []   # (is_table, serialized)
        for child in list(body):
            is_table = _localname(child.tag) in _XML_TABLE_TAGS or any(
                _localname(el.tag) in _XML_TABLE_TAGS for el in child.iter())
            blocks.append((is_table, ET.tostring(child, encoding="unicode")))

        if not blocks:
            blocks = [(False, ET.tostring(body, encoding="unicode"))]

        # Floats last: they are referenced from the prose ("see Table 2"), so
        # document order is already broken for them, and appending keeps the
        # narrative contiguous.
        blocks += [(True, ET.tostring(el, encoding="unicode")) for el in floats]

        text = _budget_xml_blocks(blocks, max_chars)
        # The floor is measured on real text content, not on serialized length:
        # a document that is all tags and no words would otherwise pass.
        if len(re.sub(r"\s+", " ", " ".join(body.itertext())).strip()) >= _XML_MIN_CHARS:
            return text
    except Exception as e:  # noqa: BLE001 — malformed XML must fall through
        print(f"  {YELLOW}WARN: XML parse failed for {xml_path.name}: {e}{RESET}",
              file=sys.stderr, flush=True)
    return None


def _budget_xml_blocks(blocks: list[tuple[bool, str]],
                       max_chars: int | None) -> str:
    """Fit serialized blocks into *max_chars*, tables first, never cut in half."""
    joined = "\n".join(b for _, b in blocks)
    if max_chars is None or len(joined) <= max_chars:
        return joined

    tables = [(i, s) for i, (is_table, s) in enumerate(blocks) if is_table]
    prose = [(i, s) for i, (is_table, s) in enumerate(blocks) if not is_table]

    kept: dict[int, str] = {}
    table_budget = int(max_chars * _XML_TABLE_BUDGET_SHARE)
    used = 0
    dropped_tables = 0
    for i, s in tables:
        if used + len(s) > table_budget:
            dropped_tables += 1
            continue
        kept[i] = s
        used += len(s)

    dropped_prose = 0
    for i, s in prose:
        if used + len(s) > max_chars:
            dropped_prose += 1
            continue
        kept[i] = s
        used += len(s)

    out = [kept[i] for i in sorted(kept)]
    if dropped_tables or dropped_prose:
        out.append(
            f"<!-- truncated to fit the context budget: "
            f"{dropped_prose} prose block(s) and {dropped_tables} table(s) "
            f"omitted of {len(prose)} and {len(tables)} -->")
    return "\n".join(out)


#: The markdown-only note the extraction templates carried before the text
#: source became a variable. Left verbatim in per-project template overrides.
_LEGACY_FORMAT_NOTE = ("NOTE: In this text, § means ±, &lt; means <, &gt; means >. "
                       "Some tables use | pipe separators.")


def ensure_format_note_placeholder(raw: str) -> str:
    """Give a prompt template a ``{format_note}`` slot if it lacks one.

    Per-project overrides (``prompts/<slug>/``, e.g.
    ``prompts/antiviral_nasal_sprays/``) were forked before the placeholder
    existed and still hardcode the markdown-only note — which is actively
    wrong in front of raw XML. Swapping it at load time beats editing files a
    user may have hand-tuned, and a template carrying neither the placeholder
    nor the legacy line is returned untouched (it simply gets no note).

    Callers must pass ``format_note`` to ``.format()`` unconditionally: an
    unused kwarg is ignored, but a template containing a placeholder the caller
    omits raises KeyError.
    """
    if "{format_note}" in raw:
        return raw
    return raw.replace(_LEGACY_FORMAT_NOTE, "{format_note}", 1)


def parse_xml_body(xml_path: Path, max_chars: int | None = None,
                   include_tables: bool = True) -> str | None:
    """Back-compatible alias for :func:`xml_body_text`.

    Elsevier ja/dtd documents keep working: the namespace-agnostic <body> lookup
    is a strict superset of the old hardcoded one.
    """
    return xml_body_text(xml_path, max_chars=max_chars,
                         include_tables=include_tables)

_FITZ_WARNED = threading.Event()


def _warn_no_fitz(what: str) -> None:
    """One loud warning per process: PDF-only papers are being skipped."""
    if _FITZ_WARNED.is_set():
        return
    _FITZ_WARNED.set()
    bar = "=" * 68
    print(f"\n  {RED}{bar}\n"
          f"  PyMuPDF (`fitz`) is not installed, so {what} cannot be read.\n"
          f"  Papers whose only full text is a PDF get NO text (recorded as\n"
          f"  no_text and retried once it is installed). Fix: pip install PyMuPDF\n"
          f"  {bar}{RESET}", file=sys.stderr, flush=True)


def read_pdf_text(pdf_path: Path) -> str | None:
    """Extract text from PDF via pymupdf. Returns plain text or None."""
    if not pdf_path.exists():
        return None
    try:
        import fitz
    except ImportError:
        _warn_no_fitz(pdf_path.name)
        return None
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        text = text.strip().replace("\x00", "")
        if len(text) >= 200:
            return text
    except Exception as e:
        print(f"  {YELLOW}WARN: PDF read failed for {pdf_path.name}: {e}{RESET}",
              file=sys.stderr, flush=True)
    return None

# --- Resumability helpers ---
def load_done_dois_csv(output_path: Path, id_column: str = "doi",
                       skip_values: set[str] | None = None,
                       status_column: "str | tuple[str, ...] | None" = None) -> set[str]:
    """Load already-processed IDs from a CSV for resumability.

    *status_column* names the column holding the row's verdict — or a tuple of
    candidate names, the first one present in the file's header winning (for
    files still carrying a legacy header such as ``is_long_covid``). Rows whose
    verdict is in *skip_values* are NOT done, so a failed/undecided row is
    retried on the next run.

    Without a status column every row with an id counts as done. There used to
    be a fallback that GUESSED at two hardcoded columns (``is_long_covid`` /
    ``article_type``); no current file matched it, so ``unknown`` rows counted
    as done and were never re-screened. Name the column.
    """
    if skip_values is None:
        skip_values = {"parse_error", "unknown", ""}
    done = set()
    if not output_path.exists():
        return done
    with open(output_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        column = None
        if status_column:
            candidates = ((status_column,) if isinstance(status_column, str)
                          else tuple(status_column))
            column = next((c for c in candidates if c in header), candidates[0])
        for row in reader:
            val = row.get(id_column, "")
            status = (row.get(column) or "") if column else "ok"
            if val and status not in skip_values:
                done.add(val)
    return done

def read_jsonl(path: Path, on_error: str = "raise") -> list[dict]:
    """Every JSON object in a JSONL file, in order. A missing file is ``[]``.

    *on_error* decides what a malformed line (bad JSON, or JSON that is not an
    object) does: ``"raise"`` (ValueError naming the line), ``"warn"`` (one
    stderr line per bad line, then skip) or ``"skip"`` (silently). A torn last
    line — an append interrupted mid-write — is the common case for warn/skip.
    """
    if on_error not in ("raise", "warn", "skip"):
        raise ValueError(f"on_error must be raise|warn|skip, not {on_error!r}")
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    raise ValueError(f"expected a JSON object, got {type(record).__name__}")
            except ValueError as exc:
                if on_error == "raise":
                    raise ValueError(f"{path}:{lineno}: {exc}") from exc
                if on_error == "warn":
                    print(f"  {YELLOW}WARN: {path.name}:{lineno}: skipping malformed "
                          f"line ({exc}){RESET}", file=sys.stderr, flush=True)
                continue
            out.append(record)
    return out


def drop_jsonl_records(path: Path, predicate) -> int:
    """Rewrite *path* without the records *predicate* matches (atomic).

    Malformed lines are kept verbatim — not ours to judge. Returns the number
    of records dropped.
    """
    if not path.exists():
        return 0
    kept: list[str] = []
    dropped = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                kept.append(stripped)
                continue
            if isinstance(record, dict) and predicate(record):
                dropped += 1
                continue
            kept.append(stripped)
    if dropped:
        from ber_core.storage import atomic_text
        atomic_text(path, "\n".join(kept) + ("\n" if kept else ""))
    return dropped


def requeue_no_text(path: Path, paper_ids) -> int:
    """Make ``no_text`` verdicts for *paper_ids* retryable.

    ``no_text`` means "nothing readable on disk YET": body.md may be converted
    later, PyMuPDF installed, publisher XML fetched. Treating it as terminal
    stranded those papers forever. Re-reading costs no LLM call when there is
    still no text, so the stale records are simply dropped and the papers run
    again (writing a fresh verdict either way).
    """
    wanted = set(paper_ids)
    if not wanted:
        return 0
    return drop_jsonl_records(
        path, lambda r: r.get("_status") == "no_text" and r.get("paper_id") in wanted)


def load_done_ids_jsonl(output_path: Path, id_field: str = "paper_id",
                        skip_statuses: set[str] | None = None) -> set[str]:
    """Load already-processed IDs from a JSONL for resumability.

    A malformed line (a torn append) is reported and skipped — its paper then
    simply runs again.
    """
    if skip_statuses is None:
        skip_statuses = {"parse_error", "error"}
    done = set()
    for obj in read_jsonl(output_path, on_error="warn"):
        pid = obj.get(id_field, "")
        status = obj.get("_status", "ok")
        if pid and status not in skip_statuses:
            done.add(pid)
    return done

# --- Progress logging ---
def log_progress(done: int, total: int, cost: float = 0.0,
                 start_time: float = 0.0, extra: str = "") -> None:
    """Print a progress line with ETA to stderr.

    ETA is seconds-aware ("45s" / "12m30s" / "1h02m") and prints "?" until the
    first completion gives a rate — the old XhYYm-only format rendered every
    sub-minute ETA (and the no-data-yet state) as a misleading "0h00m".
    """
    wall = time.monotonic() - start_time if start_time else 0
    rate = done / wall if wall > 0 else 0
    remaining = total - done
    eta_s = remaining / rate if rate > 0 else None
    if eta_s is None:
        eta_str = "?"
    elif eta_s < 60:
        eta_str = f"{max(1, int(eta_s))}s"
    elif eta_s < 3600:
        eta_str = f"{int(eta_s // 60)}m{int(eta_s % 60):02d}s"
    else:
        eta_str = f"{int(eta_s // 3600)}h{int((eta_s % 3600) // 60):02d}m"
    parts = [f"Progress: {done}/{total}"]
    if cost > 0:
        parts.append(f"cost: ${cost:.3f}")
    parts.append(f"ETA: {eta_str}")
    if extra:
        parts.append(extra)
    print(f"  ── {' | '.join(parts)} ──", file=sys.stderr, flush=True)

def log_status(seq: int, total: int, status: str, detail: str = "") -> None:
    """Print a formatted status log line to stderr."""
    tag = f"  #{seq:03d}/{total:<3d}"
    detail_str = f"  {detail}" if detail else ""
    if status in ("FAIL", "TIMEOUT"):
        color, reset = RED, RESET
    elif status in ("WARN", "SKIP", "RETRY"):
        color, reset = YELLOW, RESET
    else:
        color, reset = "", ""
    print(f"{tag}  {color}{status:<7s}{detail_str}{reset}", file=sys.stderr, flush=True)

# --- Thread-safe CSV/JSONL writing ---
import threading

def write_csv_atomic(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Write rows to CSV atomically: a unique temp file, fsynced, then renamed.

    The temp name is unique per call (two writers no longer share one
    ``.csv.tmp``) and the replaced file's permissions are kept.
    """
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --- Config loading ---
def load_config(config_path: str | Path | None = None) -> dict:
    """Load a review's YAML config. There is no default config.

    A missing config is a fatal error, not a warning: running on ``{}`` means
    every vocabulary empty and every prompt generic, which looks like a
    successful run until the output is inspected (the silent-garbage failure
    mode the config schema exists to prevent). A repo-root ``config.yaml`` used
    to be the implicit default for every script; reviews now live in
    ``configs/`` and new ones start from ``configs/example.yaml``.
    """
    if config_path is None or not str(config_path).strip():
        print(f"\n{RED}ERROR: no review config given.{RESET} Pass --config "
              f"configs/config_<topic>.yaml\n       (start a new review from "
              f"configs/example.yaml).\n", file=sys.stderr)
        sys.exit(1)
    path = Path(config_path)
    if not path.exists():
        hint = ""
        if path.name == "config.yaml" and not path.parent.parts:
            hint = ("       There is no default config any more: pass --config\n"
                    "       configs/config_<topic>.yaml, or copy configs/example.yaml.\n")
        print(f"\n{RED}ERROR: config file not found: {path}{RESET}\n"
              f"       Nothing would be configured — every vocabulary empty and\n"
              f"       every prompt generic — so this exits instead of running.\n"
              f"{hint}", file=sys.stderr)
        sys.exit(1)
    try:
        import yaml
    except ImportError:
        print(f"\n{RED}ERROR: PyYAML is not installed, so {path} cannot be read."
              f"{RESET}\n       Install it (`pip install pyyaml`) — silently using an\n"
              f"       empty config would run the pipeline on defaults.\n",
              file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# --- Machine-readable run summaries ------------------------------------------
# Every LLM step already maintains a counter dict (done/total/cost/tokens/...)
# under its write lock and then throws it away into an f-string. The server was
# left recovering cost by regexing `cost: $X.YZ` out of job logs and taking the
# LAST match — which silently reports a mid-run cumulative if the run was killed,
# or only the final step's cost when several steps append to one log. Emitting
# the counter as JSON replaces that guesswork with the real number.

RUN_SUMMARY_PREFIX = "RUN_SUMMARY "


def emit_run_summary(step: str, counter: dict, *, outputs=(), model: str = "",
                     extra: dict | None = None, data_dir: "Path | None" = None) -> dict:
    """Emit a step's final counters as JSON, for the server to read.

    Writes one ``RUN_SUMMARY {json}`` line to stderr (the job log the runner
    already tees) and, when ``data_dir`` is given, a durable copy under
    ``<data_dir>/_runs/``. Returns the payload so callers can log it too.

    ``counter`` is passed through as-is apart from ``start``, which is converted
    into ``elapsed_s``. Scripts therefore need no new bookkeeping — they already
    track everything this reports.
    """
    payload = {k: v for k, v in (counter or {}).items() if k != "start"}
    start = (counter or {}).get("start")
    if start:
        payload["elapsed_s"] = round(time.monotonic() - start, 1)
    payload["step"] = step
    if model:
        payload["model"] = model
    if outputs:
        payload["outputs"] = [str(o) for o in outputs]
    if extra:
        payload.update(extra)

    # Single line, no newlines inside, so a line-oriented log reader can find it.
    print(RUN_SUMMARY_PREFIX + json.dumps(payload, ensure_ascii=False, default=str),
          file=sys.stderr, flush=True)

    if data_dir:
        try:
            runs = Path(data_dir) / "_runs"
            runs.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%S")
            (runs / f"{step}_{stamp}.summary.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass  # a summary is diagnostics; never fail a completed run over it
    return payload


def parse_run_summaries(log_text: str) -> list[dict]:
    """Every ``RUN_SUMMARY`` payload in a job log, oldest first.

    A pipeline job appends all its steps to one log, so this returns one entry
    per step rather than a single ambiguous total.
    """
    out: list[dict] = []
    for line in (log_text or "").splitlines():
        idx = line.find(RUN_SUMMARY_PREFIX)
        if idx == -1:
            continue
        try:
            out.append(json.loads(line[idx + len(RUN_SUMMARY_PREFIX):]))
        except ValueError:
            continue
    return out


def load_prompt_template(template_name: str, prompts_dir: str | Path | None = None,
                         slug: str | None = None) -> str:
    """Load a prompt template, preferring a per-project (and per-review) override.

    Resolution order (first hit wins):

    1. ``$BER_PROMPTS_DIR/<slug>/<template_name>`` — per-review, per-project
    2. ``$BER_PROMPTS_DIR/<template_name>``        — per-review, shared
    3. ``<prompts_dir>/<slug>/<template_name>``    — per-project (e.g.
       antiviral_nasal_sprays customises a prompt without affecting others)
    4. ``<prompts_dir>/<template_name>``           — the shipped default

    ``BER_PROMPTS_DIR`` lets the studio backend point a single review at a
    workspace prompts dir holding user-edited screening criteria (an overridden
    ``<slug>/screen_trials.txt``) while every non-overridden template still falls
    back to the repo defaults. Unset/empty ``BER_PROMPTS_DIR`` preserves the
    historical behavior exactly.
    """
    search_dirs: list[Path] = []
    env_dir = os.environ.get("BER_PROMPTS_DIR")
    if env_dir:
        search_dirs.append(Path(env_dir))
    # Default to the repo's own prompts/ (not CWD-relative), so scripts that
    # live or run elsewhere still find the shipped templates.
    search_dirs.append(Path(prompts_dir) if prompts_dir is not None
                       else Path(__file__).resolve().parents[1] / "prompts")

    for base in search_dirs:
        if slug:
            override = base / slug / template_name
            if override.exists():
                return override.read_text(encoding="utf-8")
        shared = base / template_name
        if shared.exists():
            return shared.read_text(encoding="utf-8")

    # Not found anywhere — report the default location for a clear error.
    raise FileNotFoundError(
        f"Prompt template not found: {search_dirs[-1] / template_name}"
    )


# --- PDF / paper gathering helpers ---

def build_pdf_index(dirs: list[Path], recursive: bool = False) -> dict[str, Path]:
    """Build DOI-stem -> PDF path mapping for O(1) lookups.

    Scans *dirs* for ``*.pdf`` files whose stem starts with ``10.``.
    Uses ``rglob`` when *recursive* is True, ``glob`` otherwise.
    """
    index: dict[str, Path] = {}
    for d in dirs:
        pattern_fn = d.rglob if recursive else d.glob
        for p in pattern_fn("*.pdf"):
            if p.stem.startswith("10."):
                index[p.stem] = p
    return index


def gather_papers(
    dirs: list[Path],
    include_pdf_only: bool = False,
    recursive: bool = False,
) -> list[tuple[str, Path | None, str]]:
    """Gather papers from directories.

    Returns a sorted list of ``(doi, abstract_path | None, source_folder)``
    tuples.  When *include_pdf_only* is True, PDFs without a matching
    ``*_abstract.md`` file are also included (with ``abstract_path=None``).
    """
    # Search also saves CyberLeninka abstracts under stable source identifiers
    # when no DOI is available. They still need the same relevance screen.
    valid_prefixes = ("10.", "pmid_", "cyberleninka:")
    papers: list[tuple[str, Path | None, str]] = []
    seen: set[str] = set()
    for d in dirs:
        folder_name = d.name
        pattern_fn = d.rglob if recursive else d.glob
        for md in sorted(pattern_fn("*_abstract.md")):
            if not md.stem.startswith(valid_prefixes):
                continue
            doi = filename_to_doi(md.name)
            if doi not in seen:
                seen.add(doi)
                papers.append((doi, md, folder_name))
        if include_pdf_only:
            for pdf in sorted(pattern_fn("*.pdf")):
                if not pdf.stem.startswith(valid_prefixes):
                    continue
                doi = pdf.stem.replace("--", "/")
                if doi not in seen:
                    seen.add(doi)
                    papers.append((doi, None, folder_name))
    return papers


# --- Project paths & unified paper discovery ----------------------------

from dataclasses import dataclass

# Repo-root projects dir (ber_core/ sits directly under the repo root).
_PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (Path(__file__).resolve().parents[1] / "projects"))


from ber_core.identity import slugify


from ber_core.storage import paper_root


def project_paths(config: dict, projects_dir: Path | None = None) -> dict:
    """Return per-project paths derived from ``config['topic']['name']``.

    Single source of truth (superset of the per-script copies). Keys:
    ``topic_name, slug, project_dir, abstracts_dir, papers_dir, converted_dir,
    data_dir``.

    ``papers_dir`` is the current per-paper store (``papers/<Type>/<stem>/``);
    ``converted_dir`` is the legacy layout it replaced, kept so un-migrated
    projects still resolve. See ``paper_store.py``.
    """
    base = projects_dir or _PROJECTS_DIR
    topic_name = (config.get("topic", {}) or {}).get("name", "default")
    slug = slugify(topic_name)
    project_dir = base / slug
    return {
        "topic_name": topic_name,
        "slug": slug,
        "project_dir": project_dir,
        "abstracts_dir": project_dir / "abstracts",
        "papers_dir": paper_root(
            project_dir, (config.get("dirs") or {}).get("base_pdf_dir")),
        "converted_dir": project_dir / "converted",
        "data_dir": project_dir / "data",
    }


# Every non-"ok" ``_status`` the extraction passes can write. "refused" (prose
# refusal, no retries), "ineligible" (schema-compliant all-null extraction) and
# "low_quality_text" (boilerplate-only converted text) are terminal verdicts:
# resumability treats them as done, and no consumer may pool them as data.
from ber_core.constants import FAILED_STATUS as FAILED_EXTRACTION_STATUSES  # noqa: E402  (single definition)


def managed_run() -> bool:
    """True inside a studio-managed run (the server sets ``BER_MANAGED_RUN=1``)."""
    return os.environ.get("BER_MANAGED_RUN") == "1"


def configured_dir(config: dict, key: str) -> str | None:
    """``dirs.<key>`` from *config*, or None when unset — or when MANAGED.

    A studio user edits their review's config, so a ``dirs.*`` path there is a
    user-supplied filesystem path. Reading one as an INPUT would let a review
    point extraction or screening at another user's papers, so managed runs
    ignore them and use the review's own workspace.
    """
    if managed_run():
        return None
    value = (config.get("dirs") or {}).get(key)
    if value is None or isinstance(value, (list, tuple)):
        return value or None
    return str(value) if str(value).strip() else None


def resolve_input_dir(
    args_input: str | None,
    configured: str | None,
    project_converted_dir: Path,
) -> Path:
    """Resolve an input directory, project-aware, never collapsing to ``.``.

    Priority: ``args_input`` -> ``configured`` (only if non-blank) ->
    ``project_converted_dir``. Empty/whitespace strings and None are treated
    as unset, so an empty config value can never become ``Path(".")`` and
    recursively glob the whole repo.
    """
    if args_input and str(args_input).strip():
        return Path(args_input)
    if configured and str(configured).strip():
        return Path(configured)
    return project_converted_dir


@dataclass(frozen=True)
class PaperRef:
    """A single paper located on disk, layout-agnostic.

    ``paper_id`` is the DOI form ("/" restored); ``stem`` is the filesystem
    form ("/"->"--"). ``layout`` is "nested" (``<dir>/<stem>/`` with body.md +
    ``<stem>.pdf``), "typed" (``<dir>/<Type>/<stem>/``, the paper store) or
    "flat" (``<dir>/<stem>.md|.xml|.pdf`` side-by-side).
    """
    paper_id: str
    stem: str
    layout: str
    md_path: Path | None
    xml_path: Path | None
    pdf_path: Path | None
    dir: Path


def _stem_paper_id(stem: str) -> str:
    """A folder/file stem -> ``PaperRef.paper_id`` (the DOI, case preserved).

    Through :func:`ber_core.identity.stem_to_doi`, so a fetchpdf-named folder
    (``10.1023--a~1022``) yields ``10.1023/a:1022`` rather than keeping the
    ``~`` escape, which joined to nothing.
    """
    from ber_core.identity import stem_to_doi
    return stem_to_doi(stem)


def _valid_stem(stem: str, valid_prefixes: tuple[str, ...]) -> bool:
    return stem.startswith(valid_prefixes)


def _paper_folder_ref(child: Path, layout: str) -> PaperRef | None:
    """Build a ``PaperRef`` for one per-paper folder, or None if it holds none
    of md/xml/pdf. Shared by the nested and typed passes."""
    stem = child.name
    body = child / "body.md"
    md_path = body if body.exists() else None
    xml = child / f"{stem}.xml"
    xml_path = xml if xml.exists() else None
    pdf = child / f"{stem}.pdf"
    if pdf.exists():
        pdf_path: Path | None = pdf
    else:
        # safety net: any single PDF in the dir
        pdfs = list(child.glob("*.pdf"))
        pdf_path = pdfs[0] if len(pdfs) == 1 else None
    if not (md_path or xml_path or pdf_path):
        return None
    return PaperRef(
        paper_id=_stem_paper_id(stem),
        stem=stem,
        layout=layout,
        md_path=md_path,
        xml_path=xml_path,
        pdf_path=pdf_path,
        dir=child,
    )


def discover_papers(
    input_dir: Path,
    valid_prefixes: tuple[str, ...] = ("10.", "pmid_"),
) -> list[PaperRef]:
    """Discover papers under *input_dir* in any of three layouts.

    NESTED: ``input_dir/<stem>/`` containing ``body.md`` and/or ``<stem>.pdf``
    (+ optional ``<stem>.xml``). Detected when a child directory's name is a
    valid stem.
    TYPED:  ``input_dir/<Type>/<stem>/`` — the paper store (see
    ``paper_store.py``). Detected when a child directory's name is NOT a valid
    stem but it contains directories that are. Recursed exactly one level, so a
    stray directory elsewhere can never turn this into a whole-tree walk.
    FLAT:   ``input_dir/<stem>.md|.xml|.pdf`` side-by-side (``*_abstract.md``
    excluded).

    A paper qualifies if it has ANY of md/xml/pdf. Mixed dirs run every pass;
    on stem collisions nested wins over typed, and both win over flat. Returns
    one ``PaperRef`` per unique stem, sorted by stem.
    """
    if not input_dir.exists():
        return []

    refs: dict[str, PaperRef] = {}
    type_dirs: list[Path] = []

    # --- Nested pass: child dirs whose name is a valid stem ---
    for child in input_dir.iterdir():
        if not child.is_dir():
            continue
        if not _valid_stem(child.name, valid_prefixes):
            type_dirs.append(child)
            continue
        ref = _paper_folder_ref(child, "nested")
        if ref is not None:
            refs[child.name] = ref

    # --- Typed pass: <Type>/<stem>/ one level down (nested wins) ---
    # Real type buckets before the store's own ``_incoming``/``_unclassified``,
    # and on a collision between buckets the folder with text (body.md / XML)
    # beats a bare PDF — directory order used to decide.
    nested = set(refs)
    for bucket in sorted(type_dirs, key=lambda d: (d.name.startswith("_"), d.name)):
        for child in bucket.iterdir():
            if not child.is_dir() or not _valid_stem(child.name, valid_prefixes):
                continue
            if child.name in nested:
                continue
            ref = _paper_folder_ref(child, "typed")
            if ref is None:
                continue
            previous = refs.get(child.name)
            if previous is not None and (previous.md_path or previous.xml_path
                                         or not (ref.md_path or ref.xml_path)):
                continue
            refs[child.name] = ref

    # --- Flat pass: sibling files grouped by stem (folders win) ---
    flat_stems: set[str] = set()
    for f in input_dir.glob("*"):
        if not f.is_file():
            continue
        name = f.name
        if name.endswith("_abstract.md"):
            continue
        for suffix in (".md", ".xml", ".pdf"):
            if name.endswith(suffix):
                flat_stems.add(name[: -len(suffix)])
                break
    for stem in flat_stems:
        if stem in refs or not _valid_stem(stem, valid_prefixes):
            continue
        md = input_dir / f"{stem}.md"
        xml = input_dir / f"{stem}.xml"
        pdf = input_dir / f"{stem}.pdf"
        md_path = md if md.exists() else None
        xml_path = xml if xml.exists() else None
        pdf_path = pdf if pdf.exists() else None
        if md_path or xml_path or pdf_path:
            refs[stem] = PaperRef(
                paper_id=_stem_paper_id(stem),
                stem=stem,
                layout="flat",
                md_path=md_path,
                xml_path=xml_path,
                pdf_path=pdf_path,
                dir=input_dir,
            )

    return [refs[s] for s in sorted(refs)]


def read_best_text(
    ref: PaperRef,
    max_chars: int | None = 80000,
    xml_first: bool = True,
    reject_boilerplate: bool = False,
    xml_max_chars: int | None = None,
    xml_tables: bool = True,
) -> tuple[str | None, str]:
    """Best-available text for a *ref*. Default order: xml -> md -> pdf.

    ``max_chars=None`` returns the full selected text for callers that perform
    their own bounded, table-aware batching. Existing callers retain the cap.

    Uses the ref's explicit ``md_path``/``xml_path``/``pdf_path`` (correct per
    layout) -- never ``with_suffix``. Returns ``(text|None, source)`` where
    source is 'md', 'xml', 'pdf', 'boilerplate', or 'none'.

    ``reject_boilerplate`` (opt-in; the LLM extraction passes use it): a
    candidate that is pure library/paywall boilerplate is skipped like garbled
    text, falling through to the next source. If every candidate is
    boilerplate the result is ``(None, "boilerplate")`` — distinct from
    ``"none"`` so callers can record an auditable status instead of burning an
    LLM call on a download stamp.

    ``xml_max_chars`` gives the XML branch its own budget: serialized markup is
    tag-heavier than markdown per unit of content, so sharing one number would
    truncate XML far earlier. ``xml_tables=False`` drops tables (pass 1).
    """
    saw_boilerplate = False

    def _screen(text: str, source: str) -> tuple[str | None, str] | None:
        nonlocal saw_boilerplate
        if reject_boilerplate and looks_like_boilerplate(text):
            saw_boilerplate = True
            return None
        return (text if max_chars is None else text[:max_chars]), source

    def _try_md() -> tuple[str | None, str] | None:
        if not ref.md_path or not ref.md_path.exists():
            return None
        try:
            raw = ref.md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        text = preprocess_md(raw)
        if len(text.strip()) >= 200 and not is_garbled(text):
            return _screen(text, "md")
        return None

    def _try_xml() -> tuple[str | None, str] | None:
        if not ref.xml_path:
            return None
        text = xml_body_text(ref.xml_path,
                             max_chars=xml_max_chars or max_chars,
                             include_tables=xml_tables)
        if not text:
            return None
        plain = xml_plain_text(text)
        # An abstract-only JATS stub must not beat a full body.md. It still wins
        # when it is the ONLY source — a stub beats nothing.
        if len(plain) < XML_MIN_BODY_CHARS and ref.md_path and ref.md_path.exists():
            return None
        if reject_boilerplate and looks_like_boilerplate(plain):
            nonlocal saw_boilerplate
            saw_boilerplate = True
            return None
        # NOT _screen(): its blanket text[:max_chars] slice would undo the
        # table-aware budgeting and can cut a table in half. is_garbled is also
        # skipped deliberately — its run-on-word heuristic fires on attribute
        # values, DOIs in <ext-link> and base64 in <inline-graphic>, and XML has
        # no ligature-corruption failure mode to detect in the first place.
        return text, "xml"

    def _try_pdf() -> tuple[str | None, str] | None:
        if not ref.pdf_path:
            return None
        text = read_pdf_text(ref.pdf_path)
        return _screen(text, "pdf") if text else None

    order = (_try_xml, _try_md, _try_pdf) if xml_first else (_try_md, _try_xml, _try_pdf)
    for fn in order:
        result = fn()
        if result:
            return result
    return None, ("boilerplate" if saw_boilerplate else "none")


def dedup_csv(
    path: Path,
    id_column: str = "doi",
    preserve_extra_columns: bool = False,
) -> int:
    """Deduplicate a CSV in place, keeping the last value per *id_column*.

    When *preserve_extra_columns* is True, later rows MERGE over earlier ones
    field-by-field: a blank cell never overwrites a value already on record.

    That merge is the whole point of the flag. These CSVs are written by more
    than one step — the screen appends a row carrying only its own columns, and
    a later enrichment pass fills in ``paper_title`` / ``paper_year``. A plain
    last-row-wins dedup let a re-screen's blank-enrichment row silently erase
    the enrichment (verified: an enriched row became ``10.1/a,yes,,``). The flag
    existed to prevent exactly this and its body was empty, so it never did.

    With the flag False the historical last-row-wins behaviour is kept, since
    callers that pass nothing want a straight dedup.

    Returns the number of unique rows written.
    """
    if not path.exists():
        return 0
    seen: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        file_fieldnames = list(reader.fieldnames) if reader.fieldnames else []
        for row in reader:
            key = row.get(id_column, "")
            if not key:
                continue
            prior = seen.get(key)
            if prior is None or not preserve_extra_columns:
                seen[key] = row
            else:
                # Later row wins per FIELD, but only where it actually has a
                # value; blanks leave the earlier value intact.
                merged = dict(prior)
                for k, v in row.items():
                    if v not in (None, ""):
                        merged[k] = v
                seen[key] = merged
    if not seen:
        return 0
    # Atomic: this rewrites a step's whole output in place, and a crash (or a
    # concurrent reader) mid-write used to see a truncated file.
    write_csv_atomic(path, list(seen.values()), list(file_fieldnames))
    return len(seen)


# --- Cross-encoder filename decoding ------------------------------------------
# Two encoders write files into the same directories and they are NOT the same.
#
#   this repo   doi_to_filename          "/" -> "--"                (that's all)
#   fetchpdf    doi_to_safe_filename     "/" -> "--", ":" -> "~",
#                                        <>"\|?* -> "~XX~" (hex),
#                                        a literal "-" run -> "~2d~"
#
# They agree on ordinary DOIs and diverge on any DOI containing ":" or a literal
# "--" — so a `.xml` fetchpdf just wrote can sit directly beside a `.pdf` we
# named and never join to it. `norm_doi` doesn't reverse the extra escapes
# either, so normalizing a fetchpdf stem yields a WRONG key, not a colliding one
# (silent under-matching, the worst shape).
_ARTIFACT_ESCAPE_RE = re.compile(r"~([0-9a-fA-F]{2})~")

#: Suffixes fetchpdf and the converter write beside a paper's PDF. Longest-first:
#: ".fulltext.html" must be tried before ".html" would be, and the supplementary
#: manifest before the generic ".json".
ARTIFACT_SUFFIXES = (
    "_supplementary_info.json",
    "_linked_artifacts.json",
    ".provenance.json",
    ".fulltext.html",
    ".source.tar.gz",
    ".suppl.zip",
    "_from_xml.md",
    "_abstract.md",
    ".landing.html",
    ".xml",
    ".pdf",
    ".txt",
    ".md",
)


def strip_artifact_suffix(name: str) -> str:
    """Filename -> its stem, understanding fetchpdf's multi-part suffixes.

    ``Path.stem`` gets ``"10.1/x.fulltext.html"`` wrong (it yields
    ``"10.1--x.fulltext"``), which would key that record separately from its own
    PDF.
    """
    for suffix in ARTIFACT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def decode_artifact_stem(stem: str) -> str:
    """A filename stem -> normalized DOI, decoding EITHER encoder's escapes.

    Superset decoder: it reverses fetchpdf's ``~XX~``/``~2d~``/``~`` escapes and
    then defers to :func:`norm_doi` for the ``--`` -> ``/`` mapping, lowercasing
    and stripping. A stem this repo wrote contains no ``~`` at all and so passes
    through the first stage untouched — meaning one index can hold artifacts from
    both encoders and every lookup is a plain :func:`norm_doi` of the DOI.
    """
    # "~2d~" is a literal "-" (fetchpdf escapes it only where it would be
    # ambiguous with the "/" encoding), so it must decode BEFORE the bare
    # "~" -> ":" pass, which would otherwise eat its delimiters.
    from ber_core.identity import decode_stem_escapes
    return norm_doi(decode_stem_escapes(stem))


# Names other modules import FROM utils (the pipeline's single helper import
# site) without utils using them itself. Referenced here so a linter sees them
# as the deliberate re-exports they are.
_REEXPORTS = (
    norm_paper_id, load_duplicate_dois, is_unpublishable_record,
    drop_duplicate_records, load_exclusion_dois, load_inclusion_dois,
    FAILED_EXTRACTION_STATUSES,
)
