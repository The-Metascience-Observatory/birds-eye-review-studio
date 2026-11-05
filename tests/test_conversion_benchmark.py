"""No model downloads or production data: test the benchmark's trust boundaries."""
import copy
import json
import os
from pathlib import Path
import sys
import time

import pytest

from ber_core import conversion_benchmark as b


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def paper(tmp_path, name="a", family=None, split="dev", sentinel=False):
    directory = tmp_path / "store" / "Clinical_Trial" / name
    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / "source.pdf"
    pdf.write_bytes(b"%PDF fixture " + name.encode())
    body = directory / "body.md"
    body.write_text("Original table value 51%", encoding="utf-8")
    provenance = write_json(directory / "provenance.json", {"extraction_mode": "docling-only"})
    return {"paper_id": f"10.1234/{name}", "paper_dir": str(directory), "sources": [b.artifact(pdf, "main_pdf")],
            "baseline": [b.artifact(body, "baseline"), b.artifact(provenance, "baseline")],
            "split": split, "dev_sentinel": sentinel, "gold_path": None,
            "verification": {"is_rct": True, "identity_verified": True, "family_verified": True,
                             "trial_family": family or name, "verified_by": "reviewer", "evidence_refs": ["PDF p1 methods"]}}


def fact(p, **changes):
    result = dict.fromkeys(b.FACT_FIELDS)
    result.update(population="long covid", subgroup="all", arm="cacao", outcome="fatigue",
                  timepoint="6 weeks", n=20, denominator=None, statistic="within_arm_percent_change",
                  value="51", units="percent", uncertainty_type="not reported", adjustment="unadjusted",
                  primary=True, review_required=False,
                  evidence={"source_sha256": p["sources"][0]["sha256"], "locator": "p7 table 2"})
    result.update(changes)
    return result


def gold(tmp_path, p, facts=None):
    result = b.annotation_template(p)
    result.update(status="adjudicated", source_coverage_complete=True, conflicts_resolved=True,
                  adjudicated_by="reviewer-two", facts=facts or [fact(p)])
    reviewers = []
    for name in ("reviewer-one", "reviewer-two"):
        annotation = {"paper_id": p["paper_id"], "reviewed_by": name,
                      "status": "independent_review_complete", "source_coverage_complete": True,
                      "sources": p["sources"], "facts": result["facts"]}
        path = write_json(tmp_path / (p["paper_id"].split("/")[-1] + name + ".json"), annotation)
        reviewers.append({"id": name, "independent": True, "source_reviewed": True,
                          "annotation": b.artifact(path, "independent_annotation")})
    result["reviewers"] = reviewers
    path = write_json(tmp_path / (p["paper_id"].split("/")[-1] + "-gold.json"), result)
    p["gold_path"] = str(path)
    return result


ADAPTER = {"id": "baseline", "kind": "snapshot", "configuration": {"copy": True},
           "packages": {"benchmark_snapshot": "1"}, "python": sys.executable}
EVALUATION = {"model": "frozen-test-model", "prompt_sha256": "a" * 64,
              "extraction_options": {"all_outcomes": True}, "source_policy": "pdf_only",
              "input_budget": 80000, "packaging": "fixed-body"}


def frozen(tmp_path, monkeypatch, papers, *, split="dev", adapters=None):
    monkeypatch.setattr(b, "runtime_probe", lambda *args, **kwargs: {"packages": {}, "cuda_available": False})
    cohort = {"status": "verified_split", "seed": "test", "papers": papers,
              "paper_store_root": str(tmp_path / "store")}
    return b.freeze(cohort, adapters or [ADAPTER], EVALUATION, split=split)


def sealed(tmp_path, monkeypatch, p, facts=None, flagged=False):
    f = frozen(tmp_path, monkeypatch, [p])
    run = {"frozen_sha256": f["sha256"], "results": [{"adapter": "baseline", "paper_id": p["paper_id"],
            "requires_review": False, "artifacts": []}]}
    predictions = {"adapter": "baseline", "papers": [{"paper_id": p["paper_id"],
                   "review_required": flagged, "flags": ["review"] if flagged else [],
                   "facts": facts if facts is not None else [fact(p)]}]}
    return b.seal_predictions(predictions, f, run), f


def test_inventory_is_pending_and_no_observational_pilot_is_imported(tmp_path):
    p = paper(tmp_path)
    project = tmp_path / "project"
    bucket = project / "eligible_rct"
    bucket.mkdir(parents=True)
    # paper_store resolves the DOI-named folder independent of its type.
    old_dir = Path(p["paper_dir"])
    directory = old_dir.with_name("10.1234--a")
    old_dir.rename(directory)
    (bucket / directory.name).symlink_to(directory, target_is_directory=True)
    result = b.inventory(project, papers_root=tmp_path / "store")
    assert len(result["papers"]) == 1
    candidate = result["papers"][0]
    assert candidate["verification"]["is_rct"] is None
    assert result["status"] == "proposal_unverified"
    with pytest.raises(b.BenchmarkError, match="No source"):
        b.split_inventory(result)


def test_split_deterministic_families_and_sentinels(tmp_path):
    papers = [paper(tmp_path, str(i)) for i in range(65)]
    papers[1]["verification"]["trial_family"] = papers[0]["verification"]["trial_family"]
    papers[1]["dev_sentinel"] = True
    first = b.assign_splits(papers, seed="fixed")
    assert first == b.assign_splits(list(reversed(papers)), seed="fixed")
    assert first[papers[0]["paper_id"]] == first[papers[1]["paper_id"]] == "dev"
    assert sum(s == "dev" for s in first.values()) >= 20
    assert sum(s == "holdout" for s in first.values()) >= 40
    papers[2]["verification"]["is_rct"] = False
    assert papers[2]["paper_id"] not in b.assign_splits(papers, seed="fixed")


def test_freeze_rejects_family_leakage_and_pilot_holdout(tmp_path, monkeypatch):
    dev = paper(tmp_path, "dev", family="shared")
    holdout = paper(tmp_path, "holdout", family="shared", split="holdout")
    with pytest.raises(b.BenchmarkError, match="family leaks"):
        frozen(tmp_path, monkeypatch, [dev, holdout])
    holdout["verification"]["trial_family"] = "different"
    holdout["dev_sentinel"] = True
    with pytest.raises(b.BenchmarkError, match="sentinel"):
        frozen(tmp_path, monkeypatch, [dev, holdout])


def test_holdout_requires_independent_gold(tmp_path, monkeypatch):
    p = paper(tmp_path, split="holdout")
    with pytest.raises(b.BenchmarkError, match="independent reviewers"):
        frozen(tmp_path, monkeypatch, [p], split="holdout")
    g = gold(tmp_path, p)
    assert frozen(tmp_path, monkeypatch, [p], split="holdout")["payload"]["gold"]
    g["reviewers"] = g["reviewers"][:1]
    with pytest.raises(b.BenchmarkError, match="Two distinct"):
        b.validate_gold(g, p)


def test_independent_annotation_and_gold_source_guards(tmp_path):
    p = paper(tmp_path)
    g = gold(tmp_path, p)
    b.validate_gold(g, p)
    Path(g["reviewers"][0]["annotation"]["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(b.BenchmarkError, match="changed"):
        b.validate_gold(g, p)


def test_source_and_configuration_drift_stop_run(tmp_path, monkeypatch):
    p = paper(tmp_path)
    f = frozen(tmp_path, monkeypatch, [p])
    changed = copy.deepcopy(f)
    changed["payload"]["evaluation"]["model"] = "different"
    with pytest.raises(b.BenchmarkError, match="manifest changed"):
        b.validate_frozen(changed)
    Path(p["sources"][0]["path"]).write_bytes(b"changed")
    with pytest.raises(b.BenchmarkError, match="changed"):
        b.run(f, tmp_path / "output")


def test_snapshot_isolated_and_historical_not_timed_as_conversion(tmp_path, monkeypatch):
    p = paper(tmp_path)
    f = frozen(tmp_path, monkeypatch, [p])
    with pytest.raises(b.BenchmarkError, match="paper store"):
        b.run(f, tmp_path / "store" / "new-output")
    result = b.run(f, tmp_path / "run")
    row = result["results"][0]
    assert row["usable_output"]
    assert not row["requires_review"]
    assert row["conversion_status"] == "historical_unknown"
    assert "not conversion latency" in row["measurement"]
    assert b.sha256(Path(p["paper_dir"]) / "body.md") == p["baseline"][0]["sha256"]
    with pytest.raises(b.BenchmarkError, match="already exists"):
        b.run(f, tmp_path / "run")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group test")
def test_deadline_kills_descendant_processes(tmp_path):
    marker = tmp_path / "escaped.txt"
    child = f"import time,pathlib; time.sleep(0.7); pathlib.Path({str(marker)!r}).write_text('bad')"
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
    result = b.supervise([sys.executable, "-c", code], cwd=tmp_path, timeout=0.2,
                         log_path=tmp_path / "worker.log")
    assert result["timed_out"]
    assert result["wall_seconds"] < 5
    time.sleep(0.8)
    assert not marker.exists()


def test_external_adapter_requires_pins_and_runs_explicit_argv(tmp_path, monkeypatch):
    p = paper(tmp_path)
    script = tmp_path / "fake converter.py"
    script.write_text("import sys,pathlib,json\nout=pathlib.Path(sys.argv[2])\n"
                      "(out/'body.md').write_text('table 51 percent')\n"
                      "(out/'provenance.json').write_text(json.dumps({'conversion_status':'complete'}))\n", encoding="utf-8")
    adapter = {"id": "fake", "kind": "external", "python": sys.executable,
               "packages": {"benchmark_snapshot": "1"}, "configuration": {"fake": True},
               "model_artifacts": [b.artifact(script, "test_model")],
               "code_artifacts": [b.artifact(script, "wrapper_code")],
               "argv": [sys.executable, str(script), "{input}", "{output}"]}
    bad = {**adapter, "model_artifacts": []}
    with pytest.raises(b.BenchmarkError, match="hashed model"):
        b.validate_adapter(bad, probe=False)
    f = frozen(tmp_path, monkeypatch, [p], adapters=[adapter])
    result = b.run(f, tmp_path / "run")
    assert result["results"][0]["usable_output"]
    assert result["results"][0]["conversion_status"] == "complete"


def test_cuda_required_never_falls_back_to_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "runtime_probe", lambda *args, **kwargs: {"packages": {}, "cuda_available": False})
    adapter = {**ADAPTER, "requires_cuda": True}
    with pytest.raises(b.BenchmarkError, match="CUDA unavailable"):
        b.validate_adapter(adapter)


def test_correct_facts_score_and_percent_is_not_a_mean(tmp_path, monkeypatch):
    p = paper(tmp_path)
    gold(tmp_path, p)
    s, f = sealed(tmp_path, monkeypatch, p, [fact(p, value="51.0")])
    baseline = b.score(s, f)
    assert baseline["auto_primary_recall"] == 1
    assert baseline["auto_precision"] == 1
    s2, f2 = sealed(tmp_path, monkeypatch, p, [fact(p, statistic="mean")])
    result = b.score(s2, f2, baseline=baseline)
    assert result["counts"]["wrong_statistic"] == 1
    assert result["counts"]["unflagged_critical_errors"] == 1
    assert not result["acceptance"]["passed"]


def test_median_iqr_cannot_be_scored_as_ci(tmp_path, monkeypatch):
    p = paper(tmp_path)
    correct = fact(p, statistic="median", outcome="FEV1 %VCMAX", value="76.26", units="percent",
                   uncertainty_type="IQR", uncertainty_lower="73.33", uncertainty_upper="80.95")
    gold(tmp_path, p, [correct])
    s, f = sealed(tmp_path, monkeypatch, p, [{**correct, "uncertainty_type": "CI"}])
    result = b.score(s, f)
    assert result["raw_recall"] == 0
    assert result["counts"]["wrong_uncertainty_type"] == 1
    damaged = {**correct, "uncertainty_upper": "80.9"}
    s, f = sealed(tmp_path, monkeypatch, p, [damaged])
    assert b.score(s, f)["counts"]["unflagged_critical_errors"] == 1


def test_all_flagged_never_passes_or_improves_automatic_recall(tmp_path, monkeypatch):
    p = paper(tmp_path)
    gold(tmp_path, p)
    s, f = sealed(tmp_path, monkeypatch, p)
    baseline = b.score(s, f)
    s, f = sealed(tmp_path, monkeypatch, p, flagged=True)
    result = b.score(s, f, baseline=baseline)
    assert result["raw_recall"] == 1
    assert result["auto_primary_recall"] == 0
    assert result["counts"]["unflagged_critical_errors"] == 0
    assert not result["acceptance"]["passed"]
    assert result["flagged_report_fraction"] == 1


def test_missing_papers_partial_conversions_and_late_flag_changes_rejected(tmp_path, monkeypatch):
    p = paper(tmp_path)
    gold(tmp_path, p)
    s, f = sealed(tmp_path, monkeypatch, p)
    s["payload"]["predictions"]["papers"][0]["review_required"] = True
    with pytest.raises(b.BenchmarkError, match="changed after sealing"):
        b.score(s, f)
    run = {"frozen_sha256": f["sha256"], "results": [{"adapter": "baseline", "paper_id": p["paper_id"],
            "requires_review": True, "artifacts": []}]}
    predictions = {"adapter": "baseline", "papers": []}
    with pytest.raises(b.BenchmarkError, match="every frozen"):
        b.seal_predictions(predictions, f, run)
    predictions["papers"] = [{"paper_id": p["paper_id"], "review_required": False, "flags": [], "facts": [fact(p)]}]
    with pytest.raises(b.BenchmarkError, match="cannot be automatically accepted"):
        b.seal_predictions(predictions, f, run)


@pytest.mark.parametrize("field,value,category", [("arm", "control", "wrong_arm"),
                                                   ("timepoint", "12 weeks", "wrong_timepoint")])
def test_wrong_attribution_is_critical(tmp_path, monkeypatch, field, value, category):
    p = paper(tmp_path)
    gold(tmp_path, p)
    s, f = sealed(tmp_path, monkeypatch, p, [fact(p, **{field: value})])
    result = b.score(s, f)
    assert result["counts"][category] == 1
    assert result["counts"]["unflagged_critical_errors"] == 1


def test_duplicate_predictions_and_changed_baseline_cannot_inflate_results(tmp_path, monkeypatch):
    p = paper(tmp_path)
    gold(tmp_path, p)
    s, f = sealed(tmp_path, monkeypatch, p, [fact(p), fact(p)])
    result = b.score(s, f)
    assert result["raw_precision"] == 0.5
    assert result["counts"]["unflagged_critical_errors"] == 1
    changed = {**result, "evaluation_sha256": "different"}
    with pytest.raises(b.BenchmarkError, match="same sources"):
        b.score(s, f, baseline=changed)


def test_template_has_no_gold_or_eligibility_claims(tmp_path):
    p = paper(tmp_path)
    template = b.annotation_template(p)
    assert not template["source_coverage_complete"]
    assert template["facts"] == template["reviewers"] == []
    with pytest.raises(b.BenchmarkError, match="unverified"):
        b.validate_gold(template, p)


def test_paired_extraction_dry_run_stages_only_markdown_and_exact_controls(tmp_path, monkeypatch):
    p = paper(tmp_path)
    f = frozen(tmp_path, monkeypatch, [p])
    resolved = {"system": "system", "user_template": "user", "resolved_model": "frozen-test-model"}
    f["payload"]["evaluation"].update(provider="codex_cli", raw_prompt="raw prompt", resolved_prompt=resolved,
                                    pass2_config={"topic": {"name": "Long COVID"}},
                                    packaging="legacy", prompt_sha256=b.digest(resolved))
    f["sha256"] = b.digest(f["payload"])
    monkeypatch.setattr(b, "_resolve_prompt", lambda *args: resolved)
    results = b.run(f, tmp_path / "conversion")
    monkeypatch.setattr(b, "supervise", lambda *args, **kwargs: pytest.fail("Dry run invoked LLM process"))
    plan = b.prepare_extraction(f, results, tmp_path / "extraction")
    assert plan["execute"] is False
    assert len(plan["jobs"]) == 3
    for job in plan["jobs"]:
        assert job["status"] == "prepared_no_llm_calls"
        assert "--no-xml-first" in job["argv"] and "--no-retry-on-empty" in job["argv"]
        assert job["argv"][job["argv"].index("--input-budget-chars") + 1] == "80000"
        assert job["argv"][job["argv"].index("--input-packaging") + 1] == "legacy"
        inputs = Path(job["argv"][job["argv"].index("--input") + 1])
        assert {path.name for path in inputs.rglob("*") if path.is_file()} == {"body.md", "provenance.json"}
        assert Path(job["environment_overrides"]["BER_PROJECTS_DIR"]).is_relative_to(tmp_path / "extraction")


def test_real_pass2_prompt_resolution_is_nonmutating_and_preserves_exact_flags(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("topic:\n  name: Long COVID\n", encoding="utf-8")
    evaluation = b.prepare_evaluation(config, model="gpt-6.1-sol")
    assert evaluation["extraction_options"]["subgroup_analyses"] is True
    assert "subgroup" not in evaluation["extraction_options"]
    assert evaluation["prompt_sha256"] == b.digest(evaluation["resolved_prompt"])
    assert evaluation["pass2_config"]["dirs"] == {}
    assert evaluation["resolved_prompt"]["resolved_model"] == "gpt-6.1-sol"
    assert list(tmp_path.iterdir()) == [config]


def test_explicit_mapping_preserves_raw_numbers_and_semantic_review(tmp_path, monkeypatch):
    p = paper(tmp_path)
    f = frozen(tmp_path, monkeypatch, [p])
    raw = {"paper_id": p["paper_id"], "_status": "ok", "outcomes": [{"arm_results": [{"percent": 51}]}]}
    path = tmp_path / "raw.jsonl"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    mapped = fact(p, n=None, raw_fields={"value": "/outcomes/0/arm_results/0/percent"})
    mapping = {"adapter": "baseline", "raw_artifact": b.artifact(path, "raw_pass2"),
               "papers": [{"paper_id": p["paper_id"], "raw_record_sha256": b.digest(raw),
                           "review_required": False, "flags": [], "mapping_verified_by": "mapper",
                           "facts": [mapped]}]}
    result = b.map_predictions(mapping, f)
    assert result["papers"][0]["facts"][0]["value"] == "51"
    mapped["value"] = "22.6"
    with pytest.raises(b.BenchmarkError, match="differs from its raw"):
        b.map_predictions(mapping, f)
    mapped["value"] = "51"
    mapping["papers"][0]["mapping_verified_by"] = ""
    with pytest.raises(b.BenchmarkError, match="verified semantics"):
        b.map_predictions(mapping, f)


def test_none_baseline_primary_recall_fails_gate_without_type_error(tmp_path, monkeypatch):
    p = paper(tmp_path)
    gold(tmp_path, p)
    s, f = sealed(tmp_path, monkeypatch, p)
    baseline = b.score(s, f)
    baseline["auto_primary_recall"] = None
    assert not b.score(s, f, baseline=baseline)["acceptance"]["passed"]


@pytest.mark.parametrize("warning", [{"warnings": ["cell_digit_mismatch"]}, {"requires_review": True}])
def test_complete_status_and_snapshot_cannot_hide_explicit_warnings(tmp_path, monkeypatch, warning):
    p = paper(tmp_path)
    provenance = write_json(Path(p["paper_dir"]) / "provenance.json", {"conversion_status": "complete", **warning})
    p["baseline"][1] = b.artifact(provenance, "baseline")
    f = frozen(tmp_path, monkeypatch, [p])
    result = b.run(f, tmp_path / "run")
    assert result["results"][0]["usable_output"]
    assert result["results"][0]["requires_review"]
    assert result["results"][0]["provenance_warnings"] == warning


def test_scientific_summary_reports_variance_and_withheld_comparisons_without_gold_claim():
    record = {"paper_id": "10.1234/example", "_status": "ok", "synthesis_population": "adults with long covid",
              "study_design": {"design_type": "RCT", "arms": [
                  {"arm_id": 1, "label": "Therapy", "type": "intervention", "intervention_name": "Therapy"},
                  {"arm_id": 2, "label": "Placebo", "type": "placebo", "intervention_name": "Placebo"}]},
              "outcomes": [{"outcome_id": 1, "name": "Fatigue", "is_primary": True, "unit": "points",
                  "analysis_basis": "endpoint", "analysis_population": "ITT",
                  "timepoints": [{"timepoint_id": 1, "label": "Week 4", "weeks_from_randomization": 4}],
                  "between_group_effects": [{"intervention_arm_id": 1, "comparator_arm_id": 2,
                      "timepoint_id": 1, "effect_measure": "mean_difference", "effect_value": -2,
                      "se": 0.4, "result_type": "endpoint", "analysis_population": "ITT", "effect_source": "verbatim"}]}]}
    result = b.scientific_summary({record["paper_id"]: record})
    row = result["reports"][0]["comparisons"][0]
    assert row["estimate"] == -2
    assert row["variance"] == pytest.approx(0.16)
    assert row["poolable"]
    assert result["reports"][0]["status"] == "derived_not_source_verified"
    record["outcomes"][0]["between_group_effects"][0]["se"] = None
    row = b.scientific_summary({record["paper_id"]: record})["reports"][0]["comparisons"][0]
    assert not row["poolable"]
    assert any(issue["code"] == "unresolved_uncertainty" for issue in row["withheld_reasons"])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper/process tree")
@pytest.mark.parametrize("orphan", [False, True])
def test_timeout_reaps_detached_and_orphaned_workers(tmp_path, orphan):
    marker = tmp_path / "escaped.txt"
    ready = tmp_path / "ready.txt"
    child = (f"import pathlib,time; pathlib.Path({str(ready)!r}).write_text('started'); "
             f"time.sleep(1.2); pathlib.Path({str(marker)!r}).write_text('escaped')")
    detached = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
    if orphan:
        # Intermediate process exits; the detached grandchild must be adopted.
        middle = detached
        code = f"import subprocess,sys,time; subprocess.run([sys.executable,'-c',{middle!r}]); time.sleep(20)"
    else:
        code = detached + "time.sleep(20)"
    result = b.supervise([sys.executable, "-c", code], cwd=tmp_path, timeout=0.6,
                         log_path=tmp_path / "worker.log")
    assert ready.exists(), (tmp_path / "worker.log").read_text()
    assert result["timed_out"]
    time.sleep(1)
    assert not marker.exists()


def add_native(p, relative, text="source", role="supplement"):
    path = Path(p["paper_dir"]) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    item = {**b.artifact(path, role), "relative_path": relative}
    p["sources"].append(item)
    return item


def production_frozen(tmp_path, monkeypatch, p):
    f = frozen(tmp_path, monkeypatch, [p])
    resolved = {"system": "system", "user_template": "user", "resolved_model": "frozen-test-model"}
    f["payload"]["evaluation"].update(provider="codex_cli", raw_prompt="raw prompt", resolved_prompt=resolved,
                                    pass2_config={"topic": {"name": "Long COVID"}},
                                    packaging="table-aware", prompt_sha256=b.digest(resolved), source_policy="production")
    f["payload"].update(experiment="production_workflow", source_policy="production")
    f["sha256"] = b.digest(f["payload"])
    monkeypatch.setattr(b, "_resolve_prompt", lambda *args: resolved)
    return f


def test_production_stages_pinned_xml_and_supplements_without_basename_collisions(tmp_path, monkeypatch):
    p = paper(tmp_path)
    add_native(p, "publisher.xml", "<article><body>Native text</body></article>", "main_xml")
    add_native(p, "article.fulltext.html", "<p>Native HTML</p>", "main_html")
    add_native(p, "supplementary/a/body.md", "first supplement")
    add_native(p, "supplementary/b/body.md", "second supplement")
    f = production_frozen(tmp_path, monkeypatch, p)
    results = b.run(f, tmp_path / "conversions")
    plan = b.prepare_extraction(f, results, tmp_path / "extraction", repetitions=1)
    job = plan["jobs"][0]
    inputs = Path(job["argv"][job["argv"].index("--input") + 1]) / "10.1234--a"
    assert (inputs / "10.1234--a.xml").is_file()
    assert (inputs / "supplementary/a/body.md").read_text() == "first supplement"
    assert (inputs / "supplementary/b/body.md").read_text() == "second supplement"
    assert "--xml-first" in job["argv"] and "--no-retry-on-empty" in job["argv"]
    assert "--no-xml-first" not in job["argv"]
    assert not list(inputs.glob("*.pdf"))
    assert job["staged"][0]["limitations"][0]["reason"].startswith("native HTML pinned/staged")
    assert plan["experiment"] == "production_workflow"


@pytest.mark.parametrize("relative", ["../escape.md", "/absolute.md", "supplementary/../body.md", "supplementary\\bad.md"])
def test_production_rejects_unsafe_relative_paths(tmp_path, relative):
    p = paper(tmp_path)
    item = add_native(p, "supplementary/real.md")
    item["relative_path"] = relative
    with pytest.raises(b.BenchmarkError, match="Unsafe"):
        b.production_sources(p)


def test_production_native_hash_drift_and_ambiguous_xml_fail_closed(tmp_path, monkeypatch):
    p = paper(tmp_path)
    item = add_native(p, "source.xml", "<article/>", "main_xml")
    f = production_frozen(tmp_path, monkeypatch, p)
    Path(item["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(b.BenchmarkError, match="changed"):
        b.run(f, tmp_path / "run")
    add_native(p, "other.xml", "<article/>", "main_xml")
    with pytest.raises(b.BenchmarkError, match="Multiple main XML"):
        b.production_sources(p)


def test_inventory_pins_native_and_converted_supplements_excludes_bookkeeping(tmp_path):
    p = paper(tmp_path, "10.1234--a")
    add_native(p, "10.1234--a.xml", "<article/>", "main_xml")
    add_native(p, "10.1234--a.fulltext.html", "<p>native</p>", "main_html")
    for name in ("data.md", "notes.txt", "table.htm", "import_provenance.json", "abstract.md", "references.md"):
        add_native(p, "supplementary/" + name)
    project = tmp_path / "project"
    (project / "eligible_rct").mkdir(parents=True)
    (project / "eligible_rct" / "10.1234--a").symlink_to(p["paper_dir"], target_is_directory=True)
    result = b.inventory(project, papers_root=tmp_path / "store")
    sources = result["papers"][0]["sources"]
    assert {s["role"] for s in sources} == {"main_pdf", "main_xml", "main_html", "supplement"}
    names = {Path(s["path"]).name for s in sources}
    assert {"data.md", "notes.txt", "table.htm"} <= names
    assert not {"import_provenance.json", "abstract.md", "references.md"} & names


def test_adapter_environment_is_validated_frozen_and_passed_to_process(tmp_path, monkeypatch):
    p = paper(tmp_path)
    script = tmp_path / "fake.py"
    script.write_text("import os,pathlib,sys,json\no=pathlib.Path(sys.argv[2])\n"
                      "(o/'body.md').write_text(os.environ['HF_HUB_OFFLINE'])\n"
                      "(o/'provenance.json').write_text(json.dumps({'conversion_status':'complete'}))\n", encoding="utf-8")
    adapter = {"id": "fake", "kind": "external", "packages": {"benchmark_snapshot": "1"},
               "configuration": {"test": True}, "environment": {"HF_HUB_OFFLINE": "1"},
               "model_artifacts": [b.artifact(script, "test_model")], "code_artifacts": [b.artifact(script, "wrapper")],
               "argv": [sys.executable, str(script), "{input}", "{output}"]}
    with pytest.raises(b.BenchmarkError, match="string-to-string"):
        b.validate_adapter({**adapter, "environment": {"HF_HUB_OFFLINE": 1}}, probe=False)
    f = frozen(tmp_path, monkeypatch, [p], adapters=[adapter])
    result = b.run(f, tmp_path / "run")
    assert (Path(result["results"][0]["output"]) / "body.md").read_text() == "1"
    assert f["payload"]["adapters"][0]["environment"]["HF_HUB_OFFLINE"] == "1"
