"""Regression fixtures for the codebase review; no research or network access."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ber_core.bundles import build_snapshot, approval_digest, source_fingerprint, INPUTS
from ber_core.evidence import drop_duplicate_records
from ber_core.environment import load_local_env, enforce_credentials
from ber_core.search import QueryLedger
from ber_core.projects import Project
from ber_core.storage import WorkspaceContext
from ber_core import grading
from ber_core import pipeline
import run_pipeline
from ber_core import claude_cli


def load_script(filename):
    spec = importlib.util.spec_from_file_location("regression_" + Path(filename).stem, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(pid, status="ok"):
    return {"paper_id": pid, "_status": status,
            "study_design": {"design_type": "RCT", "arms": [
                {"arm_id": 1, "arm_type": "intervention"},
                {"arm_id": 2, "arm_type": "placebo"}]}, "outcomes": []}


@pytest.mark.parametrize("status", ["error", "truncated", "parse_error"])
def test_failed_canonical_does_not_replace_usable_preprint(status):
    export = load_script("steps/export_dashboard.py")
    old, new = record("10.1000/preprint"), record("10.1000/published", status)
    mapping = {"10.1000/preprint": "10.1000/published"}
    kept, _ = export._union_records([old, new], [], mapping)
    assert old in kept
    assert old in drop_duplicate_records([old, new], mapping)
    kept, _ = export._union_records([old, record("10.1000/published")], [], mapping)
    assert old not in kept
    kept, _ = export._union_records([old, record("10.1000/published")], [], mapping,
                                    excluded={"10.1000/published"})
    assert old in kept


def test_stats_consumers_choose_endpoint_and_orient_reversed_comparison():
    roles = {1: "intervention", 2: "control"}
    r = {"paper_id": "test", "synthesis_population": "adults", "study_design": {"design_type": "RCT", "arms": [{"arm_id": 1,"type": "intervention","intervention_name": "A"},{"arm_id": 2,"type": "placebo"}]}}
    outcome = {"name": "Function", "smd_construct": "function", "analysis_population": "ITT", "higher_is_better": True, "timepoints": [
        {"timepoint_id": 0, "label": "baseline", "weeks_from_randomization": 0},
        {"timepoint_id": 1, "label": "week 8", "weeks_from_randomization": 8}],
        "arm_results": [{"arm_id": a, "timepoint_id": t, "mean": v, "sd": 2,
                         "n_analyzed": 50, "result_type": "endpoint", "n_unit": "participants", "mean_type": "raw", "uncertainty_type": "sd", "analysis_population": "ITT"}
                        for a, t, v in [(1, 0, 10), (2, 0, 10), (1, 1, 14), (2, 1, 10)]]}
    expected = grading.smd_from_outcome(outcome, roles, r)
    assert expected[0] > 1.9
    outcome["arm_results"] = outcome["arm_results"][:2]
    assert grading.smd_from_outcome(outcome, roles, r) is None
    outcome = {"higher_is_better": True, "between_group_effects": [
        {"comparison": "arm2_vs_arm1", "effect_measure": "odds_ratio", "effect_value": 2, "se": .2}]}
    assert grading.smd_from_outcome(outcome, roles, {}) is None  # ratio-to-SMD is prohibited


def test_failed_query_cannot_be_marked_complete():
    ledger = QueryLedger()
    ledger.begin("pubmed:x")
    ledger.fail("page 2 failed")
    assert not ledger.complete("pubmed:x")
    assert ledger.incomplete
    ledger.begin("pubmed:x")
    assert ledger.complete("pubmed:x")
    assert not ledger.incomplete


def test_snapshot_removes_stale_files_and_preserves_previous_on_failure(tmp_path):
    target = tmp_path / "dashboard"
    target.mkdir()
    (target / "meta_analysis.json").write_text("old")
    with pytest.raises(RuntimeError):
        with build_snapshot(target) as stage:
            (stage / "summary.json").write_text("partial")
            raise RuntimeError("failed export")
    assert (target / "meta_analysis.json").read_text() == "old"
    with build_snapshot(target) as stage:
        (stage / "summary.json").write_text("new")
    assert not (target / "meta_analysis.json").exists()
    assert (target / "summary.json").read_text() == "new"


@pytest.mark.parametrize("name", INPUTS)
def test_every_export_input_participates_in_freshness(tmp_path, name):
    before = source_fingerprint(tmp_path, {})
    (tmp_path / name).write_text("changed")
    assert source_fingerprint(tmp_path, {}) != before


def test_approval_binds_all_candidate_and_live_contents(tmp_path):
    candidate, live = tmp_path / "candidate", tmp_path / "live"
    candidate.mkdir(); live.mkdir()
    for folder in (candidate, live):
        path = folder / "meta_analysis.json"
        path.write_text("old")
        stamp = path.stat()
        token = approval_digest(candidate, live, "x")
        path.write_text("new")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert token != approval_digest(candidate, live, "x")


def test_cli_prerequisites_and_empty_bucket_share_contract(tmp_path):
    project = Project("x", tmp_path / "config.yaml", {"topic": {"name": "X"}}, tmp_path / "projects/x")
    canonical = pipeline.canonical_steps(project)
    selected = [s for s in canonical if s.id.startswith("9_pass2")]
    steps = pipeline.execution_steps(project, selected)
    assert [s.script for s in steps[:5]] == ["download_pdfs.py", "convert_pdfs.py",
        "install_structured.py", "extract_pass1.py", "stage_eligible.py"]
    assert sum(s.script == "extract_pass1.py" for s in steps) == 1
    bucket = project.project_dir / "eligible_rct"; bucket.mkdir(parents=True)
    assert run_pipeline._verify_produces(selected[0]) is None
    assert pipeline.execution_steps(project, []) == []


def test_custom_store_and_legacy_text_coexist(tmp_path):
    from ber_core import paper_store
    external = tmp_path / "external"
    project = tmp_path / "project"
    for path in [external / "Clinical_Trial/10.1000--new", project / "converted/10.1000--old"]:
        path.mkdir(parents=True); (path / "body.md").write_text("text")
    WorkspaceContext(project, tmp_path / "config.yaml", {}).remember_store(external)
    assert paper_store.papers_root(project) == external
    assert {r.paper_id for r in paper_store.discover_project_papers(project)} == {"10.1000/new", "10.1000/old"}


def test_managed_run_cannot_rehydrate_operator_key(tmp_path, monkeypatch):
    path = tmp_path / ".env.local"; path.write_text("ANTHROPIC_API_KEY=operator-placeholder\n")
    monkeypatch.setenv("BER_MANAGED_RUN", "1")
    assert load_local_env(path) is False
    env = {"BER_MANAGED_RUN": "1", "ANTHROPIC_API_KEY": "operator-placeholder",
           "CLAUDE_CODE_OAUTH_TOKEN": "tenant-placeholder"}
    assert "ANTHROPIC_API_KEY" not in enforce_credentials(env, "claude_token")
    with pytest.raises(ValueError):
        enforce_credentials({"BER_MANAGED_RUN": "1"}, "anthropic_key")


def test_auth_failure_does_not_escape_credential_scope():
    with claude_cli.llm_context("anthropic_key", {"ANTHROPIC_API_KEY": "a"}):
        claude_cli._auth_state()["error"] = "rejected"
        assert claude_cli._auth_state()
    with claude_cli.llm_context("anthropic_key", {"ANTHROPIC_API_KEY": "b"}):
        assert not claude_cli._auth_state()


def test_async_timeout_kills_and_reaps_child(monkeypatch):
    class Child:
        returncode = None
        killed = False
        waited = False
        async def communicate(self, input=None):
            await asyncio.sleep(5)
        def kill(self):
            self.killed = True
        async def wait(self):
            self.waited = True
            self.returncode = -9
    child = Child()
    async def spawn(*args, **kwargs):
        return child
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn)
    response = asyncio.run(claude_cli._async_cli_call_claude("fixture", "sonnet", "fixture", timeout=.01))
    assert child.killed and child.waited
    assert not response.text


def test_failed_search_retains_checkpoint_and_partial_source_does_not_advance_it(tmp_path, monkeypatch):
    search = load_script("steps/search_papers.py")
    progress = {"completed_queries": [], "last_run_date": "2000-01-01"}
    monkeypatch.setattr(search, "load_config", lambda _: {"topic": {"name": "Fixture"},
        "search": {"pubmed": ["x"], "europepmc": ["x"]}})
    monkeypatch.setattr(search, "configure_project_paths", lambda _: None)
    monkeypatch.setattr(search, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(search, "DATA_DIR", tmp_path)
    monkeypatch.setattr(search, "load_progress", lambda: progress)
    monkeypatch.setattr(search, "save_progress", lambda _: None)
    for name in ["_load_existing_abstract_log", "enrich_missing_dois", "produce_final_csv"]:
        monkeypatch.setattr(search, name, lambda: None)
    monkeypatch.setattr(search, "load_seen_from_csv", lambda: 0)
    monkeypatch.setattr(search, "ENTREZ_EMAIL", "fixture@example.org")
    args = SimpleNamespace(max_per_query=10, since="2000-01-01", fresh=False, config="fixture",
        backfill_cyberleninka_abstracts=False, sources="pubmed", parallel=False)
    def fail(_):
        raise RuntimeError("synthetic outage")
    monkeypatch.setattr(search, "run_pubmed_searches", fail)
    assert search.main(args) == 2
    assert progress["last_run_date"] == "2000-01-01"
    monkeypatch.setattr(search, "run_pubmed_searches", lambda _: 0)
    assert search.main(args) == 0
    assert progress["last_run_date"] == "2000-01-01"  # Europe PMC was not covered


def test_real_export_rebuild_and_observational_only_bucket(tmp_path):
    projects = tmp_path / "projects"
    project = projects / "fixture"
    data = project / "data"; data.mkdir(parents=True)
    config = tmp_path / "config.yaml"; config.write_text("topic:\n  name: Fixture\n")
    (data / "observational_extractions.jsonl").write_text(json.dumps(record("10.1000/observational")) + "\n")
    env = dict(os.environ, BER_PROJECTS_DIR=str(projects), BER_MANAGED_RUN="1")
    command = [sys.executable, "steps/export_dashboard.py", "--config", str(config),
               "--no-metadata-fetch", "--allow-shrink"]
    first = subprocess.run([*command, "--meta-analysis"], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    bundle = project / "dashboard"
    assert (bundle / "meta_analysis.json").exists()
    assert "10.1000/observational" in (bundle / "trial_extractions.jsonl").read_text()
    (bundle / "stale_optional.json").write_text("old")
    second = subprocess.run(command, env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert not (bundle / "meta_analysis.json").exists()
    assert not (bundle / "stale_optional.json").exists()
    assert (bundle / "bundle.inputs.json").exists()
