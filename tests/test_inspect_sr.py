"""Scientific identity, provenance and failure-mode regressions for INSPECT-SR."""
import csv
import json
from pathlib import Path

import pytest

from ber_core.inspect_corpus import collect_corpus, dashboard_records
from ber_core.inspect_sources import RetractionIndex, pubpeer_lookup, write_json
from ber_core.inspect_sr import CHECKS, adjudicate, validate_assessment
from ber_core.inspect_stats import integer_moments, pooled_t_p_range
from ber_core.inspect_report import synthesis_sets
from ber_core.projects import Project
from ber_core.pipeline import canonical_steps


def csv_write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def jsonl_write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows))


def fixture_assessment(pid="10.1000/a"):
    return {"paper_id": pid, "eligibility": "randomized_trial", "eligibility_reason": "Randomized trial",
            "eligibility_evidence": [{"source": "body", "quote": "randomized trial"}],
            "study_registration_ids": [], "population": "adults", "intervention": "A", "comparator": "B",
            "intervention_category": "drug", "findings": [],
            "checks": [{"id": k, "answer": "Unclear", "reason": "Insufficient evidence", "evidence": []} for k in CHECKS],
            "domains": [{"id": str(k), "judgment": "not assessed", "reason": "Insufficient evidence"} for k in range(1,5)],
            "overall": {"judgment": "not assessed", "reason": "Insufficient evidence"},
            "risk_of_bias": {"assessment": "Not assessed", "evidence": []}, "certainty_limitations": "No assessment", "unresolved": []}


def test_all_21_checks_and_four_domains_required():
    packet = {"paper_id": "10.1000/a", "sources": {"body": "This randomized trial evaluated a treatment."}}
    a = fixture_assessment()
    assert validate_assessment(a, packet) is a
    a["checks"].pop()
    with pytest.raises(ValueError, match="every check"):
        validate_assessment(a, packet)


def test_unverifiable_quote_and_wrong_identifier_rejected():
    packet = {"paper_id": "10.1000/a", "sources": {"body": "randomized trial"}}
    a = fixture_assessment()
    a["eligibility_evidence"][0]["quote"] = "invented evidence"
    with pytest.raises(ValueError, match="unverifiable"):
        validate_assessment(a, packet)
    a = fixture_assessment("10.1000/b")
    with pytest.raises(ValueError, match="identifier"):
        validate_assessment(a, packet)


def test_numbers_must_come_from_the_associated_quotes():
    packet = {"paper_id": "10.1000/a", "sources": {"body": "randomized trial with 20 participants", "other": "500 people"}}
    a = fixture_assessment()
    a["findings"] = [{"finding": "500 participants were randomized", "evidence": [{"source": "body", "quote": "20 participants"}]}]
    with pytest.raises(ValueError, match="numbers absent"):
        validate_assessment(a, packet)


def test_retry_feedback_reports_figure_failure_hidden_by_bad_finding():
    from ber_core.inspect_sr import assessment_errors, withhold_unverified_findings
    packet = {"paper_id": "10.1000/a", "sources": {"body": "randomized trial with 20 participants"},
              "visual_inspection": {"status": "partial_main_pdf"}}
    a = fixture_assessment()
    a["findings"] = [{"finding": "500 participants were randomized",
                      "evidence": [{"source": "body", "quote": "20 participants"}]}]
    figure_check = next(c for c in a["checks"] if c["id"] == "3.2")
    figure_check.update(answer="No", evidence=[{"source": "body", "quote": "randomized trial"}])
    original = json.dumps(a, sort_keys=True)
    errors = assessment_errors(a, packet)
    assert any("numbers absent" in e for e in errors)
    assert any("Figure clearance" in e for e in errors)
    assert json.dumps(a, sort_keys=True) == original
    assert withhold_unverified_findings(a, packet) is None
    with pytest.raises(ValueError, match="numbers absent"):
        validate_assessment(a, packet)


@pytest.mark.parametrize("check_id", ["3.2", "2.3"])
def test_text_cannot_certify_figures_or_unread_registration_history(check_id):
    packet = {"paper_id": "10.1000/a", "sources": {"body": "randomized trial"}}
    a = fixture_assessment()
    c = next(c for c in a["checks"] if c["id"] == check_id)
    c.update(answer="No", evidence=[{"source": "body", "quote": "randomized trial"}])
    with pytest.raises(ValueError):
        validate_assessment(a, packet)


def test_pubpeer_network_failure_is_not_no_comments(tmp_path, monkeypatch):
    import ber_core.inspect_sources as source
    monkeypatch.delenv("PUBPEER_DEVKEY", raising=False)
    assert pubpeer_lookup(["10.1000/a"], tmp_path)["10.1000/a"]["status"] == "unavailable"
    monkeypatch.setenv("PUBPEER_DEVKEY", "test-key")
    monkeypatch.setattr(source, "fetch_json", lambda *a, **kw: {"status": "unavailable", "retrieved_at": "today"})
    assert pubpeer_lookup(["10.1000/a"], tmp_path)["10.1000/a"]["status"] == "unavailable"
    monkeypatch.setattr(source, "fetch_json", lambda *a, **kw: {"status": "ok", "retrieved_at": "today", "data": {"status": "bad", "feedbacks": []}})
    assert pubpeer_lookup(["10.1000/a"], tmp_path)["10.1000/a"]["status"] == "unavailable"
    monkeypatch.setattr(source, "fetch_json", lambda *a, **kw: {"status": "ok", "retrieved_at": "today", "data": {"status": "good", "feedbacks": [{"id": "10.1000/A", "total_comments": 2}]}})
    assert len(pubpeer_lookup(["10.1000/a"], tmp_path)["10.1000/a"]["feedbacks"]) == 1


def test_retraction_matches_original_not_notice_and_names_are_candidates(tmp_path):
    p = tmp_path / "rw.csv"
    csv_write(p, [{"Record ID": "1", "OriginalPaperDOI": "10.1000/A;10.1000/B", "RetractionDOI": "10.1000/notice",
                   "RetractionNature": "Retraction", "Title": "Title", "Author": "Jane Smith"}])
    index = RetractionIndex(p)
    assert len(index.lookup("https://doi.org/10.1000/A")["doi_matches"]) == 1
    assert not index.lookup("10.1000/notice")["doi_matches"]
    result = index.lookup("10.1000/c", authors=["Smith Jane"])
    assert len(result["author_candidates"]) == 1 and not result["doi_matches"]
    assert not index.lookup("10.1000/c", authors=["J Smith"])["author_candidates"]


def test_reconciles_every_dashboard_report_and_preserves_exclusions(tmp_path):
    p = Project("topic", tmp_path / "config.yaml", {"topic": {"name": "Topic"}}, tmp_path / "project")
    csv_write(p.data_dir / "trial_screening.csv", [{"doi": "10.1000/a", "is_topic": "yes", "is_excluded": "no", "studies_treatment": "yes", "trial_type": "rct"}])
    jsonl_write(p.data_dir / "trial_extractions.jsonl", [{"paper_id": "10.1000/b", "screening": {"eligible_for_pass2": False}}])
    write_json(p.data_dir / "user_exclusions.json", {"10.1000/c": "User decision"})
    dash = tmp_path / "dashboard"
    jsonl_write(dash / "trial_extractions.jsonl", [{"paper_id": pid, "study_design": {"design_type": "RCT"}} for pid in ("10.1000/b", "10.1000/c")])
    before = (p.data_dir / "trial_extractions.jsonl").read_bytes()
    records, manifest = collect_corpus(p, tmp_path / "output", dash)
    by = {r["paper_id"]:r for r in records}
    assert set(by) == {"10.1000/a", "10.1000/b", "10.1000/c"}
    assert by["10.1000/b"]["selection_reason"] == "fulltext_ineligible"
    assert by["10.1000/c"]["selection_reason"] == "manual_exclusion"
    assert by["10.1000/b"]["dashboard_conflict"]
    assert manifest["dashboard_missing_from_audit"] == []
    assert (p.data_dir / "trial_extractions.jsonl").read_bytes() == before


def test_dashboard_filter_matches_page_loader(tmp_path):
    jsonl_write(tmp_path / "trial_extractions.jsonl", [
        {"paper_id": "10.1000/a#arm2", "study_design": {"design_type": "RCT"}},
        {"paper_id": "10.1000/b", "study_design": {"design_type": "not a clinical trial"}},
        {"paper_id": "10.1000/c"},
        {"paper_id": "10.1000/d", "study_design": {"design_type": "before_after"}}])
    csv_write(tmp_path / "trial_screening.csv", [{"doi": "10.1000/a", "is_excluded": "yes"}])
    assert [r["paper_id"] for r in dashboard_records(tmp_path)] == ["10.1000/d"]


def test_grim_never_assumes_age_is_integer_and_preserves_rounding():
    assert integer_moments("8.96", 30)["status"] == "not_assessable"
    assert integer_moments("8.96", 30, integer_measure_confirmed=True)["status"] == "inconsistent"
    assert integer_moments("9.96", 30, integer_measure_confirmed=True)["status"] == "inconsistent"
    assert integer_moments("9.93", 30, sd="0.18", integer_measure_confirmed=True)["status"] == "inconsistent"
    assert integer_moments("2.00", 3, sd="1.00", integer_measure_confirmed=True)["status"] == "compatible_necessary_conditions"
    # INSPECT-SR example: p=.02 is compatible despite rounded-data point p=.23.
    bounds = pooled_t_p_range("20", "4", 30, "21", "2", 30)
    assert bounds["p_min"] < .02 < bounds["p_max"]


def test_disagreement_not_automatically_resolved_and_serious_set_aside():
    a, b = fixture_assessment(), fixture_assessment()
    a["overall"] = {"judgment": "no concerns", "reason": "A"}
    b["overall"] = {"judgment": "serious concerns", "reason": "B"}
    r = {"paper_id": a["paper_id"], "title": "Trial", "in_dashboard": True, "selection_status": "candidate"}
    out = adjudicate([r], {a["paper_id"]: {"reviewer_1": {"assessment": a}, "reviewer_2": {"assessment": b}}})
    assert out[0]["status"] == "awaiting_human_review"
    assert "overall" in out[0]["disagreements"]
    assert out[0]["synthesis_status"] == "serious_concerns_awaiting_resolution"
    assert not synthesis_sets(out)["all_provisional_reports"]


def test_new_pipeline_tail_is_opt_in(tmp_path):
    p = Project("topic", tmp_path / "config.yaml", {}, tmp_path / "project")
    assert "17_systematic_review" not in [s.id for s in canonical_steps(p)]
    p.config["systematic_review"] = {"enabled": True}
    tail = canonical_steps(p)[-1]
    assert tail.id == "17_systematic_review" and tail.num == 19
    assert tail.produces == [str(p.project_dir / "systematic_review/systematic_review.md")]


def test_public_pubpeer_comment_content_is_cached(tmp_path, monkeypatch):
    import html
    import ber_core.inspect_sources as source
    comments = [{"id": 1, "html": "A question about the reported denominator."}]
    page = '<comment-timeline :data-comments="' + html.escape(json.dumps(comments), quote=True) + '"></comment-timeline>'
    class Response:
        text = page
        content = page.encode()
        def raise_for_status(self):
            pass
    monkeypatch.setattr(source.requests, "get", lambda *a, **kw: Response())
    feedback = {"url": "https://pubpeer.com/publications/AB12"}
    result = source.pubpeer_discussion(feedback, tmp_path)
    assert result["status"] == "ok" and result["comments"] == comments
    monkeypatch.setattr(source.requests, "get", lambda *a, **kw: pytest.fail("cache was not used"))
    assert source.pubpeer_discussion(feedback, tmp_path, offline=True) == result
    assert source.pubpeer_discussion({"url": "https://example.com/AB12"}, tmp_path)["status"] == "unavailable"


def test_partial_batch_success_is_saved_and_only_bad_report_retried(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ber_core.inspect_sr as sr
    records = [{"paper_id": pid, "title": "Trial", "design": "rct", "selection_status": "candidate",
                "selection_reason": None, "in_dashboard": True,
                "texts": {"body": "This randomized trial evaluated a treatment."}, "sources": {}}
               for pid in ("10.1000/a", "10.1000/b")]
    calls = []
    def fake(prompt, *args, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            good, bad = fixture_assessment(), fixture_assessment("10.1000/b")
            bad["eligibility_evidence"][0]["quote"] = "unverifiable quotation"
            values = [good, bad]
        else:
            assert '\"paper_id\": \"10.1000/a\"' not in prompt
            assert (tmp_path / "assessments").exists()
            values = [fixture_assessment("10.1000/b")]
        return SimpleNamespace(text=json.dumps({"assessments": values}), input_tokens=100, output_tokens=100)
    monkeypatch.setattr(sr, "call_codex", fake)
    monkeypatch.setattr(sr, "guidance_text", lambda: "guidance")
    result = sr.assess(records, tmp_path, {}, reviewers=1)
    assert len(calls) == 2 and len(result) == 2
    # A restart consumes the checked cache, with no calls or weaker validation.
    sr.assess(records, tmp_path, {}, reviewers=1)
    assert len(calls) == 2
    records[0]["texts"]["body"] += " Changed evidence."
    monkeypatch.setattr(sr, "call_codex", lambda *a, **kw: SimpleNamespace(text=json.dumps({"assessments": [fixture_assessment()]}), input_tokens=1, output_tokens=1))
    sr.assess(records, tmp_path, {}, reviewers=1)
    assert len(list((tmp_path / "assessment_history").glob("*.json"))) == 3


def test_article_xml_keeps_table_spans_and_inline_prose():
    from ber_core.inspect_sr import readable_article_xml
    raw = '<article><body><p>A <italic>randomized</italic> trial.</p><table><tr><th colspan="2">Arms</th></tr><tr><td rowspan="2">A</td><td>12</td></tr></table></body></article>'
    text = readable_article_xml(raw)
    assert "A randomized trial." in text
    assert '<th colspan="2">Arms</th>' in text
    assert '<td rowspan="2">A</td>' in text


def test_source_cache_replacement_is_safe_for_shared_registry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "shared.json"
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: write_json(path, {"writer": i}), range(50)))
    assert json.loads(path.read_text())["writer"] in range(50)
    assert not list(tmp_path.glob("*.tmp"))


def test_manuscript_accounts_for_missing_evidence_and_sensitivity_sets(tmp_path):
    from ber_core.inspect_report import write_manuscript
    p = Project("topic", tmp_path / "config.yaml", {"topic": {"name": "Topic"}}, tmp_path / "project")
    p.data_dir.mkdir(parents=True)
    out = p.project_dir / "systematic_review"
    out.mkdir()
    records = [{"paper_id": pid, "title": "Trial " + pid, "in_dashboard": True,
                "selection_status": "candidate", "selection_reason": None, "duplicate_of": None}
               for pid in ("10.1000/a", "10.1000/b")]
    a = fixture_assessment()
    a["overall"] = {"judgment": "no concerns", "reason": "No concerns identified in supplied evidence"}
    decisions = adjudicate(records, {"10.1000/a": {"reviewer_1": {"assessment": a, "model": "gpt-5.6-terra"}}})
    manifest = {"counts": {"union_reports": 2, "dashboard_reports": 2, "with_text": 1}, "inputs": []}
    summary = write_manuscript(p, records, manifest, decisions, out, use_llm=False)
    assert summary["machine_assessed_reports"] == 1
    assert summary["reports_without_machine_assessment"] == 1
    sets = json.loads((out / "sensitivity_sets.json").read_text())
    assert sets["rct_sensitivity_no_concerns"] == ["10.1000/a"]
    assert (out / "sensitivity_by_intervention.csv").exists()
    appendix = (out / "evidence_appendix.md").read_text()
    assert "10.1000/a" in appendix and "10.1000/b" in appendix
    assert (p.data_dir / "review_document.md").read_text() == (out / "systematic_review.md").read_text()


def test_narrative_requires_numbers_in_the_cited_findings():
    from ber_core.inspect_report import narrative_errors, verified_findings_fallback
    payload = [{"paper_id": "10.1000/a", "findings": ["The between-group difference was -3.6 points."]},
               {"paper_id": "10.1000/b", "findings": []}]
    assert narrative_errors({"paragraphs": []}, payload)
    assert not narrative_errors(verified_findings_fallback(payload), payload)
    paragraph = {"text": "The difference was 3.6 points.", "citations": ["10.1000/a"]}
    assert narrative_errors({"paragraphs": [paragraph]}, payload)
    paragraph["text"] = "The difference was -3.6 points."
    paragraph["citations"] = ["10.1000/b"]
    assert narrative_errors({"paragraphs": [paragraph]}, payload)
    paragraph["citations"] = ["10.1000/a"]
    assert not narrative_errors({"paragraphs": [paragraph]}, payload)


def test_unverified_findings_are_withheld_without_relaxing_other_validation():
    from ber_core.inspect_sr import withhold_unverified_findings
    packet = {"paper_id": "10.1000/a", "sources": {"body": "A randomized trial enrolled 20 people."}}
    a = fixture_assessment()
    evidence = [{"source": "body", "quote": "enrolled 20 people"}]
    a["findings"] = [{"finding": "Enrolled 20 people.", "evidence": evidence},
                     {"finding": "Enrolled 30 people.", "evidence": evidence}]
    retained, omissions = withhold_unverified_findings(a, packet)
    assert retained["findings"] == [a["findings"][0]]
    assert omissions[0]["finding"] == a["findings"][1]
    assert retained["unresolved"] and len(a["findings"]) == 2
    assert validate_assessment(retained, packet)
    a["eligibility_evidence"][0]["quote"] = "Unverifiable eligibility claim"
    assert withhold_unverified_findings(a, packet) is None


def test_scope_conflict_withholds_treatment_inference_without_erasing_report():
    from ber_core.inspect_scope import scope_conflict
    a = fixture_assessment()
    record = {'paper_id': a['paper_id'], 'title': 'Trial', 'in_dashboard': True, 'selection_status': 'candidate'}
    reviews = {a['paper_id']: {'reviewer_1': {'assessment': a, 'model': 'gpt-5.6-terra', 'fingerprint': 'frozen'}}}
    scope = {'scope': 'acute_or_prevention', 'reason': 'Treatment preceded the target condition', 'evidence': []}
    assert scope_conflict(a, scope)
    audit = {'assessment_fingerprint': 'frozen', 'conflict': True, 'audit': scope}
    d = adjudicate([record], reviews, scope_audits={a['paper_id']: audit})[0]
    assert d['synthesis_status'] == 'scope_awaiting_resolution'
    assert d['assessment'] == a and d['in_dashboard']
    assert not synthesis_sets([d])['all_provisional_reports']
    audit['assessment_fingerprint'] = 'stale'
    with pytest.raises(ValueError, match='Scope audit is stale'):
        adjudicate([record], reviews, scope_audits={a['paper_id']: audit})
    assert not scope_conflict(a, {'scope': 'treatment_results'})


def test_scope_audit_resolves_evidence_and_reuses_only_matching_cache(tmp_path, monkeypatch):
    import ber_core.inspect_scope as scope
    from types import SimpleNamespace
    record = {'paper_id':'10.1000/a', 'title':'Trial', 'design':'RCT', 'selection_status':'candidate',
              'selection_reason':None, 'in_dashboard':True, 'texts':{'body':'A randomized trial enrolled patients with long COVID.'}, 'sources':{}}
    first = {'assessment':fixture_assessment(), 'model':'gpt-5.6-terra', 'fingerprint':'v1'}
    calls = []
    def model(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(text=json.dumps({'audits':[{'paper_id':'10.1000/a','scope':'treatment_results',
                'reason':'Participants already had the condition','passages':['@P00001']}]}))
    monkeypatch.setattr(scope, 'call_codex', model)
    results = {'10.1000/a':{'reviewer_1':first}}
    audit = scope.audit_scope([record],results,tmp_path,{})
    assert audit['10.1000/a']['audit']['evidence'][0]['quote'] == record['texts']['body']
    scope.audit_scope([record],results,tmp_path,{})
    assert len(calls) == 1
    first['fingerprint'] = 'v2'
    scope.audit_scope([record],results,tmp_path,{})
    assert len(calls) == 2


def test_human_resolution_reenters_sensitivity_sets_and_clears_machine_queue(tmp_path):
    from copy import deepcopy
    from ber_core.inspect_report import write_manuscript
    a = fixture_assessment()
    a['overall'] = {'judgment':'some concerns','reason':'Unresolved question'}
    b = deepcopy(a)
    b['overall'] = {'judgment':'no concerns','reason':'No concerns in the available evidence'}
    record = {'paper_id':a['paper_id'],'title':'Trial','design':'RCT','selection_status':'candidate',
              'selection_reason':None,'duplicate_of':None,'in_dashboard':True,
              'texts':{'body':'A randomized trial enrolled people with the condition.'},'sources':{}}
    reviews = {a['paper_id']:{'reviewer_1':{'assessment':a,'model':'gpt-5.6-terra','fingerprint':'v1',
                 'validation_omissions':[{'finding':{'finding':'Unverified old claim','evidence':[]},'reason':'Unverified'}]},
                 'reviewer_2':{'assessment':b,'model':'gpt-5.6-terra','fingerprint':'v2'}}}
    human = tmp_path/'human.json'
    write_json(human,{a['paper_id']:{'reviewer':'Test reviewer','reviewed_at':'2026-09-19','reason':'Source verification completed',
                                     'input_fingerprint':'v1','assessment':b}})
    audits = {a['paper_id']:{'assessment_fingerprint':'v1','conflict':True,
              'audit':{'scope':'different_condition','reason':'Machine uncertainty','evidence':[]}}}
    decisions = adjudicate([record], reviews, human, scope_audits=audits)
    assert decisions[0]['disagreements']  # Raw machine differences remain auditable.
    assert synthesis_sets(decisions)['rct_sensitivity_no_concerns'] == decisions
    p = Project('topic',tmp_path/'config.yaml',{'topic':{'name':'Topic'}},tmp_path/'project')
    p.data_dir.mkdir(parents=True)
    out = p.project_dir/'systematic_review';out.mkdir()
    summary = write_manuscript(p,[record],{'counts':{'union_reports':1,'dashboard_reports':1,'with_text':1},'inputs':[]},decisions,out,use_llm=False)
    assert summary['reports_with_withheld_findings'] == summary['scope_audit_conflicts'] == 0
    assert len((out/'adjudication_queue.csv').read_text().splitlines()) == 1
    assert '## Trial' not in (out/'related_evidence.md').read_text()


def test_passage_selection_resolves_verbatim_and_cannot_cross_sources():
    from ber_core.inspect_sr import evidence_passages, resolve_passages
    packet = {"paper_id": "10.1000/a", "sources": {"body": "This randomized trial enrolled 20 people.", "registry": "First posted 2020-01-01"}}
    labeled, index = evidence_passages(packet)
    assert "[@P00001] This randomized trial" in labeled["sources"]["body"]
    a = fixture_assessment()
    a["eligibility_evidence"] = [{"source": "body", "quote": "@P00001"}]
    resolved = resolve_passages(a, index)
    assert resolved["eligibility_evidence"][0]["quote"] == packet["sources"]["body"]
    validate_assessment(resolved, packet)
    a["eligibility_evidence"][0]["source"] = "registry"
    with pytest.raises(ValueError, match="wrong source"):
        resolve_passages(a, index)
    a["eligibility_evidence"][0]["quote"] = "@P99999"
    with pytest.raises(ValueError, match="Unknown passage"):
        resolve_passages(a, index)


def test_numeric_provenance_preserves_signs_and_decimal_magnitude():
    from ber_core.inspect_stats import numeric_tokens
    assert numeric_tokens('−3.6, P=.05; 1,200 people') == numeric_tokens('-3.60, P=0.050; 1200 people')
    assert numeric_tokens('3.6') != numeric_tokens('−3.6')
    assert numeric_tokens('p = 0 ⋅ 1299; -0 ⋅ 75; 2 ⋅ 91') == numeric_tokens('p = 0.1299; -0.75; 2.91')
    assert numeric_tokens('p = 0,477; p < 0,001; n = 1,234') == numeric_tokens('p = 0.477; p < 0.001; n = 1234')
    assert numeric_tokens('72,2 [64,72; 76,1]; 79,1; p<0,05; n=1,234') == numeric_tokens('72.2 [64.72; 76.1]; 79.1; p<0.05; n=1234')
    assert numeric_tokens('p<0,05; groups 1, 2') == numeric_tokens('p<0.05; groups 1, 2')
    assert numeric_tokens('.05') != numeric_tokens('5')
    assert numeric_tokens('FSS-7 with range 10–15') == numeric_tokens('7, 10 and 15')
    packet = {"paper_id": "10.1000/a", "sources": {"body": "randomized trial: difference −3.6"}}
    a = fixture_assessment()
    a['findings'] = [{"finding": "The difference was 3.6", "evidence": [{"source": "body", "quote": "difference −3.6"}]}]
    with pytest.raises(ValueError, match="numbers absent"):
        validate_assessment(a, packet)


def test_bounded_concurrent_assessment_keeps_reports_independent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading
    import time
    import ber_core.inspect_sr as sr
    active = peak = 0
    lock = threading.Lock()
    records = [{"paper_id": '10.1000/' + str(i), "title": "Trial", "design": "rct",
                "selection_status": "candidate", "selection_reason": None, "in_dashboard": True,
                "texts": {"body": "This randomized trial evaluated a treatment."}, "sources": {}}
               for i in range(4)]
    def fake(prompt, *args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.03)
        ids = json.loads(prompt.split('REQUIRED OUTPUT: exactly one assessment for EACH of these report identifiers: ')[1])
        values = [fixture_assessment(pid) for pid in ids]
        with lock:
            active -= 1
        return SimpleNamespace(text=json.dumps({"assessments": values}), input_tokens=1, output_tokens=1)
    monkeypatch.setattr(sr, "call_codex", fake)
    monkeypatch.setattr(sr, "guidance_text", lambda: "guidance")
    results = sr.assess(records, tmp_path, {}, reviewers=1, batch_size=1, workers=2)
    assert set(results) == {r['paper_id'] for r in records}
    assert peak == 2


def test_selection_uses_frozen_bytes_when_live_screen_changes(tmp_path, monkeypatch):
    import ber_core.inspect_corpus as corpus
    p = Project("topic", tmp_path / "config.yaml", {"topic": {"name": "Topic"}}, tmp_path / "project")
    cls = p.data_dir / "classifications.csv"
    row = {"doi": "10.1000/a", "is_relevant": "yes", "is_excluded": "no", "article_type": "Clinical Trial"}
    csv_write(cls, [row])
    original = cls.read_bytes()
    real = corpus.paper_store.build_index
    def during_snapshot(*a, **kw):
        csv_write(cls, [{**row, "is_excluded": "yes"}])
        return real(*a, **kw)
    monkeypatch.setattr(corpus.paper_store, "build_index", during_snapshot)
    records, manifest = collect_corpus(p, tmp_path / "out")
    assert records[0]["selection_status"] == "candidate"
    assert records[0]["sources"]["classification"]["is_excluded"] == "no"
    assert Path(manifest["inputs"][0]["snapshot_path"]).read_bytes() == original
    assert cls.read_bytes() != original


def test_statistical_inequalities_do_not_erase_source_prose():
    from ber_core.inspect_sr import normalize_quote, quote_in_source
    source = 'P < .05. The primary analysis found a difference. A second P > .10.'
    assert 'The primary analysis' in normalize_quote(source)
    assert quote_in_source('The primary analysis found a difference.', source)
    assert quote_in_source('primary analysis', '<p>The <i>primary</i> analysis found a difference.</p>')


def test_completed_response_can_be_recovered_without_another_model_call(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ber_core.inspect_sr as sr
    record = {"paper_id": "10.1000/a", "title": "Trial", "design": "rct", "selection_status": "candidate",
              "selection_reason": None, "in_dashboard": True, "texts": {"body": "randomized trial"}, "sources": {}}
    monkeypatch.setattr(sr, "guidance_text", lambda: "guidance")
    monkeypatch.setattr(sr, "call_codex", lambda *a, **kw: SimpleNamespace(text=json.dumps({"assessments": [fixture_assessment()]}), input_tokens=1, output_tokens=1))
    sr.assess([record], tmp_path, {}, reviewers=1)
    for p in (tmp_path / 'assessments').glob('*.json'):
        p.unlink()
    monkeypatch.setattr(sr, 'call_codex', lambda *a, **kw: pytest.fail('Completed response should have been recovered'))
    result = sr.assess([record], tmp_path, {}, reviewers=1)
    assert result['10.1000/a']['reviewer_1']['recovered_at']


def test_repair_candidates_do_not_approve_unsupported_numbers():
    from ber_core.inspect_sr import repair_hints
    a = fixture_assessment()
    a['findings'] = [{'finding': 'Fatigue improved by 12 points', 'evidence': []}]
    hints = repair_hints(a, {'@P00001': {'source': 'body', 'quote': 'Fatigue changed by 12 points'},
                            '@P00002': {'source': 'body', 'quote': 'There were 90 participants'}})
    assert hints['hints'][0]['candidate_passages'][0]['label'] == '@P00001'
    assert not a['findings'][0]['evidence']  # retrieval does not add a citation or certify support


def test_published_conflict_is_assessed_but_exclusion_is_preserved(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ber_core.inspect_sr as sr
    record = {"paper_id": "10.1000/a", "title": "Trial", "design": "rct", "selection_status": "excluded",
              "selection_reason": "fulltext_ineligible", "in_dashboard": True, "texts": {"body": "randomized trial"}, "sources": {}}
    monkeypatch.setattr(sr, "guidance_text", lambda: "guidance")
    monkeypatch.setattr(sr, "call_codex", lambda *a, **kw: SimpleNamespace(text=json.dumps({"assessments": [fixture_assessment()]}), input_tokens=1, output_tokens=1))
    results = sr.assess([record], tmp_path, {}, reviewers=1)
    decision = adjudicate([record], results)[0]
    assert decision['machine_reviewers'] == 1
    assert decision['assessment']['eligibility'] == 'randomized_trial'
    assert decision['synthesis_status'] == 'excluded'


def test_numeric_provenance_handles_real_publisher_formats():
    from ber_core.inspect_stats import numeric_tokens
    assert numeric_tokens('95% CI 0.12 - 0.47') == numeric_tokens('95% CI 0.12–0.47')
    assert numeric_tokens('−2 - −1') == numeric_tokens('-2 to -1')
    assert numeric_tokens(r'follow-up at\n3 months, difference\n36.5') == numeric_tokens('follow-up at 3 months, difference 36.5')
    assert numeric_tokens(r'−\n20⋅0 [−28⋅5; −\n11⋅0], p = 0⋅043') == numeric_tokens('-20.0 [-28.5; -11.0], p = 0.043')
    assert numeric_tokens('Fifty -five of the 483 participants') == numeric_tokens('55 of 483 participants')
    assert numeric_tokens('<td rowspan="7">0.47</td>') == numeric_tokens('0.47')
    assert numeric_tokens('2·3') != numeric_tokens('2.3')


def test_retraction_gate_applies_to_nonrandomized_reports():
    a = fixture_assessment()
    a.update(eligibility='nonrandomized_trial', checks=[], domains=[])
    record = {'paper_id': a['paper_id'], 'title': 'Trial', 'in_dashboard': True, 'selection_status': 'candidate',
              'sources': {'retraction_watch': {'doi_matches': [{'RetractionNature': 'Retraction'}]}}}
    reviews = {a['paper_id']: {'reviewer_1': {'assessment': a, 'model': 'gpt-5.6-terra'}}}
    d = adjudicate([record], reviews)[0]
    assert d['synthesis_status'] == 'retraction_awaiting_notice_verification'
    assert not synthesis_sets([d])['all_provisional_reports']
    record['sources']['retraction_notices'] = [{'status': 'publisher_metadata_confirmed'}]
    assert adjudicate([record], reviews)[0]['synthesis_status'] == 'excluded_retracted'
    record['sources'].pop('retraction_watch')
    assert adjudicate([record], reviews)[0]['synthesis_status'] == 'excluded_retracted'


def test_publisher_notice_confirmation_requires_publisher_link(tmp_path, monkeypatch):
    import ber_core.inspect_sources as s
    rows = [{'RetractionNature': 'Retraction', 'RetractionDOI': '10.1000/notice'}]
    m = {'DOI': '10.1000/notice', 'title': ['Retraction Notice to Trial'],
         'update-to': [{'DOI': '10.1000/a', 'source': 'retraction-watch', 'type': 'retraction'}]}
    monkeypatch.setattr(s,'crossref_lookup',lambda *a,**kw: {'status':'ok','data':{'message':m}})
    assert s.retraction_notices('10.1000/a',rows,tmp_path)[0]['status'] == 'requires_notice_verification'
    m['update-to'].append({'DOI':'10.1000/a','source':'publisher','type':'erratum'})
    assert s.retraction_notices('10.1000/a',rows,tmp_path)[0]['status'] == 'publisher_metadata_confirmed'
    m['title'] = ['Correction to Trial']
    assert s.retraction_notices('10.1000/a',rows,tmp_path)[0]['status'] == 'requires_notice_verification'


def test_dashboard_matches_javascript_empty_object_and_csv_whitespace(tmp_path):
    jsonl_write(tmp_path/'trial_extractions.jsonl',[
        {'paper_id':'10.1000/a','study_design':{}},
        {'paper_id':'10.1000/b','study_design':{'design_type':'RCT'}}])
    csv_write(tmp_path/'trial_screening.csv',[{'doi':' 10.1000/b ','is_excluded':' YES '}])
    assert [r['paper_id'] for r in dashboard_records(tmp_path)] == ['10.1000/a']


def test_missing_requested_human_adjudication_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        adjudicate([], {}, tmp_path/'missing.json')
