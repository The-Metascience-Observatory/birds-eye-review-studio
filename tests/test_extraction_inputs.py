"""Source preservation tests with synthetic documents and mocked model calls."""
from copy import deepcopy
import importlib.util
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from ber_core.extraction_inputs import (
    build_extraction_inputs, chunk_text, compact_table_layout, load_supplements, merge_extraction_records,
)
from ber_core.scientific import validate_record, selected_comparisons
from ber_core.utils import PaperRef, read_best_text


def ref(tmp_path):
    return PaperRef("10.1000/test", "10.1000--test", "typed", tmp_path / "body.md", None, None, tmp_path)


@pytest.mark.parametrize("table", [
    '<table><tr><th colspan="2">Intervention</th></tr><tr><td>mean</td><td>9.2</td></tr></table>',
    '| outcome | Mean | SD |\n|---|---|---|\n| Fatigue | 9.2 | 1.3 |\n',
    'outcome | Mean | SD\n---|---|---\nFatigue | 9.2 | 1.3\n',
    '<table-wrap><label>Table 3</label><table><tr><td>9.2</td></tr></table><table-wrap-foot>Mean (SD)</table-wrap-foot></table-wrap>',
])
def test_table_crossing_budget_is_complete_in_later_batch(table):
    text = "Intro " * 40 + "\n\n" + table
    chunks, issues = chunk_text(text, 256)
    assert issues == []
    assert len(chunks) > 1
    assert any(table in chunk for chunk in chunks)
    assert all(len(chunk) <= 256 for chunk in chunks)
    assert "".join(chunks) == text


def test_oversized_and_malformed_tables_are_never_partially_sent():
    oversized = '<table><tr><td>' + '123 ' * 100 + '</td></tr></table>'
    chunks, issues = chunk_text("Intro\n\n" + oversized, 256)
    assert all('<table' not in c for c in chunks)
    assert issues[0]["code"] == "oversized_table"
    chunks, issues = chunk_text('<table><tr><td>1</td></tr>', 256)
    assert chunks == []
    assert issues[0]["code"] == "malformed_table"


@pytest.mark.parametrize('table', [
    '<table><caption>Table II. Changes in symptoms</caption></table>',
    '<table><tr><td> </td><td>&nbsp;</td></tr></table>',
    '<j:table><j:caption>Table 3</j:caption><j:tr><j:td><graphic href="table.png"/></j:td></j:tr></j:table>',
    '<ce:table><cals:tgroup><cals:tbody><cals:row><ce:entry> </ce:entry></cals:row></cals:tbody></cals:tgroup></ce:table>',
])
def test_caption_only_or_unreadable_table_cannot_claim_complete_inputs(tmp_path, table):
    text = 'Results describe improvement.\n\n' + table
    plan = build_extraction_inputs(ref(tmp_path), text, 'md', max_chars=2000)
    assert ''.join(batch.text for batch in plan.batches) == text
    assert plan.manifest()['status'] == 'partial'
    assert plan.manifest()['requires_review']
    assert [issue['code'] for issue in plan.issues] == ['table_cells_unavailable']


@pytest.mark.parametrize('text', [
    '<table><tr><th>Finding</th><th>No adverse effects reported</th></tr></table>',
    '<table><tr><td>0</td><td><b>3.5</b></td></tr></table>',
    '<ce:table><cals:tgroup cols="2"><cals:tbody><cals:row><ce:entry colname="col1">35</ce:entry><ce:entry colname="col2">p &lt;0.001</ce:entry></cals:row></cals:tbody></cals:tgroup></ce:table>',
    '<figure><caption>Figure 1. Flow chart</caption></figure>',
])
def test_text_tables_and_non_table_figures_do_not_trigger_missing_cells(text):
    chunks, issues = chunk_text(text, 2000)
    assert ''.join(chunks) == text
    assert issues == []


def test_image_only_jats_table_adds_pdf_conversion_without_discarding_xml(tmp_path):
    paper = ref(tmp_path)
    xml = '<body><p>Mean pain 4.2</p><table-wrap id="T2"><label>Table 2</label><graphic href="table.jpg"/></table-wrap></body>'
    md = '<table><caption>Table 2</caption><tr><td>Fatigue</td><td>6.8</td></tr></table>'
    paper.md_path.write_text(md)
    plan = build_extraction_inputs(paper, xml, 'xml', max_chars=2000)
    assert [b.text for b in plan.batches] == [xml, md]
    assert [b.source_kind for b in plan.batches] == ['xml', 'md']
    assert plan.manifest()['requires_review']  # fallback is evidence, not adjudication
    assert plan.transformations[0]['kind'] == 'xml_missing_tables_pdf_conversion_added'


def test_jats_table_gap_without_pdf_conversion_remains_explicit(tmp_path):
    xml = '<table-wrap><graphic href="image.png"/></table-wrap>'
    plan = build_extraction_inputs(ref(tmp_path), xml, 'xml', max_chars=2000)
    assert {i['code'] for i in plan.issues} == {'table_cells_unavailable','xml_table_fallback_unavailable'}
    assert plan.batches[0].text == xml


def test_partial_image_table_with_some_text_still_requires_review():
    _, issues = chunk_text('<table><tr><th>Results</th><td><img src="values.png"/></td></tr></table>', 1000)
    assert [i['code'] for i in issues] == ['table_image_requires_review']


def test_declared_supplement_requires_actual_verified_file(tmp_path):
    import hashlib
    xml = tmp_path/'article.xml'
    xml.write_text('<article xmlns:xlink="http://www.w3.org/1999/xlink"><back><supplementary-material><media xlink:href="mmc1.doc"/></supplementary-material></back></article>')
    paper = SimpleNamespace(dir=tmp_path, xml_path=xml, md_path=None, pdf_path=None)
    def missing():
        return [i for i in build_extraction_inputs(paper,'Article text','xml').issues
                if i['code']=='declared_supplement_unavailable']
    assert missing()[0]['attachment']=='mmc1.doc'
    si=tmp_path/'supplementary';si.mkdir()
    manifest=si/'article_supplementary_info.json'
    manifest.write_text(json.dumps({'files':[{'filename':'renamed.doc','original_name':'mmc1.doc',
                                            'sha256':hashlib.sha256(b'contents').hexdigest()}]}))
    assert missing()  # a receipt alone is insufficient
    (si/'renamed.doc').write_bytes(b'contents')
    assert not missing()
    (si/'renamed.doc').write_bytes(b'changed contents')
    assert missing()


def test_docx_with_publisher_doc_extension_keeps_tables(tmp_path):
    directory=tmp_path/'supplementary';directory.mkdir()
    path=directory/'mmc1.doc'
    write_docx(path)
    docs, issues=load_supplements(directory)
    assert not issues
    assert '<table>' in docs[0][1] and 'colspan="2"' in docs[0][1]


def test_redundant_layout_recovery_preserves_complete_table_and_source(tmp_path):
    import hashlib
    import xml.etree.ElementTree as ET
    table = ('<table-wrap id="T2"><caption>Mean ± SE; not SD</caption><table>'
             '<tr><th colspan="2" align="center">Follow-up</th></tr>'
             '<tr><td rowspan="2" align="right" valign="middle" colspan="1">Fatigue</td>'
             '<td align="right" valign="middle" rowspan="1" colspan="1" '
             'data-number-format="0%" style="font-weight:bold">−1.21<xref rid="n1">*</xref></td></tr>'
             '<tr><td align="right" valign="middle" rowspan="1" colspan="1">0.25</td></tr>'
             '</table><fn id="n1">Adjusted; p&lt;.001</fn></table-wrap>')
    paper = ref(tmp_path);paper.md_path.write_text(table)
    plan = build_extraction_inputs(paper, table, 'md', max_chars=400)
    assert not plan.issues
    assert len(plan.batches) == 1
    compact = plan.batches[0].text
    assert len(compact) <= 400 < len(table)
    assert list(ET.fromstring(compact).itertext()) == list(ET.fromstring(table).itertext())
    assert 'rowspan="2"' in compact and 'colspan="2"' in compact
    assert 'rid="n1"' in compact and 'data-number-format="0%"' in compact
    assert 'style="font-weight:bold"' in compact
    assert paper.md_path.read_text() == table
    assert plan.sources[0]['sha256'] == hashlib.sha256(table.encode()).hexdigest()
    proof = plan.manifest()['transformations'][0]
    assert proof['xml_content_verified']
    assert proof['original_sha256'] == plan.sources[0]['sha256']
    assert proof['sha256'] == hashlib.sha256(compact.encode()).hexdigest()


def test_table_compaction_keeps_attribute_contents_and_mixed_text_exact():
    table = ('''<table><tr><td title='contains align="right" and rowspan="1"' align="left" '''
             '''valign="middle"> 9.<sup>2</sup> − 4 &amp; 5 </td></tr></table>''')
    text, proof = compact_table_layout(table, 100, 'body.xml')
    assert '''title='contains align="right" and rowspan="1"'>''' in text
    assert '> 9.<sup>2</sup> − 4 &amp; 5 </td>' in text
    assert proof[0]['xml_content_verified']
    assert compact_table_layout(table, len(table), 'body.xml') == (table, [])


@pytest.mark.parametrize('table', [
    '<table><tr><td align="right">' + '123 ' * 100 + '</td></tr></table>',
    '<table><tr><td align="right">' + '123 ' * 100 + '</td></tr>',
])
def test_compaction_never_sends_partial_oversized_or_invalid_table(tmp_path, table):
    plan = build_extraction_inputs(ref(tmp_path), table, 'md', max_chars=256)
    assert not plan.batches
    assert plan.issues[0]['code'] == 'oversized_table'


def test_caption_and_footnote_move_with_table_at_budget_boundary():
    intro = 'Methods: randomized study. ' * 7 + '\n\n'
    caption = 'Table 2. Fatigue at three months (median [IQR]).\n\n'
    table = '<table><tr><td>76.26 [73.33-80.95]</td></tr></table>'
    footnote = '\n\nNote. Values are medians and IQRs, not confidence intervals.'
    text = intro + caption + table + footnote
    chunks, issues = chunk_text(text, 256)
    assert issues == []
    assert len(chunks) == 2
    assert caption + table + footnote in chunks[1]
    assert ''.join(chunks) == text


@pytest.mark.parametrize('limit', [0, -1, 21, True, '6', 1.5, None])
def test_invalid_batch_limit_fails_explicitly(tmp_path, limit):
    with pytest.raises(ValueError, match='integer between 1 and 20'):
        build_extraction_inputs(ref(tmp_path), 'main', 'md', max_batches=limit)


def test_full_reader_is_opt_in_and_primary_scope_reads_late_main_table(tmp_path):
    paper = ref(tmp_path)
    paper.md_path.write_text("A readable sentence. " * 5000 + '\n\n<table><tr><td>Late outcome</td></tr></table>')
    limited, _ = read_best_text(paper)
    full, source = read_best_text(paper, max_chars=None)
    assert len(limited) == 80000
    assert len(full) > 80000
    inputs = build_extraction_inputs(paper, full, source)
    assert len(inputs.batches) == 2
    assert not inputs.manifest()["requires_review"]
    assert 'Late outcome' in inputs.batches[-1].text
    deep = build_extraction_inputs(paper, full, source, all_outcomes=True)
    assert [b.text for b in deep.batches] == [b.text for b in inputs.batches]


@pytest.mark.parametrize('all_outcomes', [False, True])
def test_batch_limit_reports_every_omitted_part(tmp_path, all_outcomes):
    plan = build_extraction_inputs(ref(tmp_path), "text " * 300, "md", all_outcomes=all_outcomes,
                                   max_chars=256, max_batches=2)
    assert len(plan.batches) == 2
    assert plan.manifest()["status"] == "partial"
    assert all(i["reason"] == "batch_limit" for i in plan.issues)


def write_docx(path):
    xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>Supplement 1. Mean and SD.</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Intervention</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>Fatigue</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>9.2 (1.3)</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>8.1 (1.0)</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl></w:body></w:document>'''
    with ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
        z.writestr("word/footnotes.xml", '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>SD, not SE</w:t></w:r></w:p></w:footnote></w:footnotes>')


def test_native_docx_spans_headers_and_footnotes(tmp_path):
    supp = tmp_path / "supplementary"; supp.mkdir()
    write_docx(supp / "outcomes.docx")
    docs, issues = load_supplements(supp)
    assert issues == []
    text = docs[0][1]
    assert '<th colspan="2" rowspan="1">Intervention</th>' in text
    assert '<td colspan="1" rowspan="2">Fatigue</td>' in text
    assert text.count('Fatigue') == 1
    assert '[footnote 1]' in text and 'SD, not SE' in text
    plan = build_extraction_inputs(ref(tmp_path), "Main paper", "md", all_outcomes=False)
    assert len(plan.batches) == 2  # Primary data may be in a supplement.
    assert plan.sources[1]['source'] == 'supplementary/outcomes.docx'


def test_docx_formatting_runs_preserve_decimal_and_word_adjacency(tmp_path):
    supp = tmp_path / 'supplementary'; supp.mkdir()
    with ZipFile(supp / 'runs.docx', 'w') as z:
        z.writestr('word/document.xml', '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc>
        <w:p><w:r><w:t>80.</w:t></w:r><w:r><w:t>95</w:t></w:r><w:r><w:tab/><w:t>Fat</w:t></w:r><w:r><w:t>igue</w:t><w:br/><w:t>Mean</w:t></w:r></w:p>
        <w:p><w:r><w:t>SD</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>''')
    docs, issues = load_supplements(supp)
    assert not issues
    assert '>80.95\tFatigue\nMean\nSD</td>' in docs[0][1]


def test_docx_publisher_export_preserves_all_bodies_after_empty_first_body(tmp_path):
    supp=tmp_path/'supplementary';supp.mkdir()
    with ZipFile(supp/'survey.docx','w') as archive:
        archive.writestr('word/document.xml','''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:body/><w:body><w:p><w:r><w:t>Survey inclusion criteria</w:t></w:r></w:p></w:body>
        <w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Late result: 42</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body>
        <w:body><w:p><w:r><w:t>Final footnote</w:t></w:r></w:p></w:body></w:document>''')
    docs,issues=load_supplements(supp)
    assert not issues
    text=docs[0][1]
    assert text.index('Survey inclusion')<text.index('Late result: 42')<text.index('Final footnote')
    assert '<table>' in text and text.count('Late result: 42')==1


def test_docx_table_keeps_referenced_foot_and_endnotes_in_its_batch(tmp_path):
    supp = tmp_path / 'supplementary'; supp.mkdir()
    with ZipFile(supp / 'notes.docx', 'w') as z:
        ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        z.writestr('word/document.xml', f'''<w:document xmlns:w="{ns}"><w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>80.95</w:t><w:footnoteReference w:id="1"/><w:endnoteReference w:id="1"/></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>{'Later methods. ' * 80}</w:t></w:r></w:p></w:body></w:document>''')
        for kind, label in [('footnote', 'SD, not SE'), ('endnote', 'Adjusted estimate')]:
            z.writestr(f'word/{kind}s.xml', f'<w:{kind}s xmlns:w="{ns}"><w:{kind} w:id="1"><w:p><w:r><w:t>{label}</w:t></w:r></w:p></w:{kind}></w:{kind}s>')
    inputs = build_extraction_inputs(ref(tmp_path), 'Main', 'md', max_chars=300)
    table_batch = next(b for b in inputs.batches if '80.95' in b.text)
    assert '[footnote 1] SD, not SE' in table_batch.text
    assert '[endnote 1] Adjusted estimate' in table_batch.text
    assert not inputs.issues


def test_primary_only_still_reads_later_supplementary_tables(tmp_path):
    supp = tmp_path / 'supplementary'; supp.mkdir()
    (supp / 'S1.md').write_text('Appendix methods. ' * 20 + '\n\n<table><tr><td>Primary result</td></tr></table>')
    inputs = build_extraction_inputs(ref(tmp_path), 'Main paper', 'md', all_outcomes=False, max_chars=256)
    assert len(inputs.batches) == 3
    assert 'Primary result' in inputs.batches[-1].text
    assert not inputs.issues


def test_xlsx_merges_and_formula_cache_warnings(tmp_path):
    supp = tmp_path / "supplementary"; supp.mkdir()
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(supp / "outcomes.xlsx", "w") as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Outcomes" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', f'''<worksheet xmlns="{ns}"><sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is><t>Intervention</t></is></c></row>
        <row r="2"><c r="A2"><f>1+1</f><v>2</v></c><c r="B2"><f>A2+1</f></c></row></sheetData>
        <mergeCells><mergeCell ref="A1:B1"/></mergeCells></worksheet>''')
    docs, issues = load_supplements(supp)
    assert 'colspan="2"' in docs[0][1]
    assert '>2</td>' in docs[0][1]
    assert '[formula cache missing]' in docs[0][1]
    assert {i['code'] for i in issues} == {'xlsx_formula_cached_unverified', 'xlsx_formula_cache_missing'}


def test_missing_pdf_conversion_and_corrupt_supplement_are_explicit(tmp_path):
    supp = tmp_path / "supplementary"; supp.mkdir()
    (supp / 'outcomes.pdf').write_bytes(b'%PDF-not converted')
    (supp / 'bad.docx').write_bytes(b'not zip')
    (supp / 'import_provenance.json').write_text('{}')
    docs, issues = load_supplements(supp)
    assert docs == []
    assert {i['code'] for i in issues} == {'unconverted_pdf_supplement', 'supplement_read_failed'}
    (supp / 'outcomes.md').write_text('<table><tr><td>8</td></tr></table>')
    docs, issues = load_supplements(supp)
    assert len(docs) == 1
    assert not any(i['code'] == 'unconverted_pdf_supplement' for i in issues)


def record(name='Fatigue', mean=8, arm_id=1, timepoint=1):
    return {'paper_id': '10.1000/test', 'study_design': {'design_type': 'RCT', 'arms': [
        {'arm_id': arm_id, 'label': 'Therapy', 'intervention_name': 'Therapy', 'type': 'intervention'}]},
        'sample_sizes': {'n_randomized_total': 20}, 'outcomes': [{
            'outcome_id': 1, 'name': name, 'measurement_instrument': name, 'unit': 'points',
            'timepoints': [{'timepoint_id': timepoint, 'label': 'Week 4', 'weeks_from_randomization': 4}],
            'arm_results': [{'arm_id': arm_id, 'timepoint_id': timepoint, 'result_type': 'endpoint',
                             'n_analyzed': 20, 'mean': mean, 'sd': 1.2}], 'between_group_effects': []}]}


def test_merge_late_outcomes_remaps_ids_and_preserves_conflicts():
    base = record()
    late = record('Sleep', 4, arm_id='treatment', timepoint='end')
    merged = merge_extraction_records(base, late, 'supplementary/S1.docx')
    assert [o['name'] for o in merged['outcomes']] == ['Fatigue', 'Sleep']
    assert merged['outcomes'][1]['arm_results'][0]['arm_id'] == 1
    assert base == record()  # pure merge
    conflicting = record(mean=7, arm_id='treatment', timepoint='end')
    merged = merge_extraction_records(merged, conflicting, 'body.md part 2')
    assert merged['outcomes'][0]['arm_results'][0]['mean'] == 8
    assert merged['_merge_conflicts'][0]['incoming']['mean'] == 7
    findings = validate_record(merged)['issues']
    assert any(i['code'] == 'extraction_batch_conflict' and
               i['path'] == 'outcomes.0.arm_results.0' and i['severity'] == 'blocking' for i in findings)


def test_unresolved_merge_keeps_entire_candidate_outside_numeric_results():
    incoming = record()
    incoming['study_design']['arms'] = []
    merged = merge_extraction_records(record(), incoming, 'S1')
    assert merged['outcomes'] == record()['outcomes']
    assert merged['_unmerged_extractions'][0]['reason'] == 'unresolved_result_arm'


def test_conflicting_comparison_is_withheld_after_semantic_arm_remapping():
    base = record()
    base['synthesis_population'] = 'Adults with long COVID'
    base['study_design']['arms'].append({'arm_id': 2, 'label': 'Placebo', 'intervention_name': 'Placebo', 'type': 'placebo'})
    outcome = base['outcomes'][0]
    outcome['analysis_population'] = 'ITT'
    outcome['arm_results'] = []
    outcome['between_group_effects'] = [{'comparison': 'arm1_vs_arm2', 'intervention_arm_id': 1,
        'comparator_arm_id': 2, 'timepoint_id': 1, 'result_type': 'endpoint',
        'effect_measure': 'mean_difference', 'effect_value': -2, 'se': 0.4}]
    candidate = deepcopy(base)
    # A second extraction numbers arms in the opposite order.
    candidate['study_design']['arms'][0]['arm_id'] = 2
    candidate['study_design']['arms'][1]['arm_id'] = 1
    effect = candidate['outcomes'][0]['between_group_effects'][0]
    effect.update(intervention_arm_id=2, comparator_arm_id=1, comparison='arm2_vs_arm1', effect_value=-3)
    assert selected_comparisons(base)[0]['poolable']
    merged = merge_extraction_records(base, candidate, 'supplementary/S1.docx')
    assert merged['_merge_conflicts'][0]['incoming']['intervention_arm_id'] == 1
    assert merged['_merge_conflicts'][0]['incoming']['effect_value'] == -3
    assert not selected_comparisons(merged)[0]['poolable']


def test_duplicate_batch_timepoint_ids_cannot_reassign_results():
    candidate = record()
    candidate['outcomes'][0]['timepoints'].append({'timepoint_id': 1, 'label': 'Week 8', 'weeks_from_randomization': 8})
    merged = merge_extraction_records(record(), candidate, 'S1')
    assert merged['outcomes'] == record()['outcomes']
    assert merged['_unmerged_extractions'][0]['reason'] == 'duplicate_timepoint_id'


def test_new_outcome_preserves_distinct_same_day_measurements():
    incoming = record('Oxygen uptake')
    outcome = incoming['outcomes'][0]
    outcome['timepoints'] = [
        {'timepoint_id': 'before', 'label': 'First exercise test',
         'weeks_from_randomization': 0, 'elapsed_hours': 0},
        {'timepoint_id': 'after', 'label': 'Second exercise test',
         'weeks_from_randomization': 0, 'elapsed_hours': 50 / 60},
    ]
    outcome['arm_results'] = [dict(outcome['arm_results'][0], timepoint_id=tp, mean=value)
                              for tp, value in [('before', 1200), ('after', 1300)]]
    merged = merge_extraction_records(record(), incoming, 'Table3')
    assert not merged.get('_unmerged_extractions')
    added = merged['outcomes'][1]
    assert len(added['timepoints']) == 2
    assert [n['mean'] for n in added['arm_results']] == [1200, 1300]
    assert len({n['timepoint_id'] for n in added['arm_results']}) == 2


def test_new_rest_peak_visits_do_not_collide_with_each_other():
    incoming = record()
    outcome = incoming['outcomes'][0]
    outcome['timepoints'] = [
        {'timepoint_id': 'rest', 'label': 'Week8 rest', 'weeks_from_randomization': 8},
        {'timepoint_id': 'peak', 'label': 'Week8 peak', 'weeks_from_randomization': 8},
    ]
    outcome['arm_results'] = [dict(outcome['arm_results'][0], timepoint_id=tp, mean=value)
                              for tp, value in [('rest', 10), ('peak', 20)]]
    merged = merge_extraction_records(record(), incoming, 'Table3')
    assert not merged.get('_unmerged_extractions')
    assert len(merged['outcomes'][0]['timepoints']) == 3
    assert [n['mean'] for n in merged['outcomes'][0]['arm_results']] == [8, 10, 20]


def test_same_day_cross_source_identity_remains_ambiguous():
    base = record()
    base['outcomes'][0]['timepoints'] = [
        {'timepoint_id': 1, 'label': 'First test', 'weeks_from_randomization': 0}]
    incoming = deepcopy(base)
    incoming['outcomes'][0]['timepoints'][0]['label'] = 'Pre-dose assessment'
    merged = merge_extraction_records(base, incoming, 'Table3')
    assert merged['_unmerged_extractions'][0]['reason'] == 'ambiguous_timepoint_identity'
    assert merged['outcomes'] == base['outcomes']


def test_within_group_changes_remap_both_visits_and_retain_later_results():
    base = record()
    incoming = record(arm_id='active', timepoint='end')
    o = incoming['outcomes'][0]
    o['timepoints'].insert(0, {'timepoint_id': 'start', 'label': 'Baseline',
                             'weeks_from_randomization': 0})
    o['within_group_change'] = [{'arm_id': 'active', 'timepoint_from': 'start',
                                'timepoint_to': 'end', 'change_mean': -3, 'p_value': .02}]
    merged = merge_extraction_records(base, incoming, 'S1')
    result = merged['outcomes'][0]['within_group_change'][0]
    points = {t['label']: t['timepoint_id'] for t in merged['outcomes'][0]['timepoints']}
    assert result['arm_id'] == 1
    assert result['timepoint_from'] == points['Baseline']
    assert result['timepoint_to'] == points['Week 4']
    assert result['change_mean'] == -3
    assert result['source_file'] == 'S1'
    # Within-arm results never become between-arm treatment effects.
    assert merged['outcomes'][0]['between_group_effects'] == []
    conflicting = deepcopy(incoming)
    conflicting['outcomes'][0]['within_group_change'][0]['change_mean'] = -9
    revised = merge_extraction_records(merged, conflicting, 'S2')
    assert revised['outcomes'][0]['within_group_change'][0]['change_mean'] == -3
    assert revised['_merge_conflicts'][0]['path'] == 'outcomes.0.within_group_change.0'


def test_new_outcome_within_group_change_uses_remapped_arm_id():
    incoming = record('Sleep', arm_id='active', timepoint='after')
    o = incoming['outcomes'][0]
    o['timepoints'].insert(0, {'timepoint_id': 'before', 'label': 'Baseline',
                             'weeks_from_randomization': 0})
    o['within_group_change'] = [{'arm_id': 'active', 'timepoint_from': 'before',
                                'timepoint_to': 'after', 'change_mean': -2}]
    merged = merge_extraction_records(record(), incoming, 'S1')
    added = merged['outcomes'][1]
    assert added['within_group_change'][0]['arm_id'] == 1
    assert added['within_group_change'][0]['timepoint_from'] == added['timepoints'][0]['timepoint_id']
    assert added['within_group_change'][0]['timepoint_to'] == added['timepoints'][1]['timepoint_id']


@pytest.mark.parametrize('basis', ['change_from_baseline', None, 'unknown'])
def test_same_name_endpoint_and_change_are_separate_only_when_explicit(basis):
    base = record()
    base['outcomes'][0]['analysis_basis'] = 'endpoint'
    incoming = record(mean=-2)
    incoming['outcomes'][0]['analysis_basis'] = basis
    incoming['outcomes'][0]['arm_results'][0]['result_type'] = basis
    merged = merge_extraction_records(base, incoming, 'Table3')
    if basis == 'change_from_baseline':
        assert not merged.get('_unmerged_extractions')
        assert len(merged['outcomes']) == 2
        assert [o['arm_results'][0]['mean'] for o in merged['outcomes']] == [8, -2]
    else:
        assert merged['_unmerged_extractions'][0]['reason'] == 'ambiguous_outcome_identity'
        assert merged['outcomes'] == base['outcomes']


@pytest.mark.parametrize('change,reason', [
    ('timepoint_role', 'ambiguous_timepoint_identity'),
    ('outcome_unit', 'ambiguous_outcome_identity'),
])
def test_missing_identity_metadata_quarantines_and_blocks_existing_values(change, reason):
    base = record()
    base['outcomes'][0]['timepoints'][0]['role'] = 'endpoint'
    incoming = deepcopy(base)
    incoming['outcomes'][0]['arm_results'][0]['mean'] = 99
    if change == 'timepoint_role':
        del incoming['outcomes'][0]['timepoints'][0]['role']
    else:
        del incoming['outcomes'][0]['unit']
    merged = merge_extraction_records(base, incoming, 'S1')
    assert merged['outcomes'] == base['outcomes']
    assert merged['_unmerged_extractions'][0]['reason'] == reason
    assert any(i['path'] == 'outcomes.0.arm_results.0' and i['severity'] == 'blocking'
               for i in validate_record(merged)['issues'])


def test_numeric_timepoint_representation_cannot_hide_conflicting_estimate():
    incoming = record(mean=99)
    incoming['outcomes'][0]['timepoints'][0]['weeks_from_randomization'] = 4.0
    merged = merge_extraction_records(record(), incoming, 'S1')
    assert len(merged['outcomes'][0]['timepoints']) == 1
    assert merged['_merge_conflicts'][0]['incoming']['mean'] == 99


def test_missing_result_population_cannot_create_duplicate_measurement():
    base = record()
    base['outcomes'][0]['arm_results'][0]['analysis_population'] = 'ITT'
    merged = merge_extraction_records(base, record(mean=99), 'S1')
    assert len(merged['outcomes'][0]['arm_results']) == 1
    assert merged['_merge_conflicts'][0]['incoming']['mean'] == 99


def test_new_ids_are_collision_free_and_legacy_numeric_compatible():
    base = record(arm_id=2)
    incoming = record('Sleep', arm_id=1)
    incoming['study_design']['arms'][0].update(label='Other therapy', intervention_name='Other therapy')
    merged = merge_extraction_records(base, incoming, 'S1')
    ids = [a['arm_id'] for a in merged['study_design']['arms']]
    assert ids == [2, 1]
    assert merged['outcomes'][1]['arm_results'][0]['arm_id'] == 1


def test_added_fields_keep_actual_supplement_source():
    base = record()
    cell = base['outcomes'][0]['arm_results'][0]
    cell.update(sd=None, source_file='body.md')
    incoming = record()
    incoming['outcomes'][0]['arm_results'][0]['source_file'] = 'hallucinated.pdf'
    merged = merge_extraction_records(base, incoming, 'supplementary/S1.docx')
    cell = merged['outcomes'][0]['arm_results'][0]
    assert cell['sd'] == 1.2
    assert cell['_field_sources']['sd'] == 'supplementary/S1.docx'
    assert cell['_source_files'] == ['body.md', 'supplementary/S1.docx']
    new = merge_extraction_records(record('Sleep'), incoming, 'supplementary/S1.docx')
    assert new['outcomes'][1]['arm_results'][0]['source_file'] == 'supplementary/S1.docx'


@pytest.fixture
def p2():
    spec = importlib.util.spec_from_file_location('pass2_batches_test', 'steps/extract_pass2.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('all_outcomes', [False, True])
@pytest.mark.parametrize('fail_extra', [False, True])
def test_pass2_extracts_late_table_and_marks_partial_failure(tmp_path, monkeypatch, p2, fail_extra, all_outcomes):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable methods and study design. ' * 7 + '\n\n<table><tr><td>Sleep 4 (1.2)</td></tr></table>')
    seen = []
    def extract(prompt, *args, **kwargs):
        seen.append(prompt)
        if len(seen) == 2 and fail_extra:
            return p2._Extraction(None, None, '', 0, 1, 10, 0)
        return p2._Extraction(record('Fatigue' if len(seen) == 1 else 'Sleep'), None, '', 0, 1, 10, 10)
    monkeypatch.setattr(p2, '_run_extraction', extract)
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    result = p2.extract_one(paper, 1, 1, 'system', '{full_text} {paper_id}', 'rct',
                            all_outcomes=all_outcomes, retry_on_empty=False, input_max_chars=256)
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    assert result['_status'] == 'ok'
    assert len(seen) == 2
    assert '<table><tr><td>Sleep 4 (1.2)</td></tr></table>' in seen[1]
    if fail_extra:
        assert saved['_extraction_inputs']['status'] == 'partial'
        assert 'extraction_inputs_require_review' in saved['_warnings']
        assert len(saved['outcomes']) == 1
    else:
        assert [o['name'] for o in saved['outcomes']] == ['Fatigue', 'Sleep']
        assert saved['_extraction_inputs']['status'] == 'complete'


def test_partial_conversion_warning_survives_successful_extraction(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable outcome study with results. ' * 20)
    (tmp_path / 'provenance.json').write_text(json.dumps({'extraction_mode': 'docling-only', 'conversion_status': 'partial'}))
    monkeypatch.setattr(p2, '_run_extraction', lambda *a, **k: p2._Extraction(record(), None, '', 0, 1, 10, 10))
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, 'system', '{full_text}', 'rct')
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    assert saved['_conversion_quality']['requires_review']
    assert 'conversion_requires_review' in saved['_warnings']


def test_no_outcome_supplement_is_successful_abstention(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable outcome study with results. ' * 20)
    supp = tmp_path / 'supplementary'; supp.mkdir()
    (supp / 'methods.txt').write_text('Appendix. Laboratory instrument calibration; no patient results.')
    results = iter([record(), {'study_design': None, 'sample_sizes': None, 'outcomes': []}])
    monkeypatch.setattr(p2, '_run_extraction', lambda *a, **k: p2._Extraction(next(results), None, '', 0, 1, 10, 10))
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, 'system', '{full_text}', 'rct')
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    assert saved['_extraction_inputs']['status'] == 'complete'
    assert saved['_extraction_inputs']['batch_results'][-1]['status'] == 'no_outcomes'


def test_safety_only_batch_retains_metadata_and_retires_partial_summary(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable study methods and outcomes. ' * 20)
    supp = tmp_path / 'supplementary'; supp.mkdir()
    (supp / 'safety.txt').write_text('Safety appendix: two withdrawals due to headache.')
    first = record()
    first['summary'] = 'Outcome tables were not supplied.'
    first['summary_of'] = 'paper'
    late = {'study_design': first['study_design'], 'outcomes': [],
            'adverse_events': {'per_arm': [{'arm_id': 1, 'n_withdrawn_due_to_ae': 2}]},
            'extraction_metadata': {'extractor_notes': 'Withdrawals listed in the appendix.'}}
    results = iter([first, late])
    monkeypatch.setattr(p2, '_run_extraction', lambda *a, **k: p2._Extraction(next(results), None, '', 0, 1, 10, 10))
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, 'system', '{full_text}', 'rct')
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    fragments = saved['_batch_metadata']
    assert fragments[0]['record']['summary'] == first['summary']
    assert fragments[-1]['source'] == 'supplementary/safety.txt'
    assert fragments[-1]['record']['adverse_events'] == late['adverse_events']
    assert fragments[-1]['record']['extraction_metadata'] == late['extraction_metadata']
    assert saved['summary'] is None
    assert saved['_extraction_inputs']['requires_review']
    assert saved['_extraction_inputs']['batch_results'][-1]['status'] == 'no_outcomes'
    assert len(saved['outcomes']) == 1
    assert 'adverse_events' not in saved  # Not silently promoted without adjudication.


def test_unmergeable_batch_still_retains_all_metadata_without_mutating_inputs():
    base, late = record(), record()
    late['study_design']['arms'][0]['dose'] = 'different dose'
    late['baseline_demographics'] = {'per_arm': [{'arm_id': 1, 'age_mean': 42}]}
    original_base, original_late = deepcopy(base), deepcopy(late)
    merged = merge_extraction_records(base, late, 'Table 1')
    assert merged['_unmerged_extractions']
    assert merged['_batch_metadata'][-1]['record']['baseline_demographics'] == late['baseline_demographics']
    assert base == original_base and late == original_late


def test_conflicting_participant_flow_blocks_effects_without_mixing_denominators():
    base, late = record(), record()
    late['sample_sizes']['n_randomized_total'] = 30
    merged = merge_extraction_records(base, late, 'CONSORT figure')
    assert merged['sample_sizes']['n_randomized_total'] == 20
    assert merged['_batch_metadata'][-1]['record']['sample_sizes']['n_randomized_total'] == 30
    assert any(i['code'] == 'extraction_metadata_requires_review' and i['severity'] == 'blocking'
               and i['path'] == 'outcomes' for i in validate_record(merged)['issues'])


def test_metadata_arm_ids_are_not_treated_as_interchangeable():
    base, late = record(), record()
    base['sample_sizes']['per_arm'] = [{'arm_id': 1, 'n_completed': 8}]
    late['sample_sizes']['per_arm'] = [{'arm_id': 2, 'n_completed': 8}]
    merged = merge_extraction_records(base, late, 'Flow')
    assert any(p['section'] == 'sample_sizes' for p in merged['_metadata_review'])


def test_supplement_outcomes_are_tried_before_pdf_fallback(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable study. See supporting information for outcomes. ' * 10)
    supp = tmp_path / 'supplementary'; supp.mkdir()
    (supp / 'S1.md').write_text('<table><tr><td>Fatigue 8 (1.2)</td></tr></table>')
    empty = record(); empty['outcomes'] = []
    results = iter([empty, record()])
    monkeypatch.setattr(p2, '_run_extraction', lambda *a, **k: p2._Extraction(next(results), None, '', 0, 1, 10, 10))
    monkeypatch.setattr(p2, '_fallback_sources', lambda *a: pytest.fail('Supplement should supply the missing numbers'))
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, 'system', '{full_text}', 'rct')
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    assert saved['outcomes'][0]['name'] == 'Fatigue'


def test_xml_input_has_no_legacy_budget_drop_before_batching(tmp_path):
    from dataclasses import replace
    path = tmp_path / 'paper.xml'
    path.write_text('<article><body><p>' + 'Readable study methods. ' * 60 + '</p><table-wrap><label>Table 9</label><table><tr><td>Late outcome 8.2</td></tr></table></table-wrap></body></article>')
    paper = replace(ref(tmp_path), xml_path=path)
    text, source = read_best_text(paper, xml_first=True, max_chars=None)
    inputs = build_extraction_inputs(paper, text, source, all_outcomes=True, max_chars=512)
    assert any('Late outcome 8.2' in b.text for b in inputs.batches)
    assert not inputs.issues


def test_legacy_benchmark_control_reproduces_actual_partial_table(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    text = 'Readable study methods. ' * 10 + '\n\n<table><tr><td>8.2 (1.4)</td></tr></table>'
    paper.md_path.write_text(text)
    prompts = []
    def run(prompt, *args, **kwargs):
        prompts.append(prompt)
        return p2._Extraction(record(), None, '', 0, 1, 10, 10)
    monkeypatch.setattr(p2, '_run_extraction', run)
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, '', '{full_text}', 'rct', all_outcomes=True,
                   input_max_chars=256, input_packaging='legacy', retry_on_empty=False)
    assert prompts == [text[:256]]
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    assert saved['_extraction_inputs']['packaging'] == 'legacy'
    assert saved['_extraction_inputs']['issues'][0]['code'] == 'legacy_input_truncated'
def test_html_figure_keeps_table_caption_and_footer_in_one_batch():
    from ber_core.extraction_inputs import chunk_text
    figure = ('<figure><figcaption>Table 3: Follow-up</figcaption>'
              '<table><tr><td>76.26 [73.33–80.95]</td></tr></table>'
              '<footer>Median and IQR; denominator 20.</footer></figure>')
    chunks, issues = chunk_text("Prose " * 39 + "\n\n" + figure + "\n\n" + "Discussion " * 40, 256)
    assert not issues
    assert any(figure in chunk for chunk in chunks)


def test_followup_batch_receives_outcome_identity_without_previous_results(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable methods and study design. ' * 7 + '\n\n<table><tr><td>Fatigue 4 (1.2)</td></tr></table>')
    seen = []
    first = record()
    first['outcomes'][0]['synthesis_population'] = 'Adults with CFS'
    first['outcomes'][0]['analysis_population'] = 'complete cases'
    def extract(prompt, *args, **kwargs):
        seen.append(prompt)
        return p2._Extraction(deepcopy(first), None, '', 0, 1, 10, 10)
    monkeypatch.setattr(p2, '_run_extraction', extract)
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, 'system', '{full_text}', 'rct',
                   retry_on_empty=False, input_max_chars=256)
    context = json.loads(seen[1].split('Study context:\n', 1)[1])
    identity = context['outcomes'][0]
    assert identity['synthesis_population'] == 'Adults with CFS'
    assert identity['analysis_population'] == 'complete cases'
    assert identity['timepoints'] == first['outcomes'][0]['timepoints']
    assert 'arm_results' not in identity and 'between_group_effects' not in identity
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    assert len(saved['outcomes']) == 1
    assert '_unmerged_extractions' not in saved


def test_later_table_fills_abstained_uncertainty_without_losing_provenance():
    base = record(mean=None)
    cell = base['outcomes'][0]['arm_results'][0]
    cell.update(sd=None, uncertainty_type='unknown', dispersion_reported_as='unstated',
                result_source='not_reported', result_quote=None)
    incoming = record()
    incoming['outcomes'][0]['arm_results'][0].update(uncertainty_type='sd',
        dispersion_reported_as='sd', result_source='verbatim', result_quote='Fatigue 8 (1.2)')
    merged = merge_extraction_records(base, incoming, 'Table 2')
    assert '_merge_conflicts' not in merged
    result = merged['outcomes'][0]['arm_results'][0]
    assert (result['mean'], result['sd'], result['uncertainty_type']) == (8, 1.2, 'sd')
    assert result['result_source'] == 'verbatim'
    assert result['_field_sources']['uncertainty_type'] == 'Table 2'
    reversed_merge = merge_extraction_records(incoming, base, 'Methods')
    assert reversed_merge['outcomes'][0]['arm_results'][0]['uncertainty_type'] == 'sd'
    assert '_merge_conflicts' not in reversed_merge


@pytest.mark.parametrize('changed', ['uncertainty', 'sample_size'])
def test_later_table_still_blocks_actual_uncertainty_or_sample_size_conflicts(changed):
    base = record(); incoming = record()
    base['outcomes'][0]['arm_results'][0]['uncertainty_type'] = 'sd'
    cell = incoming['outcomes'][0]['arm_results'][0]
    cell['uncertainty_type'] = 'se_model' if changed == 'uncertainty' else 'sd'
    if changed == 'sample_size': cell['n_analyzed'] = 19
    merged = merge_extraction_records(base, incoming, 'Table 2')
    assert merged['_merge_conflicts']
    assert any(i['code'] == 'extraction_batch_conflict' for i in validate_record(merged)['issues'])


def test_markdown_html_table_quotes_match_rendered_cells_without_accepting_invention(tmp_path, monkeypatch, p2):
    paper = ref(tmp_path)
    paper.md_path.write_text('Readable results from the randomized trial. ' * 8 + '\n\n'
        '<table><tr><th>Fatigue score</th><td>8.0</td><td>1.2</td><td>20</td></tr></table>')
    extracted = record()
    cell = extracted['outcomes'][0]['arm_results'][0]
    cell['result_quote'] = 'Fatigue score 8.0 1.2 20'
    extracted['sample_sizes']['per_arm'] = [{'arm_id': 1, 'n_randomized': 20,
        'n_randomized_quote': 'Imaginary participants from a nonexistent multi-center trial'}]
    monkeypatch.setattr(p2, '_run_extraction', lambda *a, **k: p2._Extraction(extracted, None, '', 0, 1, 10, 10))
    monkeypatch.setattr(p2, '_OUTPUT_PATH', tmp_path / 'out.jsonl')
    p2.extract_one(paper, 1, 1, 'system', '{full_text}', 'rct', retry_on_empty=False)
    saved = json.loads((tmp_path / 'out.jsonl').read_text())
    checks = {d['path']: d['status'] for d in saved['_provenance']['details']}
    assert checks['outcomes.0.arm_results.0.result_quote'] == 'exact'
    assert checks['sample_sizes.per_arm.0.n_randomized_quote'] == 'absent'
