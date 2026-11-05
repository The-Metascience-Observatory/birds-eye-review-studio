"""Studio consumes the same selections and exposes historical-output status."""
import json
from app.schemas import MetaAnalysisOut
from app.services import feasibility, meta_bundle
from ber_core.scientific import RULES_VERSION, prepare_synthesis
from ber_core.meta_analysis import build_meta_analysis


def test_studio_preserves_selection_provenance_and_versions(tmp_path, monkeypatch):
    record={'paper_id':'x','_status':'ok','study_design':{'design_type':'RCT','arms':[
        {'arm_id':1,'type':'intervention','intervention_name':'NK'},
        {'arm_id':2,'type':'placebo','intervention_name':'Placebo'}]},
        'outcomes':[{'outcome_id':1,'name':'SBP','unit':'mmHg','synthesis_population':'hypertension',
        'analysis_basis':'endpoint','analysis_population':'ITT','is_primary':True,
        'timepoints':[{'timepoint_id':1,'label':'8 weeks','weeks_from_randomization':8}],
        'between_group_effects':[{'comparison':'arm1_vs_arm2','timepoint_id':1,'effect_measure':'mean_difference',
        'effect_value':-5,'se':2,'effect_quote':'source contrast','source_location':'Table 2'}]}]}
    (tmp_path/'trial_extractions_pass2.jsonl').write_text(json.dumps(record)+'\n')
    monkeypatch.setattr(feasibility,'_data_dir',lambda _:tmp_path)
    monkeypatch.setattr(meta_bundle.bundle_svc,'bundle_dir',lambda _:tmp_path)
    meta=build_meta_analysis([record],None)
    (tmp_path/'meta_analysis.json').write_text(json.dumps(meta))
    response=MetaAnalysisOut(**meta_bundle.read_meta(tmp_path))
    assert response.rules_version==RULES_VERSION and response.validation_status=='validated'
    assert response.groups[0]['trials'][0]['selection']=={k:v for k,v in prepare_synthesis([record])['selected'][0].items() if not k.startswith('_')}
    live=feasibility.build_feasibility(tmp_path)
    assert live['n_with_contrast']==1 and live['n_comparisons']==1
    assert live['withheld']==meta['withheld']
    meta.pop('rules_version')
    (tmp_path/'meta_analysis.json').write_text(json.dumps(meta))
    old=meta_bundle.read_meta(tmp_path)
    assert old['validation_status']=='requires_rebuild' and old['groups']
