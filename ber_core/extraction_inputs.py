"""Bounded, source-labelled extraction inputs and conservative batch merging.

No conversion or model calls happen here. Native Office tables are read from
their ZIP/XML representation; PDF supplements require an existing conversion.
Tables are indivisible: a table larger than the budget is reported for review,
never sent as a plausible-looking partial table.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import html
import json
import math
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from urllib.parse import unquote, urlparse

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_BATCHES = 6
_TABLE = re.compile(r"</?(?:[\w-]+:)?(?:table-wrap|table|figure)(?=[\s>/])[^>]*>", re.I)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass
class InputBatch:
    batch_id: str
    source: str
    text: str
    source_kind: str = "md"


@dataclass
class ExtractionInputs:
    batches: list[InputBatch] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    transformations: list[dict] = field(default_factory=list)

    def manifest(self) -> dict:
        return {"version": 1, "status": "partial" if self.issues else "complete",
                "requires_review": bool(self.issues), "sources": self.sources,
                "issues": self.issues, "transformations": self.transformations,
                "batches": [{"batch_id": b.batch_id, "source": b.source,
                             "chars": len(b.text), "sha256": _sha(b.text.encode())}
                            for b in self.batches]}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _issue(source: str, code: str, **details) -> dict:
    return {"source": source, "code": code, **details}


def _prose_blocks(text: str):
    """Keep contiguous pipe rows (including multi-level headers) together."""
    lines = text.splitlines(keepends=True)
    block, pipe = [], False
    for line in lines:
        is_pipe = "|" in line
        if block and (is_pipe != pipe or (not pipe and not line.strip())):
            yield pipe, "".join(block)
            block = []
        pipe = is_pipe
        block.append(line)
    if block:
        yield pipe, "".join(block)


def table_blocks(text: str):
    """Keep nested tables and their HTML figure/JATS caption-note containers intact."""
    start, depth, previous = None, 0, 0
    for match in _TABLE.finditer(text):
        closing = match.group().startswith("</")
        if not closing:
            if depth == 0:
                start = match.start()
                yield from _prose_blocks(text[previous:start])
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                yield True, text[start:match.end()]
                previous = match.end()
    if depth:
        # A malformed table is kept atomic and explicitly identified downstream.
        yield True, text[start:]
    else:
        yield from _prose_blocks(text[previous:])


_CAPTION = re.compile(r"^\s*(?:#{1,6}\s*)?(?:(?:supplementary|supporting)\s+)?(?:e?table)\s*[A-Z\dIVX.-]+\b", re.I)
_FOOTNOTE = re.compile(r"^\s*(?:(?:note[s]?|source[s]?|abbreviations)\s*[:.\-]|[*†‡§]|[a-z][.)]\s|(?:values|data)\s+(?:are|represent|shown)|(?:SD|SE|SEM|CI|IQR)\s*[:=])", re.I)


def contextual_blocks(text: str):
    """Keep adjacent table captions and footnotes in the table's atomic unit.

    Context never gets sliced to force a fit. If the resulting unit exceeds
    the caller's budget, chunk_text emits the ordinary oversized-table issue.
    """
    blocks = list(table_blocks(text))
    def plain(block):
        return html.unescape(re.sub(r"<[^>]*>", "", block))
    groups = {}
    for i, (table, _) in enumerate(blocks):
        if not table:
            continue
        start, end = i, i + 1
        previous = i - 1
        while previous >= 0 and not blocks[previous][1].strip():
            previous -= 1
        if previous >= 0 and not blocks[previous][0] and _CAPTION.match(plain(blocks[previous][1])):
            start = previous
        following = i + 1
        while following < len(blocks):
            candidate = following
            while candidate < len(blocks) and not blocks[candidate][1].strip():
                candidate += 1
            if candidate == len(blocks) or blocks[candidate][0] or not _FOOTNOTE.match(plain(blocks[candidate][1])):
                break
            end = candidate + 1
            following = end
        groups[start] = end
    i = 0
    while i < len(blocks):
        if i in groups:
            end = groups[i]
            yield True, "".join(block for _, block in blocks[i:end])
            i = end
        else:
            yield blocks[i]
            i += 1


def chunk_text(text: str, max_chars: int, source: str = "main") -> tuple[list[str], list[dict]]:
    if max_chars < 256:
        raise ValueError("extraction input budget must be at least 256 characters")
    chunks, issues, current = [], [], ""
    for index, (table, block) in enumerate(contextual_blocks(text)):
        if not block:
            continue
        if table and len(block) > max_chars:
            issues.append(_issue(source, "oversized_table", block=index,
                                 chars=len(block), sha256=_sha(block.encode())))
            continue
        if table and _TABLE.search(block) and any(
            len(re.findall(r"<(?:[\w-]+:)?" + tag + r"(?=[\s>/])", block, re.I)) !=
            len(re.findall(r"</(?:[\w-]+:)?" + tag + r"\s*>", block, re.I))
            for tag in ("table", "table-wrap", "figure")):
            issues.append(_issue(source, "malformed_table", block=index))
            continue
        # JATS can represent a whole table as an image, with no <table> at all.
        # A numeric result elsewhere in the article does not recover these cells.
        for match in re.finditer(r"<(?:[\w-]+:)?table-wrap(?=[\s>])[^>]*>(.*?)</(?:[\w-]+:)?table-wrap\s*>", block, re.I | re.S):
            if not re.search(r"<(?:[\w-]+:)?(?:table|array)(?=[\s>])", match.group(1), re.I):
                issues.append(_issue(source, "table_cells_unavailable", block=index,
                                     reason="table_wrapper_without_cells", sha256=_sha(match.group().encode())))
        # Some conversions retain a caption and an empty <table>, silently
        # losing every data cell. Keep the available text, but do not certify
        # complete source coverage merely because a table tag survived.
        for table_index, match in enumerate(re.finditer(
                r"<(?:[\w-]+:)?table(?=[\s>])[^>]*>(.*?)</(?:[\w-]+:)?table\s*>",
                block, re.I | re.S)):
            # Elsevier/CALS native XML uses entry cells rather than HTML td/th.
            # They carry the same complete table data and stay in the prompt.
            cells = re.findall(r"<(?:[\w-]+:)?(?:td|th|entry)(?=[\s>])[^>]*>(.*?)</(?:[\w-]+:)?(?:td|th|entry)\s*>",
                               match.group(1), re.I | re.S)
            if not any(html.unescape(re.sub(r"<[^>]*>", "", cell)).strip() for cell in cells):
                issues.append(_issue(source, "table_cells_unavailable", block=index,
                                     table=table_index, sha256=_sha(match.group().encode())))
            elif re.search(r"<(?:[\w-]+:)?(?:graphic|inline-graphic|img)(?=[\s>/])", match.group(1), re.I):
                issues.append(_issue(source, "table_image_requires_review", block=index,
                                     table=table_index, sha256=_sha(match.group().encode())))
        # Long prose may be split; tables never are. A sentence boundary is
        # preferable, but even a long unbroken paragraph must remain bounded.
        pieces = [block] if table else [block[i:i + max_chars] for i in range(0, len(block), max_chars)]
        for piece in pieces:
            if current and len(current) + len(piece) > max_chars:
                chunks.append(current)
                current = ""
            current += piece
    if current.strip():
        chunks.append(current)
    return chunks, issues


def compact_table_layout(text: str, max_chars: int, source: str) -> tuple[str, list[dict]]:
    """Remove redundant cell layout only from oversized, valid XML tables.

    Text, element order, headers, nontrivial spans, references, styles and
    number formats remain unchanged. An XML tree comparison verifies that
    only align/valign and explicit default spans were removed. The original
    source is never rewritten; input provenance records both block hashes.
    """
    cell_tag = re.compile(r'''<(?:[\w-]+:)?(?:td|th)(?=[\s/>])(?:[^"'<>]|"[^"]*"|'[^']*')*>''')
    attribute = re.compile(r'''\s+([A-Za-z_:][\w:.-]*)\s*=\s*("[^"]*"|'[^']*')''')

    def removable(name, value):
        return name in {"align", "valign"} or (name in {"rowspan", "colspan"} and value == "1")

    def signature(root, strip_layout):
        return [(node.tag, node.text, node.tail,
                 {k: v for k, v in node.attrib.items()
                  if not (strip_layout and node.tag.rsplit("}", 1)[-1] in {"td", "th"} and removable(k, v))})
                for node in root.iter()]

    output, transformations = [], []
    for index, (table, block) in enumerate(contextual_blocks(text)):
        original = block
        if table and len(block) > max_chars and _TABLE.search(block):
            try:
                before = ET.fromstring("<root>" + block + "</root>")
                compact = cell_tag.sub(lambda tag: attribute.sub(
                    lambda attr: "" if removable(attr[1], attr[2][1:-1]) else attr[0], tag[0]), block)
                after = ET.fromstring("<root>" + compact + "</root>")
                if len(compact) < len(block) and signature(before, True) == signature(after, False):
                    block = compact
                    transformations.append({"source": source, "block": index,
                        "method": "remove_redundant_cell_layout", "original_chars": len(original),
                        "chars": len(block), "original_sha256": _sha(original.encode()),
                        "sha256": _sha(block.encode()), "xml_content_verified": True})
            except ET.ParseError:
                # Existing malformed/oversized-table handling remains explicit.
                pass
        output.append(block)
    return "".join(output), transformations


def _archive(path: Path) -> ZipFile:
    archive = ZipFile(path)
    if sum(i.file_size for i in archive.infolist()) > MAX_SOURCE_BYTES:
        archive.close()
        raise ValueError("expanded Office document exceeds 32 MiB")
    return archive


def _docx(path: Path) -> str:
    with _archive(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        notes = [ET.fromstring(archive.read(name)) for name in
                 ("word/footnotes.xml", "word/endnotes.xml") if name in archive.namelist()]
    bodies = root.findall(_W + "body")
    if not bodies:
        raise ValueError("DOCX has no document body")
    note_texts = {}
    for note_root in notes:
        kind = "footnote" if note_root.tag == _W + "footnotes" else "endnote"
        for note in note_root:
            if note.get(_W + "type") not in {"separator", "continuationSeparator"}:
                note_texts[(kind, note.get(_W + "id"))] = note
    used_notes = set()

    def note_key(element):
        return ("footnote" if element.tag == _W + "footnoteReference" else "endnote",
                element.get(_W + "id"))

    def note_label(key):
        return f'[{key[0]} {key[1]}]'

    def text(node):
        parts = []
        def visit(element):
            if element.tag == _W + "t":
                parts.append(element.text or "")
            elif element.tag in {_W + "footnoteReference", _W + "endnoteReference"}:
                parts.append(" " + note_label(note_key(element)))
            elif element.tag == _W + "tab":
                parts.append("\t")
            elif element.tag in {_W + "br", _W + "cr"}:
                parts.append("\n")
            else:
                for child in element:
                    visit(child)
                if element.tag == _W + "p":
                    parts.append("\n")
        visit(node)
        # Runs are formatting boundaries, not word or number boundaries.
        return "".join(parts).strip()

    def attached_notes(node):
        keys = dict.fromkeys(note_key(element) for element in node.iter()
                             if element.tag in {_W + "footnoteReference", _W + "endnoteReference"})
        if any(key not in note_texts for key in keys):
            raise ValueError("DOCX contains an unresolved footnote or endnote reference")
        used_notes.update(keys)
        return "\n\nNotes: " + "; ".join(note_label(key) + " " + html.escape(text(note_texts[key])).replace("\n", "<br/>")
                                           for key in keys) if keys else ""

    out = []
    # Some publisher exports wrap each paragraph in a separate body element,
    # preceded by an empty body. Preserve every native block in document order
    # rather than treating the first empty body as the whole supplement.
    for node in (node for body in bodies for node in body):
        if node.tag != _W + "tbl":
            out.append(html.escape(text(node)) + attached_notes(node))
            continue
        if node.findall(f".//{_W}tbl"):
            raise ValueError("nested DOCX tables require separate source review")
        rows, active = [], {}
        for tr in node.findall(_W + "tr"):
            row, col = [], 0
            before = tr.find(f"{_W}trPr/{_W}gridBefore")
            if before is not None:
                col = int(before.get(_W + "val", "0"))
                if col:
                    row.append({"text": "", "colspan": col, "rowspan": 1, "header": False})
            next_active = {}
            for tc in tr.findall(_W + "tc"):
                span = tc.find(f"{_W}tcPr/{_W}gridSpan")
                width = int(span.get(_W + "val", "1")) if span is not None else 1
                merge = tc.find(f"{_W}tcPr/{_W}vMerge")
                continuing = merge is not None and merge.get(_W + "val") != "restart"
                if continuing:
                    anchor = active.get(col)
                    if anchor is None or anchor["colspan"] != width:
                        raise ValueError("DOCX has an unresolved vertical table merge")
                    anchor["rowspan"] += 1
                    next_active[col] = anchor
                else:
                    cell = {"text": html.escape(text(tc)), "colspan": width, "rowspan": 1,
                            "header": tr.find(f"{_W}trPr/{_W}tblHeader") is not None}
                    row.append(cell)
                    if merge is not None:
                        next_active[col] = cell
                col += width
            active = next_active
            rows.append(row)
        out.append("<table>" + "".join("<tr>" + "".join(
            f'<{"th" if c["header"] else "td"} colspan="{c["colspan"]}" rowspan="{c["rowspan"]}">{c["text"]}</{"th" if c["header"] else "td"}>'
            for c in row) + "</tr>" for row in rows) + "</table>" + attached_notes(node))
    for key, note in note_texts.items():
        if key not in used_notes:
            out.append(note_label(key) + " " + html.escape(text(note)))
    return "\n\n".join(out)


def _cell_position(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", value)
    if not match:
        raise ValueError("invalid spreadsheet cell reference")
    col = 0
    for letter in match[1]:
        col = col * 26 + ord(letter) - 64
    return int(match[2]), col


def _xlsx(path: Path, source: str) -> tuple[str, list[dict]]:
    issues, out = [], []
    with _archive(path) as archive:
        names = set(archive.namelist())
        strings = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(x.itertext()) for x in root.findall(_S + "si")]
        formats, styles = {}, []
        if "xl/styles.xml" in names:
            style = ET.fromstring(archive.read("xl/styles.xml"))
            formats = {x.get("numFmtId"): x.get("formatCode") for x in style.findall(f"{_S}numFmts/{_S}numFmt")}
            styles = [x.get("numFmtId", "0") for x in style.findall(f"{_S}cellXfs/{_S}xf")]
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target", "") for r in rels if r.get("TargetMode") != "External"}
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in book.findall(f"{_S}sheets/{_S}sheet"):
            target = targets.get(sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "")
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(member).parts or member not in names:
                raise ValueError("missing or unsafe worksheet relationship")
            root = ET.fromstring(archive.read(member))
            cells, spans, covered = {}, {}, set()
            for cell in root.findall(f"{_S}sheetData/{_S}row/{_S}c"):
                pos = _cell_position(cell.get("r", ""))
                val = cell.find(_S + "v")
                value = val.text if val is not None and val.text else ""
                if cell.get("t") == "s":
                    value = strings[int(value)]
                elif cell.get("t") == "inlineStr":
                    inline = cell.find(_S + "is")
                    if inline is None and value:
                        raise ValueError("spreadsheet inline string has a value but no inline text")
                    # Some publisher exports store an explicit empty cell as
                    # <c t="inlineStr"/>. Keep its coordinate and format, just
                    # as for <is/>, without fabricating a zero or dropping it.
                    value = "".join(inline.itertext()) if inline is not None else ""
                if cell.find(_S + "f") is not None:
                    code = "xlsx_formula_cached_unverified" if value else "xlsx_formula_cache_missing"
                    issues.append(_issue(source, code, sheet=sheet.get("name"), cell=cell.get("r")))
                    if not value:
                        value = "[formula cache missing]"
                fmtid = styles[int(cell.get("s", "0"))] if styles else "0"
                fmt = formats.get(fmtid, {"9": "0%", "10": "0.00%", "14": "date serial"}.get(fmtid, "builtin:" + fmtid))
                cells[pos] = (value, fmt)
            for merge in root.findall(f"{_S}mergeCells/{_S}mergeCell"):
                start, end = merge.get("ref", "").split(":")
                r1, c1 = _cell_position(start); r2, c2 = _cell_position(end)
                if (r2 - r1 + 1) * (c2 - c1 + 1) > 50000:
                    raise ValueError("spreadsheet merged range exceeds cell limit")
                spans[(r1, c1)] = (r2 - r1 + 1, c2 - c1 + 1)
                covered.update((r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1) if (r, c) != (r1, c1))
            positions = set(cells) | covered | set(spans)
            if not positions:
                continue
            height, width = max(r for r, c in positions), max(c for r, c in positions)
            if height * width > 50000:
                raise ValueError("spreadsheet exceeds 50000-cell extraction limit")
            rows = []
            for r in range(1, height + 1):
                row = []
                for c in range(1, width + 1):
                    if (r, c) in covered:
                        continue
                    value, fmt = cells.get((r, c), ("", "builtin:0"))
                    rowspan, colspan = spans.get((r, c), (1, 1))
                    row.append(f'<td rowspan="{rowspan}" colspan="{colspan}" data-number-format="{html.escape(fmt, quote=True)}">{html.escape(value)}</td>')
                rows.append("<tr>" + "".join(row) + "</tr>")
            out.append(f'<table><caption>Sheet: {html.escape(sheet.get("name", ""))}; state: {sheet.get("state", "visible")}. Numeric cells retain stored values and number formats.</caption>' + "".join(rows) + "</table>")
    return "\n\n".join(out), issues


def load_supplements(directory: Path) -> tuple[list[tuple[str, str, bytes]], list[dict]]:
    """Read only a paper's canonical supplementary directory; no legacy search."""
    docs, issues = [], []
    if not directory.exists():
        return docs, issues
    root = directory.resolve()
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    for path in files:
        if path.suffix.lower() == ".pdf":
            candidates = [path.with_suffix(".md"), path.with_suffix("") / "body.md"]
            # A converted PDF in its own folder follows the ordinary paper-store layout.
            if sum(p.suffix.lower() == ".pdf" for p in path.parent.iterdir()) == 1:
                candidates.append(path.parent / "body.md")
            paired = next((p for p in candidates if p.is_file()), None)
            if paired:
                from ber_core.conversion import read_conversion_quality
                quality = read_conversion_quality(paired.parent)
                if quality.get("requires_review") or quality.get("degraded"):
                    issues.append(_issue("supplementary/" + str(paired.relative_to(directory)),
                                         "supplement_conversion_requires_review", quality=quality))
            else:
                issues.append(_issue("supplementary/" + str(path.relative_to(directory)), "unconverted_pdf_supplement"))
    for path in files:
        source = "supplementary/" + path.relative_to(directory).as_posix()
        if not path.resolve().is_relative_to(root):
            issues.append(_issue(source, "supplement_outside_canonical_directory"))
            continue
        suffix = path.suffix.lower()
        if suffix == ".pdf" or suffix == ".json" or path.name in {"provenance.json", "references.md", "abstract.md"}:
            continue
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds 32 MiB")
            raw = path.read_bytes()
            if suffix == ".docx" or (suffix == ".doc" and raw.startswith(b"PK\x03\x04")):
                # Some publishers name an OOXML Word attachment mmc1.doc.
                # Inspect the ZIP package through the ordinary DOCX reader.
                text = _docx(path)
            elif suffix == ".xlsx":
                text, warnings = _xlsx(path, source)
                issues.extend(warnings)
            elif suffix in {".md", ".txt", ".html", ".htm", ".xml"}:
                text = raw.decode("utf-8-sig")
                if suffix == ".xml":
                    ET.fromstring(text)  # A corrupt native source must be visible.
            else:
                issues.append(_issue(source, "unsupported_supplement_format"))
                continue
            if not text.strip():
                raise ValueError("empty supplementary source")
            docs.append((source, text, raw))
        except Exception as exc:
            issues.append(_issue(source, "supplement_read_failed", detail=str(exc)[:300]))
    return docs, issues


def declared_supplement_gaps(ref) -> list[dict]:
    """Missing declared native attachments cannot look like complete inputs.

    FetchPDF may rename downloads. Its original-name mapping only counts when
    the actual canonical file exists and matches the recorded content hash.
    """
    directory = getattr(ref, "dir", None)
    xml = getattr(ref, "xml_path", None)
    if directory is None or not xml or not xml.is_file():
        return []
    try:
        root = ET.parse(xml).getroot()
    except (ET.ParseError, OSError):
        return [_issue(xml.name, "supplement_declarations_unreadable")]
    native = Path(directory) / "supplementary"
    files = {p.name: p for p in native.rglob('*') if p.is_file() and
             p.resolve().is_relative_to(native.resolve())} if native.exists() else {}
    available = set(files)
    for manifest in native.glob('*_supplementary_info.json'):
        try:
            rows = json.loads(manifest.read_text(encoding='utf-8')).get('files', [])
        except (ValueError, OSError):
            continue
        for row in rows:
            actual = files.get(row.get('filename'))
            if actual and row.get('sha256') and _sha(actual.read_bytes()) == row['sha256']:
                available.add(row.get('original_name', ''))
    gaps = []
    seen = set()
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] != 'supplementary-material':
            continue
        for item in node.iter():
            href = next((v for k,v in item.attrib.items() if k.rsplit('}',1)[-1] == 'href'), '')
            name = unquote(PurePosixPath(urlparse(href).path).name)
            if not name or name in seen or Path(name).suffix.lower() not in {
                    '.pdf','.doc','.docx','.xls','.xlsx','.csv','.tsv','.zip','.txt','.xml'}:
                continue
            seen.add(name)
            if name not in available:
                gaps.append(_issue(xml.name, 'declared_supplement_unavailable', attachment=name,
                                   href=href, reason='Declared source attachment is absent from the canonical supplementary inputs'))
    return gaps


def build_extraction_inputs(ref, primary_text: str, primary_source: str, *,
                            all_outcomes: bool = False, include_supplementary: bool = True,
                            max_chars: int = 80000, max_batches: int = MAX_BATCHES) -> ExtractionInputs:
    """Read complete evidence chunks up to the shared, explicit batch limit.

    ``all_outcomes`` is retained for callers; outcome scope belongs to the
    prompt, not the source budget. Primary outcomes can occur late in a paper
    or supplement, so either scope can require up to ``max_batches`` calls.
    """
    result = ExtractionInputs()
    if isinstance(max_batches, bool) or not isinstance(max_batches, int) or not 1 <= max_batches <= 20:
        raise ValueError("fulltext.extraction_max_batches must be an integer between 1 and 20")
    source_path = getattr(ref, {"md": "md_path", "xml": "xml_path", "pdf": "pdf_path", "pdf-native": "pdf_path"}.get(primary_source, "md_path"), None)
    name = source_path.name if source_path else "main." + primary_source
    docs = [(name, primary_text, primary_text.encode(), primary_source)]
    directory = getattr(ref, "dir", None)
    if primary_source == "xml":
        # Prefer publisher XML, then add the PDF conversion when XML explicitly
        # lacks table cells. Keep BOTH source records; a fallback must not erase
        # richer supported XML results or silently resolve disagreements.
        _, primary_issues = chunk_text(primary_text, max_chars, name)
        gaps = {"table_cells_unavailable", "table_image_requires_review", "malformed_table"}
        if any(issue["code"] in gaps for issue in primary_issues):
            md = getattr(ref, "md_path", None)
            if md and md.is_file():
                raw = md.read_bytes()
                if len(raw) > MAX_SOURCE_BYTES:
                    result.issues.append(_issue(md.name, "fallback_source_too_large"))
                elif raw.strip():
                    docs.append((md.name, raw.decode("utf-8-sig"), raw, "md"))
                    result.transformations.append({"kind": "xml_missing_tables_pdf_conversion_added",
                        "source": name, "fallback_source": md.name,
                        "fallback_sha256": _sha(raw), "requires_source_adjudication": True})
                    if directory is not None:
                        from ber_core.conversion import read_conversion_quality
                        quality = read_conversion_quality(Path(directory))
                        if quality.get("requires_review") or quality.get("degraded"):
                            result.issues.append(_issue(md.name, "fallback_conversion_requires_review", quality=quality))
                else:
                    result.issues.append(_issue(name, "xml_table_fallback_unavailable"))
            else:
                result.issues.append(_issue(name, "xml_table_fallback_unavailable"))
    if include_supplementary and directory is not None:
        result.issues.extend(declared_supplement_gaps(ref))
        supplements, issues = load_supplements(Path(directory) / "supplementary")
        docs += [(source, text, raw, "supplement") for source, text, raw in supplements]
        result.issues.extend(issues)
    for doc_index, (source, text, raw, source_kind) in enumerate(docs):
        bounded_text, transformations = compact_table_layout(text, max_chars, source)
        result.transformations.extend(transformations)
        chunks, issues = chunk_text(bounded_text, max_chars, source)
        result.issues.extend(issues)
        if source_path and doc_index == 0 and source_path.exists():
            raw = source_path.read_bytes()
        info = {"source": source, "sha256": _sha(raw), "chars": len(text), "chunks": len(chunks)}
        result.sources.append(info)
        for chunk_index, chunk in enumerate(chunks):
            if len(result.batches) >= max_batches:
                result.issues.append(_issue(source, "input_chunk_omitted", chunk=chunk_index,
                                           reason="batch_limit"))
                continue
            result.batches.append(InputBatch(f"source-{doc_index + 1}-part-{chunk_index + 1}", source, chunk,
                                             source_kind))
    return result


def _norm(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return str(int(value)) if value == int(value) else str(value)
    return re.sub(r"\s+", " ", str("" if value is None else value).strip().casefold())


def _identity(node, fields):
    return tuple(_norm(node.get(key)) for key in fields)


def _objects(value):
    return [x for x in value or [] if isinstance(x, dict)]


def _fresh_id(nodes, field):
    """Use numeric IDs compatible with legacy comparison-label consumers."""
    used = {_norm(n.get(field)) for n in nodes}
    candidate = 1
    while str(candidate) in used:
        candidate += 1
    return candidate


def stamp_extraction_source(record: dict, source: str) -> dict:
    """The actual input source takes precedence over a model's filename guess."""
    out = deepcopy(record)
    out.setdefault("_batch_metadata", []).append(_metadata_fragment(record, source))
    for outcome in _objects(out.get("outcomes")):
        for section in ("arm_results", "between_group_effects", "within_group_change"):
            for node in _objects(outcome.get(section)):
                node["source_file"] = source
                location = node.get("source_location")
                if not location or not str(location).startswith(source):
                    node["source_location"] = source + (": " + str(location) if location else "")
    return out


def _metadata_fragment(record: dict, source: str) -> dict:
    # Keep native IDs together with their arm definitions. These are source
    # fragments for adjudication, not silently combined whole-paper metadata.
    return {"source": source, "record": deepcopy({
        key: value for key, value in record.items()
        if key != "outcomes" and not key.startswith("_")})}


def _metadata_evidence(value):
    """Ignore abstentions and provenance prose; preserve populated arm identity."""
    if isinstance(value, dict):
        evidence = {k: v for k, raw in value.items()
                if k not in {"arm_id", "paper_id"} and not k.endswith(("_source", "_quote"))
                and (v := _metadata_evidence(raw)) is not None}
        if evidence and "arm_id" in value:
            evidence["arm_id"] = value["arm_id"]
        return evidence or None
    if isinstance(value, list):
        return [v for raw in value if (v := _metadata_evidence(raw)) is not None] or None
    if value is None or (isinstance(value, str) and value.strip().lower() in
                         {"", "unknown", "unclear", "not_reported", "unstated"}):
        return None
    return value


def _retain_batch_metadata(base: dict, incoming: dict, source: str) -> dict:
    out = deepcopy(base)
    if not out.get("_batch_metadata"):
        out["_batch_metadata"] = [_metadata_fragment(base, base.get("_source_file"))]
    fragment = _metadata_fragment(incoming, source)
    out["_batch_metadata"].append(fragment)
    pending = out.setdefault("_metadata_review", [])
    for section, value in fragment["record"].items():
        if section in {"paper_id", "summary_of"}:
            continue
        evidence = _metadata_evidence(value)
        if evidence is None or evidence == _metadata_evidence(base.get(section)):
            continue
        pending.append({"section": section, "source": source,
                        "reason": "Later source metadata requires reconciliation; original values and arm IDs are retained in _batch_metadata."})
        # Study-level disagreements can change which participants, allocation
        # or denominator an effect represents. Do not certify those effects
        # while keeping only the first batch's interpretation.
        if section in {"study_design", "participants", "sample_sizes", "follow_up"}:
            out.setdefault("_source_findings", []).append({
                "code": "extraction_metadata_requires_review", "path": "outcomes",
                "severity": "blocking", "source_file": source,
                "reason": f"Unreconciled {section} across source batches; see _batch_metadata."})
    # A first-section narrative cannot stand in for a whole-paper synthesis.
    # Retain every original narrative above, including notes about absent tables.
    if base.get("summary") or incoming.get("summary"):
        out["summary"] = None
        out["summary_of"] = None
        if not any(p["section"] == "summary" for p in pending):
            pending.append({"section": "summary", "source": source,
                            "reason": "Whole-paper summary requires synthesis of all source sections."})
    return out


def merge_extraction_records(base: dict, incoming: dict, source: str) -> dict:
    """Merge only exact semantic identities; never overwrite a conflicting fact.

    Conflicting incoming evidence remains in `_merge_conflicts` and blocking
    findings address the existing result node so scientific selection withholds
    it. Unresolvable arm/timepoint identities remain in `_unmerged_extractions`.
    """
    metadata_base = _retain_batch_metadata(base, incoming, source)
    out = deepcopy(metadata_base)
    arms = out.setdefault("study_design", None) or {}
    out["study_design"] = arms
    base_arms = _objects(arms.get("arms"))
    arms["arms"] = base_arms
    arm_map = {}
    arm_fields = ("label", "intervention_name", "type", "dose", "route", "frequency", "duration_value", "duration_unit")

    def unresolved(reason, affected_outcomes=None):
        original = deepcopy(metadata_base)
        original.setdefault("_unmerged_extractions", []).append({"source": source, "reason": reason, "record": deepcopy(incoming)})
        original_outcomes = _objects(original.get("outcomes"))
        if affected_outcomes is None:
            names = {_norm(o.get("name")) for o in _objects(incoming.get("outcomes")) if o.get("name")}
            affected_outcomes = [oi for oi, o in enumerate(original_outcomes) if _norm(o.get("name")) in names]
        for oi in affected_outcomes:
            if oi >= len(original_outcomes):
                continue
            for section in ("arm_results", "between_group_effects", "within_group_change"):
                for ni, _ in enumerate(_objects(original_outcomes[oi].get(section))):
                    original.setdefault("_source_findings", []).append({
                        "code": "extraction_batch_identity_ambiguous", "path": f"outcomes.{oi}.{section}.{ni}",
                        "severity": "blocking", "reason": "Potentially duplicate evidence has conflicting or missing identity metadata; source review is required.",
                        "source_file": source})
        return original

    for arm in _objects((incoming.get("study_design") or {}).get("arms")):
        if not (arm.get("label") or arm.get("intervention_name")) or arm.get("arm_id") is None:
            return unresolved("unresolved_arm_identity")
        if arm["arm_id"] in arm_map:
            return unresolved("duplicate_arm_id")
        matches = [a for a in base_arms if _identity(a, arm_fields) == _identity(arm, arm_fields)]
        if len(matches) > 1:
            return unresolved("ambiguous_arm_identity")
        if matches:
            arm_map[arm["arm_id"]] = matches[0]["arm_id"]
        else:
            # Similar arm names with changed metadata need adjudication, not a
            # second intervention arm invented by a partial-document extraction.
            if any(_norm(a.get("label")) == _norm(arm.get("label")) or
                   (arm.get("intervention_name") and _norm(a.get("intervention_name")) == _norm(arm.get("intervention_name"))) for a in base_arms):
                return unresolved("conflicting_arm_definition")
            new = deepcopy(arm)
            new["arm_id"] = _fresh_id(base_arms, "arm_id")
            arm_map[arm["arm_id"]] = new["arm_id"]
            base_arms.append(new)

    outcomes = _objects(out.get("outcomes"))
    out["outcomes"] = outcomes
    identity_fields = ("name", "measurement_instrument", "unit", "outcome_concept", "synthesis_population", "analysis_basis", "analysis_population")
    tp_fields = ("label", "role", "weeks_from_randomization", "weeks_from_enrollment", "elapsed_hours")

    def conflict(path, current, proposed):
        out.setdefault("_merge_conflicts", []).append({"path": path, "source": source,
                                                      "existing": deepcopy(current), "incoming": deepcopy(proposed)})
        out.setdefault("_source_findings", []).append({"code": "extraction_batch_conflict", "path": path,
            "severity": "blocking", "reason": "Conflicting values across extraction batches require source review.", "source_file": source})

    for raw in _objects(incoming.get("outcomes")):
        outcome = deepcopy(raw)
        if not outcome.get("name"):
            return unresolved("unresolved_outcome_identity")
        matches = [o for o in outcomes if _identity(o, identity_fields) == _identity(outcome, identity_fields)]
        if len(matches) > 1:
            return unresolved("ambiguous_outcome_identity")
        dest = matches[0] if matches else None
        if dest is None:
            similar = [oi for oi, o in enumerate(outcomes)
                       if _norm(o.get("name")) == _norm(outcome.get("name"))]
            # Endpoint and change summaries are distinct measurements even when
            # a paper uses the same outcome title. Keep both records; do not
            # merge their cells or infer a basis when either source omits it.
            distinct_bases = {"endpoint", "change_from_baseline"}
            ambiguous = [oi for oi in similar if not (
                _norm(outcomes[oi].get("analysis_basis")) in distinct_bases
                and _norm(outcome.get("analysis_basis")) in distinct_bases
                and _norm(outcomes[oi]["analysis_basis"]) != _norm(outcome["analysis_basis"])
            )]
            if ambiguous:
                return unresolved("ambiguous_outcome_identity", ambiguous)
        tps = _objects(dest.get("timepoints")) if dest else []
        # Ambiguity is a cross-source question. A single incoming record may
        # explicitly distinguish rest/peak or two same-day visits while rounding
        # both week values to zero. Newly added timepoints must not make the next
        # timepoint from that same record look like an ambiguous cross-source ID.
        prior_tps = list(tps)
        tp_map = {}
        for tp in _objects(outcome.get("timepoints")):
            if tp.get("timepoint_id") is None or not any(tp.get(k) is not None for k in tp_fields):
                return unresolved("unresolved_timepoint_identity")
            if tp["timepoint_id"] in tp_map:
                return unresolved("duplicate_timepoint_id")
            matched = [t for t in tps if _identity(t, tp_fields) == _identity(tp, tp_fields)]
            if len(matched) > 1:
                return unresolved("ambiguous_timepoint_identity")
            if matched:
                tp_map[tp["timepoint_id"]] = matched[0]["timepoint_id"]
            else:
                if any(any(tp.get(k) is not None and t.get(k) is not None and
                           _norm(tp[k]) == _norm(t[k]) for k in tp_fields if k != "role") for t in prior_tps):
                    return unresolved("ambiguous_timepoint_identity", [outcomes.index(dest)] if dest else [])
                new = deepcopy(tp)
                new["timepoint_id"] = _fresh_id(tps, "timepoint_id")
                tp_map[tp["timepoint_id"]] = new["timepoint_id"]
                tps.append(new)
        if outcome.get("primary_timepoint_id") is not None:
            if outcome["primary_timepoint_id"] not in tp_map:
                return unresolved("unresolved_primary_timepoint")
            outcome["primary_timepoint_id"] = tp_map[outcome["primary_timepoint_id"]]
        for section in ("arm_results", "between_group_effects", "within_group_change"):
            for node in _objects(outcome.get(section)):
                for key in ("arm_id", "intervention_arm_id", "comparator_arm_id"):
                    if node.get(key) is not None:
                        if node[key] not in arm_map:
                            return unresolved("unresolved_result_arm")
                        node[key] = arm_map[node[key]]
                for time_key in ("timepoint_id", "timepoint_from", "timepoint_to"):
                    if node.get(time_key) is not None:
                        if node[time_key] not in tp_map:
                            return unresolved("unresolved_result_timepoint")
                        node[time_key] = tp_map[node[time_key]]
                if section == "between_group_effects" and node.get("comparison"):
                    # Comparison strings can encode the old numeric arm IDs.
                    if node.get("intervention_arm_id") is None or node.get("comparator_arm_id") is None:
                        return unresolved("comparison_requires_explicit_arm_ids")
                    node["comparison"] = f'arm{node["intervention_arm_id"]}_vs_arm{node["comparator_arm_id"]}'
                node["source_file"] = source
                location = node.get("source_location")
                if not location or not str(location).startswith(source):
                    node["source_location"] = source + (": " + str(location) if location else "")
        if dest is None:
            outcome["outcome_id"] = _fresh_id(outcomes, "outcome_id")
            outcome["timepoints"] = tps
            outcomes.append(outcome)
            continue
        oi = outcomes.index(dest)
        dest["timepoints"] = tps
        for section in ("arm_results", "between_group_effects", "within_group_change"):
            nodes = _objects(dest.get(section))
            dest[section] = nodes
            keys = {
                "arm_results": ("arm_id", "timepoint_id", "result_type", "analysis_population"),
                "between_group_effects": ("intervention_arm_id", "comparator_arm_id", "timepoint_id", "effect_measure", "result_type", "analysis_population", "adjusted"),
                "within_group_change": ("arm_id", "timepoint_from", "timepoint_to", "result_type", "analysis_population"),
            }[section]
            for node in _objects(outcome.get(section)):
                existing = [n for n in nodes if _identity(n, keys) == _identity(node, keys)]
                if not existing:
                    # An omitted optional result discriminator cannot establish
                    # a second measurement. Preserve and block both candidates.
                    possible = [n for n in nodes if all(not _norm(n.get(k)) or not _norm(node.get(k)) or
                                                       _norm(n[k]) == _norm(node[k]) for k in keys)]
                    if possible:
                        for target in possible:
                            conflict(f"outcomes.{oi}.{section}.{nodes.index(target)}", target, node)
                        continue
                    nodes.append(node)
                    continue
                target = existing[0]
                # An earlier methods-only batch can explicitly abstain on
                # uncertainty while a later table supplies it. These enum
                # sentinels are missing information, not competing estimates.
                missing_details = {
                    "mean_type": {"unknown"}, "uncertainty_type": {"unknown"},
                    "uncertainty_scale": {"unknown"}, "p_value_sidedness": {"unknown"},
                    "dispersion_reported_as": {"unstated"},
                    "range_low_operator": {"unknown"}, "range_high_operator": {"unknown"},
                    "result_source": {"not_reported"}, "effect_source": {"not_reported"},
                }
                def missing_detail(key, value):
                    return value is None or (isinstance(value, str) and
                                             value in missing_details.get(key, set()))
                conflicts = {k for k, v in node.items() if not missing_detail(k, v) and not missing_detail(k, target.get(k)) and target[k] != v
                             and k not in {"source_file", "source_location", "result_quote", "effect_quote", "result_source", "effect_source", "_field_sources", "_source_files"}}
                path = f"outcomes.{oi}.{section}.{nodes.index(target)}"
                if conflicts or len(existing) > 1:
                    conflict(path, target, node)
                else:
                    source_files = target.setdefault("_source_files", [])
                    for filename in (target.get("source_file"), source):
                        if filename and filename not in source_files:
                            source_files.append(filename)
                    for key, value in node.items():
                        if missing_detail(key, target.get(key)) and not missing_detail(key, value):
                            target[key] = value
                            if value is not None and not key.startswith("_"):
                                target.setdefault("_field_sources", {})[key] = source
        for key in ("is_primary", "higher_is_better", "primary_timepoint_id", "standardization", "smd_construct"):
            if outcome.get(key) is not None and dest.get(key) is not None and outcome[key] != dest[key]:
                # Metadata conflicts affect every numerical interpretation.
                for section in ("arm_results", "between_group_effects", "within_group_change"):
                    for ni, node in enumerate(_objects(dest.get(section))):
                        conflict(f"outcomes.{oi}.{section}.{ni}", {key: dest[key]}, {key: outcome[key]})
            elif dest.get(key) is None and outcome.get(key) is not None:
                dest[key] = outcome[key]
    return out
