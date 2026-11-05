"""Pipeline state from Step.produces, manual staging, output freshness,
and fail-loud decision files."""
from __future__ import annotations

import json
import os
import time

import pytest

from ber_core import pipeline
from ber_core import projects
from ber_core.evidence import CorruptDecisionFile, load_exclusion_dois, load_inclusion_dois
from ber_core.execution import freshness_warning, stale_outputs, verify_produces


def _project(tmp_path, **cfg_extra):
    cfg = {"topic": {"name": "Demo"}, "include_observational": False, **cfg_extra}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(json.dumps(cfg))
    proj = projects.Project(slug="demo", config_path=cfg_path, config=cfg,
                            project_dir=tmp_path / "projects" / "demo")
    proj.data_dir.mkdir(parents=True)
    return proj


def _by_script(states):
    return {s.script: s for s in states}


# ── B13: derive_pipeline_state reads Step.produces ─────────────────────────

def test_fresh_project_is_all_pending(tmp_path):
    states = projects.derive_pipeline_state(_project(tmp_path))
    assert states and all(s.status == "pending" for s in states)
    scripts = [s.script for s in states]
    assert scripts[:3] == ["search_papers.py", "fetch_abstracts.py", "classify.py"]
    assert "qc_report.py" not in scripts


def test_quick_screen_without_relevance_csv_progresses(tmp_path):
    proj = _project(tmp_path)
    (proj.data_dir / "search_dois.csv").write_text("doi\n10.1/a\n")
    proj.abstracts_dir.mkdir()
    (proj.abstracts_dir / "10.1--a_abstract.md").write_text("x")
    (proj.data_dir / "classifications.csv").write_text("doi,is_relevant\n10.1/a,yes\n")
    st = _by_script(projects.derive_pipeline_state(proj))
    assert st["search_papers.py"].status == "done"
    assert st["fetch_abstracts.py"].status == "done"
    # quick done, screen not yet: partial — never stuck on relevance.csv.
    assert st["classify.py"].status == "partial"
    assert "classifications.csv: 1 rows" in st["classify.py"].detail
    (proj.data_dir / "trial_screening.csv").write_text("doi,is_relevant\n10.1/a,yes\n")
    assert _by_script(projects.derive_pipeline_state(proj))["classify.py"].status == "done"


def test_bundle_is_read_from_the_project_not_a_sibling_repo(tmp_path):
    proj = _project(tmp_path)
    bundle = proj.project_dir / "dashboard"
    bundle.mkdir()
    (bundle / "trial_extractions.jsonl").write_text("")   # allow_empty_outputs
    st = _by_script(projects.derive_pipeline_state(proj))
    assert st["export_dashboard.py"].status == "done"


def test_no_repo_root_fallback(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setattr(projects, "REPO_ROOT", tmp_path)
    (tmp_path / "trial_extractions.jsonl").write_text('{"x": 1}\n')
    assert projects._find_jsonl(proj, ["trial_extractions.jsonl"]) is None


def test_empty_staged_bucket_counts_as_done(tmp_path):
    proj = _project(tmp_path)
    (proj.project_dir / "eligible_rct").mkdir()
    st = _by_script(projects.derive_pipeline_state(proj))
    assert st["extract_pass2.py"].status == "done"


def test_streamlit_only_helpers_are_gone():
    for name in ("RUNS_DIR", "ACTIVE_PROJECT_FILE", "get_active_project",
                 "set_active_project", "discover_projects", "project_fingerprint",
                 "project_overview_row", "SEARCH_SOURCE_KEYS", "create_project"):
        assert not hasattr(projects, name), name


# ── B11: staging: manual keeps hands off curated buckets ───────────────────

def test_manual_staging_skips_pass1_and_stage_eligible(tmp_path):
    proj = _project(tmp_path, staging="manual")
    assert pipeline.eligibility_steps(proj) == []
    selected = [s for s in pipeline.canonical_steps(proj, include_observational=False)
                if s.id == "9_pass2_rct"]
    ids = [s.id for s in pipeline.execution_steps(proj, selected)]
    assert "stage_eligible" not in ids and "8_extract_pass1" not in ids
    assert "5_download_pdfs" in ids


def test_default_staging_runs_pass1(tmp_path):
    proj = _project(tmp_path)
    assert [s.id for s in pipeline.eligibility_steps(proj)] == ["8_extract_pass1", "stage_eligible"]
    with pytest.raises(ValueError):
        pipeline.staging_mode({"staging": "sometimes"})


# ── CLI selection over the full run order ──────────────────────────────────

def _run_ids(proj, **kw):
    canonical = pipeline.canonical_steps(proj, include_observational=False)
    return [s.id for s in pipeline.select_run(proj, canonical, **kw)]


def test_a_prerequisite_step_can_be_selected_alone(tmp_path):
    proj = _project(tmp_path)
    assert _run_ids(proj, only=["6"]) == ["6_convert_pdfs"]
    assert _run_ids(proj, only=["stage_eligible"]) == ["stage_eligible"]
    assert _run_ids(proj, start="5", stop="7") == [
        "5_download_pdfs", "6_convert_pdfs", "7_install_structured"]


def test_selecting_pass2_brings_prerequisites_in_run_order(tmp_path):
    proj = _project(tmp_path)
    assert _run_ids(proj, only=["9_pass2_rct"]) == [
        "5_download_pdfs", "6_convert_pdfs", "7_install_structured",
        "8_extract_pass1", "stage_eligible", "9_pass2_rct"]
    # Starting at pass 1 still downloads/converts first, never after.
    ids = _run_ids(proj, start="10")
    assert ids[:6] == ["5_download_pdfs", "6_convert_pdfs", "7_install_structured",
                       "8_extract_pass1", "stage_eligible", "9_pass2_rct"]


def test_selection_by_number_and_unknown_selector(tmp_path):
    proj = _project(tmp_path)
    assert _run_ids(proj, stop="9") == ["1_search", "2_fetch_abstracts", "3_classify_quick",
                                        "3_classify_screen", "4_enrich_screening"]
    assert _run_ids(proj, only=["no_such_step"]) == []


# ── B36: freshness ─────────────────────────────────────────────────────────

def test_freshness_is_a_warning_by_default(tmp_path):
    out = tmp_path / "out.csv"
    out.write_text("doi\n10.1/a\n")
    old = time.time() - 3600
    os.utime(out, (old, old))
    step = pipeline.Step("x", 1, "x", "x.py", [], produces=[str(out)])
    started = time.time()
    assert verify_produces(step) is None
    assert verify_produces(step, started) is None                  # default: not fatal
    assert "not updated" in verify_produces(step, started, require_fresh=True)
    assert stale_outputs(step, started) == [out]
    assert "no declared output was updated" in freshness_warning(step, started)
    out.write_text("doi\n10.1/a\n10.1/b\n")
    assert freshness_warning(step, started) is None


# ── B19: decision files fail loudly ────────────────────────────────────────

def test_decision_files_fail_loud_on_corruption(tmp_path):
    path = tmp_path / "user_exclusions.json"
    assert load_exclusion_dois(path) == set()             # missing: no decisions
    path.write_text("")
    assert load_exclusion_dois(path) == set()             # empty: no decisions
    path.write_text('{"10.1/A": "off topic", "_note": "x"}')
    assert load_exclusion_dois(path) == {"10.1/a"}
    path.write_text("{truncated")
    with pytest.raises(CorruptDecisionFile, match="user_exclusions.json"):
        load_exclusion_dois(path)
    path.write_text('["10.1/a"]')
    with pytest.raises(CorruptDecisionFile):
        load_inclusion_dois(path)
