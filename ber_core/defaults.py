"""Shared pipeline/Studio defaults; callers copy before applying overrides."""
ARTICLE_TYPES = ('Original Research',
 'Clinical Trial',
 'Systematic Review',
 'Scoping Review',
 'Meta-Analysis',
 'Narrative Review',
 'Case Report',
 'Case Series',
 'Editorial/Commentary/Opinion',
 'Prospective Cohort Study',
 'Retrospective Cohort Study',
 'Other Observational Study',
 'Case-Control Study',
 'Cross-Sectional Study',
 'Qualitative Study',
 'Mixed Methods Study',
 'Guidelines/Consensus Statement',
 'Study Protocol',
 'Survey Study',
 'Erratum',
 'Preprint/Other',
 'no_abstract')

DEFAULT_EXTRACTION_WORKERS = 10

PASS1_FIELD_DEFAULTS = {'study_design_and_control_arm': True,
 'organism': True,
 'relevance_recheck': True,
 'authors_affiliations': False,
 'funding': False,
 'coi': False,
 'trial_registry': False}

FULLTEXT_DEFAULTS = {'structured': True,
 'supplementary': False,
 'refresh_supplementary': False,
 'data_artifacts': False,
 'max_supplementary_mb': 300,
 'xml_first': True,
 'retry_on_empty': True}

PASS2_FIELD_DEFAULTS = {'baseline_demographics': True,
 'adverse_events': True,
 'follow_up': True,
 'study_design_methodology': True,
 'all_outcomes': False,
 'per_arm_sample_detail': False,
 'subgroup_analyses': False,
 'provenance_quotes': False}
