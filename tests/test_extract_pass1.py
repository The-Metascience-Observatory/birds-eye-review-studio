"""Tests for pass-1 screening precision (extract_pass1.py + stage_eligible.py).

Two gates added after the nattokinase run, where pass 1 spent a full-text
Sonnet call on all 349 converted papers — including ~300 the abstract screen
had already ruled out — and the model's self-reported ``eligible_for_pass2``
went unchecked into the pass-2 bucket:

* The screen filter: papers with ``is_relevant=no`` / ``is_excluded=yes`` are
  skipped before the LLM call (force-includes and unclassified papers kept).
* ``_enforce_eligibility`` / ``_component_ineligible``: eligibility flips off
  when an explicit component verdict contradicts it — the self-report is no
  longer load-bearing.
"""
from __future__ import annotations

import importlib.util

import pytest


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def p1():
    return _load("steps/extract_pass1.py", "extract_pass1")


@pytest.fixture(scope="module")
def se():
    return _load("ber_core/stage_eligible.py", "stage_eligible_mod")


# --- the screen filter ------------------------------------------------------

CSV_HEADER = ("doi,is_relevant,relevance_confidence,studies_treatment,"
              "article_type,type_confidence,is_excluded,exclusion_reason,"
              "certainty,organism\n")


def _write_classifications(data_dir, rows):
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = [CSV_HEADER]
    for doi, relevant, excluded in rows:
        lines.append(f"{doi},{relevant},high,yes,Clinical Trial,high,"
                     f"{excluded},,high,human\n")
    (data_dir / "classifications.csv").write_text("".join(lines),
                                                  encoding="utf-8")


def test_screened_out_ids_matrix(p1, tmp_path):
    _write_classifications(tmp_path, [
        ("10.1/relevant", "yes", "no"),
        ("10.1/offtopic", "no", "no"),
        ("10.1/review", "yes", "yes"),
        ("10.1/unknown", "unknown", ""),
        ("10.1/blank", "", ""),
    ])
    out = p1._screened_out_ids(tmp_path)
    assert "10.1/offtopic" in out
    assert "10.1/review" in out
    # The screen didn't RULE on these — the full-text pass should look.
    assert "10.1/relevant" not in out
    assert "10.1/unknown" not in out
    assert "10.1/blank" not in out


def test_no_classifications_file_fails_open(p1, tmp_path):
    assert p1._screened_out_ids(tmp_path / "nope") == set()


# --- _enforce_eligibility ---------------------------------------------------

FLAGS = {"study_design_and_control_arm": True, "organism": True,
         "relevance_recheck": True, "include_animal_studies": False}


def _eligible_record(**screening_overrides):
    screening = {
        "is_protocol_not_article": {"verdict": False},
        "is_clinical_trial": {"verdict": "yes", "study_design": "RCT",
                              "has_control_arm": True},
        "organism": {"verdict": "human"},
        "is_relevant": {"verdict": "yes"},
        "eligible_for_pass2": True,
    }
    screening.update(screening_overrides)
    return {"paper_id": "10.1/x", "screening": screening}


def test_consistent_record_is_untouched(p1):
    rec = _eligible_record()
    p1._enforce_eligibility(rec, FLAGS)
    assert rec["screening"]["eligible_for_pass2"] is True
    assert "_warnings" not in rec


@pytest.mark.parametrize("override, reason", [
    ({"organism": {"verdict": "mouse"}}, "organism_mouse"),
    ({"is_clinical_trial": {"verdict": "no", "has_control_arm": True}},
     "not_a_trial"),
    ({"is_clinical_trial": {"verdict": "yes", "has_control_arm": False}},
     "no_control_arm"),
    ({"is_protocol_not_article": {"verdict": True}}, "protocol"),
    ({"is_relevant": {"verdict": "no"}}, "not_relevant"),
])
def test_explicit_contradiction_flips_the_gate(p1, override, reason):
    rec = _eligible_record(**override)
    p1._enforce_eligibility(rec, FLAGS)
    assert rec["screening"]["eligible_for_pass2"] is False
    assert any(reason in w for w in rec["_warnings"])


def test_unclear_verdicts_stay_with_the_model(p1):
    """Over-strictness would silently shrink the eligible set — only explicit
    negatives flip the gate."""
    rec = _eligible_record(
        is_clinical_trial={"verdict": "unclear", "has_control_arm": None},
        organism={"verdict": None})
    p1._enforce_eligibility(rec, FLAGS)
    assert rec["screening"]["eligible_for_pass2"] is True


def test_animals_allowed_relaxes_the_organism_gate(p1):
    rec = _eligible_record(organism={"verdict": "mouse"})
    p1._enforce_eligibility(rec, {**FLAGS, "include_animal_studies": True})
    assert rec["screening"]["eligible_for_pass2"] is True


def test_ineligible_records_are_left_alone(p1):
    rec = _eligible_record(organism={"verdict": "mouse"})
    rec["screening"]["eligible_for_pass2"] = False
    p1._enforce_eligibility(rec, FLAGS)
    assert "_warnings" not in rec


# --- stage_eligible's mirror check ------------------------------------------

def test_component_mismatch_detected_at_staging(se):
    screening = _eligible_record(organism={"verdict": "mouse"})["screening"]
    assert se._component_ineligible(screening, include_animals=False) == \
        "organism_mouse"
    assert se._component_ineligible(screening, include_animals=True) is None


def test_consistent_screening_stages_clean(se):
    screening = _eligible_record()["screening"]
    assert se._component_ineligible(screening, include_animals=False) is None


@pytest.mark.parametrize('include_observational', [False, True])
def test_news_repeating_trial_results_is_not_an_original_trial(p1, se, include_observational):
    rec = _eligible_record(reports_original_data={
        'verdict': False, 'report_type': 'news',
        'reasoning': 'Reports results from a separately cited randomized trial.'})
    # The other verdicts mimic the observed error: human, relevant, randomized,
    # controlled, and self-labelled eligible. Both gates must reject it.
    assert se._component_ineligible(rec['screening'], False, include_observational) == 'not_original_report'
    p1._enforce_eligibility(rec, {**FLAGS, 'include_observational': include_observational})
    assert rec['screening']['eligible_for_pass2'] is False
    assert 'gate_override_not_original_report' in rec['_warnings']


@pytest.mark.parametrize('originality', [None, {'verdict': None},
                                       {'verdict': True, 'report_type': 'secondary_analysis'}])
def test_originality_gate_preserves_legacy_unknown_and_secondary_reports(p1, se, originality):
    rec = _eligible_record(reports_original_data=originality)
    p1._enforce_eligibility(rec, FLAGS)
    assert rec['screening']['eligible_for_pass2'] is True
    assert se._component_ineligible(rec['screening'], False) is None


def test_originality_question_is_present_with_optional_fields_disabled(p1):
    prompt = p1.build_pass1_prompt('{json_schema}\n{rules}', 'ME/CFS', {})
    assert '"reports_original_data"' in prompt
    assert 'reports_original_data = true' in prompt
    assert 'THIS document' in prompt and 'secondary analysis' in prompt
    assert 'news stories' in prompt


@pytest.mark.parametrize("setting,expected", [(None, True), (True, True), (False, False)])
def test_staging_observational_default_matches_runner(se, tmp_path, monkeypatch, setting, expected):
    import json
    project = tmp_path / "review"
    data = project / "data"
    data.mkdir(parents=True)
    paper = project / "papers" / "Cohort" / "10.1--x"
    paper.mkdir(parents=True)
    (paper / "body.md").write_text("Full text", encoding="utf-8")
    rec = _eligible_record(is_clinical_trial={"verdict": "no",
        "study_design": "observational", "has_control_arm": False})
    (data / "trial_extractions.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    cfg = {} if setting is None else {"include_observational": setting}
    monkeypatch.setattr(se, "load_config", lambda _: cfg)
    monkeypatch.setattr(se, "project_paths", lambda _: {"project_dir": project, "data_dir": data})
    monkeypatch.setattr(se.paper_store, "build_index", lambda *a, **k: {"10.1/x": paper})
    monkeypatch.setattr(se.sys, "argv", ["stage_eligible.py", "--config", "unused", "--execute"])
    se.main()
    assert (project / "eligible_observational" / paper.name).is_symlink() == expected


# --- _prune_stale_records (P4: stale append-only resume) --------------------
#
# The pass-1 JSONL is append-only and resume skips already-decided papers, so
# without pruning a re-screen never invalidates an old eligible_for_pass2=true
# and stage_eligible faithfully re-stages the paper forever.

def _jsonl(path, records):
    import json
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")


def test_prune_drops_records_the_screen_now_rules_out(p1, tmp_path):
    import json
    out = tmp_path / "trial_extractions.jsonl"
    _jsonl(out, [
        {"paper_id": "10.1/keep", "_status": "ok",
         "screening": {"eligible_for_pass2": True}},
        {"paper_id": "10.1/stale", "_status": "ok",
         "screening": {"eligible_for_pass2": True}},
    ])
    dropped = p1._prune_stale_records(out, {"10.1/stale"}, set())
    assert dropped == 1
    remaining = [json.loads(l)["paper_id"]
                 for l in out.read_text().splitlines() if l.strip()]
    assert remaining == ["10.1/keep"]


def test_prune_spares_force_included_papers(p1, tmp_path):
    out = tmp_path / "trial_extractions.jsonl"
    _jsonl(out, [{"paper_id": "10.1/forced", "_status": "ok"}])
    dropped = p1._prune_stale_records(out, {"10.1/forced"}, {"10.1/forced"})
    assert dropped == 0
    assert "10.1/forced" in out.read_text()


def test_prune_is_a_noop_without_contradictions(p1, tmp_path):
    out = tmp_path / "trial_extractions.jsonl"
    _jsonl(out, [{"paper_id": "10.1/keep", "_status": "ok"}])
    before = out.read_text()
    assert p1._prune_stale_records(out, {"10.1/other"}, set()) == 0
    assert out.read_text() == before


def test_prune_missing_file_is_safe(p1, tmp_path):
    assert p1._prune_stale_records(tmp_path / "nope.jsonl",
                                   {"10.1/x"}, set()) == 0


def test_prune_keeps_undecodable_lines_verbatim(p1, tmp_path):
    out = tmp_path / "trial_extractions.jsonl"
    out.write_text('not json at all\n'
                   '{"paper_id": "10.1/stale", "_status": "ok"}\n',
                   encoding="utf-8")
    dropped = p1._prune_stale_records(out, {"10.1/stale"}, set())
    assert dropped == 1
    assert out.read_text() == "not json at all\n"


@pytest.mark.parametrize("design,trial,control,original,relevant,expected", [
    ("observational", "no", False, True, "yes", True),
    ("observational", "no", False, False, "yes", False),
    ("observational", "no", False, True, "no", False),
    ("single-arm", "yes", False, True, "yes", False),
    ("RCT", "yes", True, True, "yes", True),
])
def test_descriptive_cohort_gate_preserves_trial_and_originality_rules(
        p1, se, design, trial, control, original, relevant, expected):
    # The pediatric post-COVID service cohort has original patient findings but
    # no comparator. The same exemption must not admit a research-assigned
    # single-arm trial, a news summary, or an off-topic cohort.
    rec = _eligible_record(
        reports_original_data={"verdict": original},
        is_clinical_trial={"verdict": trial, "study_design": design,
                           "has_control_arm": control},
        is_relevant={"verdict": relevant})
    p1._enforce_eligibility(rec, {**FLAGS, "include_observational": True})
    assert rec["screening"]["eligible_for_pass2"] is expected
    assert (se._component_ineligible(rec["screening"], False, True) is None) is expected


def test_review_scope_survives_per_paper_formatting(p1):
    topic = {"description": "Persistent symptoms {after infection}",
             "synonyms": ["post-acute sequelae"],
             "not_about": ["acute illness without persistent sequelae"],
             "indication_focus": "treatment"}
    template = p1.build_pass1_prompt(
        '{full_text}\n{json_schema}\n{rules}\n{format_note}', 'Long COVID',
        {**FLAGS, "include_observational": True}, topic)
    rendered = template.format(full_text='Source {verbatim}', paper_id='10.1/x',
                               format_note='Markdown report')
    assert 'Persistent symptoms {after infection}' in rendered
    assert 'post-acute sequelae' in rendered
    assert 'acute illness without persistent sequelae' in rendered
    assert 'literal condition name is not required' in rendered
    assert 'Source {verbatim}' in rendered
    # Treatment-feed routing is a later decision, not a relevance requirement.
    assert 'indication_focus' not in rendered
