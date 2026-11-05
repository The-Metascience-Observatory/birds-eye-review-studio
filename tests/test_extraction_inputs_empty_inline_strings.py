"""Publisher XLSX blank cells must retain location without erasing stored data."""
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from ber_core.extraction_inputs import _xlsx, load_supplements

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _workbook(path, rows, merges=""):
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Outcomes" sheetId="1" r:id="r1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{_NS}"><si><t>Shared label</t></si></sst>')
        archive.writestr("xl/styles.xml", f'<styleSheet xmlns="{_NS}"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="10"/></cellXfs></styleSheet>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{_NS}"><sheetData>{rows}</sheetData>{merges}</worksheet>')


def _table(text):
    return ET.fromstring(text)


@pytest.mark.parametrize("inline", ["", "<is/>", "<is><t/></is>"])
def test_empty_inline_string_keeps_cell_position_and_zero_distinction(tmp_path, inline):
    path = tmp_path / "empty.xlsx"
    _workbook(path, f'<row r="1"><c r="A1" t="inlineStr">{inline}</c><c r="B1"><v>0</v></c><c r="C1"><v>1.20</v></c></row>')
    text, issues = _xlsx(path, path.name)
    cells = _table(text).findall("tr/td")
    assert [cell.text or "" for cell in cells] == ["", "0", "1.20"]
    assert issues == []


def test_empty_inline_string_keeps_blank_edges_spans_rich_text_and_formats(tmp_path):
    path = tmp_path / "layout.xlsx"
    _workbook(path, '''<row r="1"><c r="A1" t="inlineStr" s="1"/><c r="C1" t="inlineStr"><is><r><t>2.</t></r><r><t>40</t></r></is></c></row>
        <row r="2"><c r="C2"><v>0</v></c></row>
        <row r="3"><c r="A3" t="s"><v>0</v></c><c r="B3" t="inlineStr"><is><t>&lt;0.001</t></is></c></row>
        <row r="4"><c r="D4" t="inlineStr" s="1"/></row>''', '<mergeCells><mergeCell ref="A1:B2"/></mergeCells>')
    text, issues = _xlsx(path, path.name)
    rows = _table(text).findall("tr")
    assert len(rows) == 4
    assert [len(row.findall("td")) for row in rows] == [3, 2, 4, 4]
    anchor = rows[0][0]
    assert anchor.attrib == {"rowspan": "2", "colspan": "2", "data-number-format": "0.00%"}
    assert (anchor.text or "") == ""
    assert rows[0][1].text == "2.40"
    assert rows[1][0].text == "0"
    assert rows[2][0].text == "Shared label"
    assert rows[2][1].text == "<0.001"
    assert [cell.text or "" for cell in rows[3]] == ["", "", "", ""]
    assert rows[3][3].get("data-number-format") == "0.00%"
    assert issues == []


def test_missing_inline_text_does_not_discard_a_nonempty_stored_value(tmp_path):
    path = tmp_path / "ambiguous.xlsx"
    _workbook(path, '<row r="1"><c r="A1" t="inlineStr"><v>9.5</v></c></row>')
    with pytest.raises(ValueError, match="value but no inline text"):
        _xlsx(path, path.name)
    docs, issues = load_supplements(tmp_path)
    assert docs == []
    assert len(issues) == 1 and issues[0]["code"] == "supplement_read_failed"


def test_empty_inline_formula_retains_missing_cache_hold(tmp_path):
    path = tmp_path / "formula.xlsx"
    _workbook(path, '<row r="1"><c r="A1" t="inlineStr"><f>IF(1,1,0)</f></c><c r="B1"><f>1-1</f><v>0</v></c></row>')
    text, issues = _xlsx(path, path.name)
    assert [cell.text for cell in _table(text).findall("tr/td")] == ["[formula cache missing]", "0"]
    assert [(issue["cell"], issue["code"]) for issue in issues] == [("A1", "xlsx_formula_cache_missing"), ("B1", "xlsx_formula_cached_unverified")]
