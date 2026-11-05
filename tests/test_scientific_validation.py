"""Scientific failure cases: selection, source semantics, independence and statistics."""
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from ber_core.scientific import (selected_comparisons, validate_record,
    prepare_synthesis, feasibility, summarize_results, significance, select_timepoint)
from ber_core.prisma import derive_counts
from ber_core import meta_analysis as ma


def trial(pid='kim', name='Systolic blood pressure', value=-5.55, se=2.53):
    return {'paper_id':pid, '_status':'ok', 'synthesis_population':'untreated hypertension',
        'study_design': {'design_type':'RCT', 'arms':[
            {'arm_id':1, 'type':'intervention', 'intervention_name':'Nattokinase'},
            {'arm_id':2, 'type':'placebo', 'intervention_name':'Placebo'}]},
        'sample_sizes': {'n_randomized_total':86, 'per_arm':[
            {'arm_id':1, 'n_randomized':44}, {'arm_id':2, 'n_randomized':42}]},
        'outcomes':[{'outcome_id':1, 'name':name,'unit':'mmHg', 'is_primary':True,
            'symptom_domain':'cardiovascular', 'higher_is_better':False,
            'analysis_basis':'change_from_baseline','analysis_population':'completers',
            'timepoints':[{'timepoint_id':1,'label':'baseline','weeks_from_randomization':0},
                          {'timepoint_id':2,'label':'4 weeks','weeks_from_randomization':4},
                          {'timepoint_id':3,'label':'8 weeks','weeks_from_randomization':8}],
            'between_group_effects':[{'comparison':'arm1_vs_arm2','timepoint_id':2,
                'effect_measure':'mean_difference','effect_value':-2.67,'se':2.65},
                {'comparison':'arm1_vs_arm2','timepoint_id':3,'effect_measure':'mean_difference',
                 'effect_value':value,'se':se,'p_value':.029,'p_value_operator':'='}],
            'arm_results':[]}]}


def test_kim_uses_eight_week_result_without_earlier_p_value():
    s=selected_comparisons(trial())[0]
    assert s['timepoint_id']==3 and s['effect']==-5.55
    assert summarize_results(trial())['verdict']=='favors_treatment'


@pytest.mark.parametrize('path', ['outcomes', 'outcomes.0',
    'outcomes.0.between_group_effects',
    'outcomes.0.between_group_effects.1.effect_value'])
def test_source_audit_scope_blocks_estimate_and_benefit_claim(path):
    r = trial()
    assert selected_comparisons(r)[0]['poolable']
    r['_source_findings'] = [dict(code='source_table_attribution', path=path,
        severity='blocking', reason='Original table column attribution unresolved')]
    selected = selected_comparisons(r)[0]
    assert not selected['poolable']
    assert 'source_table_attribution' in {i['code'] for i in selected['issues']}
    assert summarize_results(r)['verdict'] == 'insufficient_data'


def test_source_audit_does_not_block_sibling_outcome_or_unused_timepoint():
    r = trial()
    r['outcomes'].append(deepcopy(r['outcomes'][0]))
    r['outcomes'][1]['outcome_id'] = 2
    r['_source_findings'] = [dict(code='source_unit_conflict', path='outcomes.0',
        severity='blocking', reason='Only the first outcome has conflicting units'),
        dict(code='earlier_assessment', path='outcomes.1.between_group_effects.0.se',
        severity='blocking', reason='Earlier unused assessment has ambiguous SE')]
    selected = selected_comparisons(r)
    assert not selected[0]['poolable']
    assert selected[1]['poolable'] and selected[1]['timepoint_id'] == 3


def test_source_audit_warning_remains_nonblocking():
    r = trial()
    r['_source_findings'] = [dict(code='limited_reporting', path='outcomes.0',
        severity='warning', reason='Secondary source detail unavailable')]
    assert selected_comparisons(r)[0]['poolable']
    assert summarize_results(r)['verdict'] == 'favors_treatment'


def test_source_audit_raw_sd_field_cannot_be_used_for_derived_contrast():
    r = trial(); o = r['outcomes'][0]
    o['between_group_effects'] = []; o['analysis_basis'] = 'endpoint'
    o['arm_results'] = [dict(arm_id=a, timepoint_id=3, n_analyzed=30,
        mean=mean, sd=1, mean_type='raw', uncertainty_type='sd', n_unit='participants',
        result_type='endpoint', analysis_population='completers')
        for a, mean in [(1, 10), (2, 12)]]
    assert selected_comparisons(r)[0]['poolable']
    r['_source_findings'] = [dict(code='source_uncertainty_label',
        path='outcomes.0.arm_results.0.sd', severity='blocking',
        reason='Original source does not identify dispersion as SD')]
    assert not selected_comparisons(r)[0]['poolable']
    # An independent, source-reported model estimate does not use the raw SD.
    o['between_group_effects'] = deepcopy(trial()['outcomes'][0]['between_group_effects'])
    assert selected_comparisons(r)[0]['poolable']


def test_source_audit_outcome_one_does_not_match_outcome_ten():
    r = trial()
    r['outcomes'] = [deepcopy(r['outcomes'][0]) for _ in range(11)]
    r['_source_findings'] = [dict(code='source_problem', path='outcomes.1',
        severity='blocking', reason='Only outcome at index 1 is affected')]
    selected = selected_comparisons(r)
    assert not selected[1]['poolable']
    assert selected[10]['poolable']


def test_explicit_primary_assessment_overrides_final():
    r=trial(); r['outcomes'][0]['primary_timepoint_id']=2
    assert selected_comparisons(r)[0]['effect']==-2.67


@pytest.mark.parametrize('op,p,expected',[('<',.05,'significant'),('≤',.05,'unknown'),('≤',.01,'significant'),
    ('=',.05,'not_detected'),('>',.05,'not_detected'),('≥',.05,'not_detected'),('<',.1,'unknown'),('>',.01,'unknown')])
def test_p_inequalities(op,p,expected):
    assert significance({'p_value':p,'p_value_operator':op})==expected


def test_ratio_ci_uses_one_and_conflicting_p_is_unresolved():
    e={'effect_measure':'risk_ratio','ci_95_low':.8,'ci_95_high':1.2}
    assert significance(e)=='not_detected'
    assert significance({**e,'p_value':.01,'p_value_operator':'='})=='conflicting'


def test_hodis_nonsignificant_primary_without_numeric_difference():
    r=trial('hodis'); e=r['outcomes'][0]['between_group_effects'][-1]
    e.update(effect_value=None,se=None,p_value=.31)
    assert summarize_results(r)['verdict']=='no_difference'
    assert not selected_comparisons(r)[0]['poolable']


def test_hodis_study_wide_counts_and_adjusted_se_never_become_sd():
    r=trial('hodis');o=r['outcomes'][0];o['between_group_effects']=[]
    o['arm_results']=[{'arm_id':a,'timepoint_id':3,'n_analyzed':234,'mean':120,'se':3,
        'mean_type':'adjusted','uncertainty_type':'se_model','n_unit':'participants',
        'result_type':'endpoint','analysis_population':'ITT'} for a in (1,2)]
    assert sum(i['code']=='arm_n_exceeds_enrolled' for i in validate_record(r)['issues'])==2
    assert not selected_comparisons(r)[0]['poolable']


def test_reported_p_and_raw_ci_conflict_is_not_displayed_as_significant():
    r = trial('four-arm-hdl'); o = r['outcomes'][0]
    o['analysis_basis'] = 'endpoint'
    o['between_group_effects'][-1].update(effect_value=None, se=None, p_value=.03)
    o['arm_results'] = [dict(arm_id=a, timepoint_id=3, n_analyzed=30,
        mean=mean, sd=1, mean_type='raw', uncertainty_type='sd', n_unit='participants',
        result_type='endpoint', analysis_population='completers') for a, mean in [(1, 1.09), (2, 1)]]
    selected = selected_comparisons(r)[0]
    assert selected['ci_low'] < 0 < selected['ci_high']
    assert selected['p_value'] == .03  # preserve the author's reported test
    assert selected['significance'] == 'conflicting'
    assert not selected['poolable']
    assert summarize_results(r)['verdict'] == 'insufficient_data'


@pytest.mark.parametrize('field,value,code',[('se',float('inf'),'invalid_number'),('se',-1,'invalid_uncertainty'),
 ('ci_95_high',-20,'invalid_interval'),('intervention_arm_id',999,'unresolved_comparison')])
def test_invalid_estimate_fields(field,value,code):
    r=trial();e=r['outcomes'][0]['between_group_effects'][-1]
    e.update(ci_95_low=-10.5,ci_95_high=-.57,intervention_arm_id=1,comparator_arm_id=2)
    e[field]=value
    assert code in {i['code'] for i in validate_record(r)['issues']}
    assert not selected_comparisons(r)[0]['poolable']


def test_events_require_valid_denominator_and_sampling_unit():
    r=trial();r['outcomes'][0]['arm_results']=[{'arm_id':1,'timepoint_id':3,'events':50,'total':40,'n_unit':'visits'}]
    assert {'invalid_events','wrong_sampling_unit'} <= {i['code'] for i in validate_record(r)['issues']}


def test_issue_preserves_field_source_and_version_without_certifying_columns():
    r=trial();r['_extraction_version']='fixture-v1';e=r['outcomes'][0]['between_group_effects'][-1]
    e.update(ci_95_low=1,ci_95_high=-1,source_location={'table':3,'row':'TC','column':'difference'})
    v=validate_record(r);i=v['issues'][0]
    assert i['path']=='outcomes.0.between_group_effects.1' and i['source_location']['table']==3
    assert i['extraction_version']=='fixture-v1' and not v['source_verified']


def test_same_day_is_not_baseline_and_numeric_timepoint_order_is_preserved():
    o={'timepoints':[{'timepoint_id':1,'label':'baseline','weeks_from_randomization':0},
        {'timepoint_id':9,'label':'4 hours','weeks_from_randomization':0},
        {'timepoint_id':10,'label':'8 hours','weeks_from_randomization':0}]}
    assert select_timepoint(o)['timepoint_id']==10


def test_crossover_and_cluster_require_design_correct_variance():
    for d in ['crossover','cluster_randomized']:
        r=trial();r['study_design']['design_type']=d
        assert not selected_comparisons(r)[0]['poolable']
        r['outcomes'][0]['between_group_effects'][-1]['design_adjusted']=True
        assert selected_comparisons(r)[0]['poolable']


def test_unknown_direction_and_partial_primary_data_do_not_imply_benefit():
    r=trial();r['outcomes'][0]['higher_is_better']=None
    assert summarize_results(r)['verdict']=='insufficient_data'
    r=trial();other=deepcopy(r['outcomes'][0]);other['between_group_effects']=[];r['outcomes'].append(other)
    assert summarize_results(r)['verdict']=='insufficient_data'


@pytest.mark.parametrize('names',[('Body weight','ALT'),('LDL cholesterol','Triglycerides'),
    ('Systolic blood pressure','Diastolic blood pressure')])
def test_broad_domain_is_never_a_pool_key(names):
    rs=[trial('a',names[0]),trial('b',names[1])]
    result=ma.build_meta_analysis(rs,None)
    assert len(result['groups'])==2 and all(not g.get('pooled') for g in result['groups'])


def test_endpoint_and_change_smds_are_separate_and_require_construct_mapping():
    rs=[trial('a'),trial('b')]
    for i,r in enumerate(rs):
        o=r['outcomes'][0];o['smd_construct']='blood_pressure';o['unit']='standard_deviation';o['standardization']='pooled_within_arm_sd';o['analysis_basis']='endpoint' if i else 'change_from_baseline'
        o['between_group_effects'][-1]['effect_measure']='smd'
    assert len(ma.build_meta_analysis(rs,None)['groups']) == 2
    assert all(not g.get('pooled') for g in ma.build_meta_analysis(rs,None)['groups'])
    del rs[0]['outcomes'][0]['smd_construct']
    assert not selected_comparisons(rs[0])[0]['poolable']


def test_four_arm_trial_retains_nk_and_combination_and_active_comparator():
    r=trial('liu2024');r['study_design']['arms'][0]['intervention_name']='Nattokinase + red yeast rice'
    r['study_design']['arms'] += [{'arm_id':3,'type':'intervention','intervention_name':'Nattokinase'},
        {'arm_id':4,'type':'active_comparator','intervention_name':'Red yeast rice'}]
    e=deepcopy(r['outcomes'][0]['between_group_effects'][-1]);e['comparison']='arm3_vs_arm2';r['outcomes'][0]['between_group_effects'].append(e)
    choices=selected_comparisons(r)
    assert {(s['intervention_arm_id'],s['comparator_arm_id']) for s in choices} == {(1,2),(1,4),(3,2),(3,4)}
    assert {s['intervention'] for s in choices} == {'Nattokinase','Nattokinase + red yeast rice'}
    from ber_core.dashboard_summary import project_row
    row=project_row(r,csv_verdicts={},domain_to_type={})
    assert 'Red yeast rice' not in row['facets']['interventionNames']
    assert 'Red yeast rice' in row['pico']['comparator']


def test_equivalent_multiple_arms_cannot_duplicate_controls_in_one_pool():
    r=trial();r['study_design']['arms'].append({'arm_id':3,'type':'intervention','intervention_name':'Nattokinase'})
    e=deepcopy(r['outcomes'][0]['between_group_effects'][-1]);e['comparison']='arm3_vs_arm2';r['outcomes'][0]['between_group_effects'].append(e)
    result=prepare_synthesis([r])
    assert not result['selected'] and all(s['issues'][-1]['code']=='shared_control' for s in result['withheld'])


def test_verified_companion_reports_count_once_without_selecting_smallest_se():
    a,b=trial('primary'),trial('companion',se=.001)
    for r in [a,b]:r['verified_study_id']='NCT00000001'
    b['outcomes'][0]['is_primary']=False
    result=prepare_synthesis([a,b]);assert len(result['selected'])==1
    assert result['selected'][0]['paper_id']=='primary'
    assert result['withheld'][0]['issues'][-1]['code']=='duplicate_study_contrast'


def test_cli_feasibility_and_engine_use_identical_selected_comparisons(tmp_path):
    rs=[trial('a'),trial('b')];prepared=prepare_synthesis(rs)
    spec=importlib.util.spec_from_file_location('meta_cli',Path(__file__).parents[1]/'scripts/pool_meta_analysis.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    result=ma.build_meta_analysis(rs,None)
    (tmp_path/'p2.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rs),encoding='utf-8')
    pooled=cli.pool_records(tmp_path,tmp_path/'p2.jsonl',tmp_path/'none.jsonl')
    groups=ma.build_meta_analysis(pooled,None)['groups']
    assert groups==result['groups']
    assert feasibility(rs)['n_pooled']==len(prepared['selected'])==2
    assert [t['selection']['paper_id'] for t in groups[0]['trials']]==[s['paper_id'] for s in prepared['selected']]


def test_independent_wald_and_hksj_reference_calculations():
    # Equal variances and effects: ordinary HKSJ collapses; its floored variant does not.
    pts=[(.5,.2),(.5,.2),(.5,.2)]
    wald=ma.pool(pts,ci_method='wald');h=ma.pool(pts,ci_method='hksj');mh=ma.pool(pts,ci_method='modified_hksj')
    assert wald['pooled']==pytest.approx(.5)
    assert wald['ci_low']==pytest.approx(.5-1.959963984540054*.2/math.sqrt(3))
    assert h['ci_low']==h['ci_high']==.5
    # Student-t(df=2) 97.5th percentile, independent reference value.
    assert mh['ci_low']==pytest.approx(.5-4.302652729911275*.2/math.sqrt(3))


def test_prisma_overlapping_legacy_exclusions_partial_and_manual_override(tmp_path):
    (tmp_path/'search_dois.csv').write_text('doi\na\nb\nc\nd\n')
    (tmp_path/'relevance.csv').write_text('doi,is_relevant\na,no\nb,yes\nc,yes\n')
    (tmp_path/'classifications.csv').write_text('doi,article_type\na,Editorial\nb,Clinical Trial\nc,Clinical Trial\n')
    (tmp_path/'user_inclusions.json').write_text('{"a":"source reviewed"}')
    (tmp_path/'user_exclusions.json').write_text('{"b":"manual"}')
    (tmp_path/'trial_extractions.jsonl').write_text(json.dumps({'paper_id':'c','screening':{'eligible_for_pass2':True}})+'\n')
    result=derive_counts(SimpleNamespace(data_dir=tmp_path))
    assert result['excluded_screening']==1 and result['screening_pending']==1
    assert result['conservation']=={'cohort':4,'assigned':4,'valid':True}
    assert result['dispositions']['extraction_pending']==['a','c']


def test_prisma_stale_extractions_are_reported_not_invented_into_funnel(tmp_path):
    (tmp_path/'search_dois.csv').write_text('doi\na\n')
    (tmp_path/'trial_extractions_pass2.jsonl').write_text(json.dumps(trial('orphan'))+'\n')
    result=derive_counts(SimpleNamespace(data_dir=tmp_path))
    assert result['included']==1 and result['included_in_flow']==0
    assert result['conservation']['valid']
    assert {'outside_search_cohort','extracted_without_confirmed_eligibility'} <= {i['code'] for i in result['issues']}


@pytest.mark.parametrize('article_type', ['Survey Study', 'Qualitative Study', 'Mixed Methods Study'])
def test_prisma_original_observational_types_reach_fulltext_review(tmp_path, article_type):
    (tmp_path/'search_dois.csv').write_text('doi\n10.1000/original\n10.1000/review\n')
    (tmp_path/'classifications.csv').write_text(
        'doi,is_relevant,article_type,is_excluded\n'
        f'10.1000/original,yes,{article_type},no\n'
        '10.1000/review,yes,Narrative Review,no\n')
    result = derive_counts(SimpleNamespace(data_dir=tmp_path))
    assert result['dispositions']['fulltext_pending'] == ['10.1000/original']
    assert result['excluded_screening'] == 1
    assert result['conservation']['valid']


def test_prisma_export_scope_keeps_routed_reports_out_of_pending_extraction(tmp_path):
    from ber_core.prisma import to_visualize_json
    (tmp_path/'search_dois.csv').write_text('doi\na\nb\nc\nd\n')
    (tmp_path/'relevance.csv').write_text('doi,is_relevant\na,yes\nb,yes\nc,yes\nd,yes\n')
    (tmp_path/'trial_extractions.jsonl').write_text(''.join(
        json.dumps({'paper_id':pid,'screening':{'eligible_for_pass2':True}})+'\n'
        for pid in ['a','b','c','d']))
    records=[trial(pid) for pid in ['a','b','c']]
    (tmp_path/'trial_extractions_pass2.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    project=SimpleNamespace(data_dir=tmp_path)
    unscoped=derive_counts(project)
    # b appears in a side feed; c is out of scope; d is genuinely unfinished.
    scoped=derive_counts(project,final_records=[records[0]])
    assert unscoped['included_reports']==3 and scoped['included_reports']==1
    assert scoped['included']==scoped['included_in_flow']==1
    assert scoped['dispositions']['outside_dashboard_scope']==['b','c']
    assert scoped['dispositions']['extraction_pending']==['d']
    assert scoped['duplicate_reports']==0
    assert scoped['conservation']=={'cohort':4,'assigned':4,'valid':True}
    flow=to_visualize_json(scoped)
    assert any(b['n']==2 and 'outside this dashboard' in b['label'] for b in flow['exclusions'])
    assert flow['main_flow'][-1]['n']==1


def test_prisma_export_scope_does_not_invent_eligibility_for_historical_feed(tmp_path):
    (tmp_path/'search_dois.csv').write_text('doi\na\n')
    record=trial('orphan')
    (tmp_path/'trial_extractions_pass2.jsonl').write_text(json.dumps(record)+'\n')
    result=derive_counts(SimpleNamespace(data_dir=tmp_path),final_records=[record])
    assert result['included_reports']==1 and result['included_in_flow']==0
    assert result['dashboard_scope']['reports_without_reconciled_eligibility']==['orphan']
    assert result['conservation']['valid']


def test_legacy_verdict_cannot_override_missing_primary_evidence():
    from ber_core.dashboard_summary import project_row
    r=trial();r['outcomes'][0]['between_group_effects']=[]
    row=project_row(r,csv_verdicts={'kim':'favors_treatment'},domain_to_type={})
    assert row['verdict']=='insufficient_data' and row['legacy_verdict']=='favors_treatment'


def test_missing_analysis_population_and_model_calculations_are_withheld():
    r=trial();del r['outcomes'][0]['analysis_population']
    assert 'unresolved_analysis_population' in {i['code'] for i in selected_comparisons(r)[0]['issues']}
    r=trial();r['outcomes'][0]['between_group_effects'][-1]['effect_source']='calculated'
    assert not selected_comparisons(r)[0]['poolable']


def test_smd_requires_same_explicit_standardization():
    rs=[trial('a'),trial('b')]
    for i,r in enumerate(rs):
        o=r['outcomes'][0];o.update(smd_construct='anxiety',unit='standard_deviation',standardization='baseline_sd' if i else 'pooled_within_arm_sd')
        o['between_group_effects'][-1]['effect_measure']='smd'
    gs=ma.build_meta_analysis(rs,None)['groups']
    assert len(gs)==2 and all(not g.get('pooled') for g in gs)


def test_feasibility_does_not_claim_a_single_arm_study_has_a_comparison():
    r=trial();r['study_design']['arms']=r['study_design']['arms'][:1]
    assert feasibility([r])['n_with_comparison']==0


def test_dashboard_counts_verified_companion_reports_as_one_study():
    from ber_core.dashboard_summary import project_row, aggregate, count_distinct_trials
    a,b=trial('a'),trial('b')
    for r in (a,b):r.update(verified_study_id='NCT01',is_rct=True)
    b['is_primary_report']=True
    rows=[project_row(r,csv_verdicts={},domain_to_type={}) for r in (a,b)]
    totals=aggregate(rows)['totals']
    assert totals['n_trials']==1 and totals['n_reports']==2
    assert totals['total_participants']==86
    assert count_distinct_trials(rows,'interventionNames')=={'nattokinase':1}


def test_prisma_duplicate_report_is_not_awaiting_extraction(tmp_path):
    (tmp_path/'search_dois.csv').write_text('doi\na\nb\n')
    (tmp_path/'classifications.csv').write_text('doi,is_relevant,article_type\na,yes,Clinical Trial\nb,yes,Clinical Trial\n')
    (tmp_path/'trial_extractions.jsonl').write_text(''.join(json.dumps({'paper_id':p,'screening':{'eligible_for_pass2':True}})+'\n' for p in ('a','b')))
    (tmp_path/'trial_extractions_pass2.jsonl').write_text(''.join(json.dumps(trial(p))+'\n' for p in ('a','b')))
    (tmp_path/'duplicate_dois.json').write_text('{"b":"a"}')
    result=derive_counts(SimpleNamespace(data_dir=tmp_path))
    assert result['included']==1 and result['duplicate_reports']==1
    assert result['extraction_pending']==0 and result['conservation']['valid']


def test_prisma_component_animal_gate_matches_staging_and_manual_override(tmp_path):
    (tmp_path/'search_dois.csv').write_text('doi\na\n')
    (tmp_path/'classifications.csv').write_text('doi,is_relevant,article_type\na,yes,Clinical Trial\n')
    (tmp_path/'trial_extractions.jsonl').write_text(json.dumps({'paper_id':'a','screening':{'eligible_for_pass2':True,'organism':{'verdict':'mouse'}}})+'\n')
    project=SimpleNamespace(data_dir=tmp_path,config={})
    assert derive_counts(project)['fulltext_excluded']==1
    (tmp_path/'user_inclusions.json').write_text('{"a":"reviewed exception"}')
    assert derive_counts(project)['fulltext_eligible']==1


def test_pool_refuses_invalid_numeric_inputs():
    for points in ([], [(1,0)], [(float('inf'),.2)], [(1,float('nan'))]):
        with pytest.raises(ValueError):ma.pool(points)


def test_unknown_and_decreasing_clinical_directions_stay_distinct():
    a,b=trial('a'),trial('b');b['outcomes'][0]['higher_is_better']=None
    groups=ma.build_meta_analysis([a,b],None)['groups']
    assert len(groups)==2 and all(not g.get('pooled') for g in groups)
    b['outcomes'][0]['higher_is_better']='unknown'
    assert summarize_results(b)['verdict']=='insufficient_data'


def test_feasibility_and_pooling_share_minimum_two_studies():
    assert feasibility([trial()],min_trials=1)['overall']=='not_possible'


@pytest.mark.parametrize('unit,quote,mean,blocked', [
    ('SF-36 points', 'Physical health maximum change 54% (46)', 54, True),
    ('SF-36 points', 'Maximum change −4,5% (12)', -4.5, True),
    ('percent change from baseline', 'Physical health maximum change 54% (46)', 54, False),
    ('%', 'Physical health maximum change 54% (46)', 54, False),
    ('mmHg', 'Mean 120 (10), 54% female', 120, False),
    ('mmHg', 'Mean 95, 95% CI 90 to 100', 95, False),
])
def test_percentage_means_cannot_be_pooled_as_raw_points(unit, quote, mean, blocked):
    r = trial(); o = r['outcomes'][0]
    o.update(unit=unit, analysis_basis='endpoint', between_group_effects=[])
    o['arm_results'] = [dict(arm_id=a, timepoint_id=3, n_analyzed=30,
        mean=mean, sd=10, mean_type='raw', uncertainty_type='sd', n_unit='participants',
        result_type='endpoint', analysis_population='completers', result_quote=quote) for a in (1, 2)]
    findings = validate_record(r)['issues']
    assert any(i['code'] == 'percentage_unit_conflict' for i in findings) == blocked
    assert selected_comparisons(r)[0]['poolable'] == (not blocked)
    assert o['arm_results'][0]['mean'] == mean  # preserve the extracted evidence
