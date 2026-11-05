"""synthesize_review.py — the deterministic layer (no LLM).

Covers the pieces that must hold regardless of what the model writes:
- the depth → default-section mapping and its stable IDs;
- the number-verification pass (payload numbers pass, invented numbers drop the
  section to its template fallback);
- a full ``--no-llm`` run over a fixture bundle produces a document + sidecar
  whose tables carry the payload's numbers.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from ber_core.scientific import RULES_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "synthesize_review", REPO_ROOT / "ber_core/synthesize_review.py")
synth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(synth)


# ── Sections / depth ─────────────────────────────────────────────────────────

def test_default_sections_nest_by_depth() -> None:
    brief = synth.default_sections("brief")
    standard = synth.default_sections("standard")
    deep = synth.default_sections("deep")
    assert set(brief) < set(standard) < set(deep)
    assert list(deep) == list(synth.SECTION_IDS), "deep enables every section"
    # Document order is the registry order, whatever the depth.
    assert standard == [s for s in synth.SECTION_IDS if s in standard]


def test_every_section_has_a_template() -> None:
    """The template map is the --no-llm document AND the fallback — a section
    without one crashes exactly when the safety net is needed."""
    assert set(synth.TEMPLATES) == set(synth.SECTION_IDS)


# ── Number verification ──────────────────────────────────────────────────────

def test_payload_numbers_are_allowed_including_derived_forms() -> None:
    allowed = synth.allowed_numbers(
        {"totals": {"n_trials": 426, "p": 0.42}, "eff": -1.25})
    assert synth.orphan_numbers(
        "Across 426 trials the effect was -1.25 (p = 0.42, i.e. 42%).",
        allowed) == []
    # Small counts and CI conventions never trip the check.
    assert synth.orphan_numbers("In 3 of the trials (95% CI)…", allowed) == []


def test_invented_numbers_are_orphans() -> None:
    allowed = synth.allowed_numbers({"totals": {"n_trials": 426}})
    assert synth.orphan_numbers(
        "A striking 87.3% improved.", allowed) == ["87.3"]


def test_singleshot_falls_back_on_invented_numbers(monkeypatch) -> None:
    """An LLM draft citing a number absent from the payload loses that section
    to the deterministic template; honest sections survive."""
    payload = {
        "topic": {"name": "T", "description": "d"},
        "totals": {"n_trials": 12},
        "feasibility": {"recommendation": "No pooling."},
        "pooled_groups": [],
    }
    doc = (
        "## Key findings\n\nOf the 12 studies, most were inconclusive.\n\n"
        "## Limitations\n\nExactly 999 papers were unreadable.\n"
    )
    monkeypatch.setattr(
        synth, "call_claude",
        lambda *a, **k: type("R", (), {"text": doc})())
    monkeypatch.setenv("BER_PROMPTS_DIR", str(REPO_ROOT / "prompts"))
    sections = synth.generate_singleshot(
        payload, ["key_findings", "limitations"], "brief", "clinician",
        "haiku", payload["topic"], "t")
    assert sections["key_findings"]["source"] == "llm"
    assert sections["limitations"]["source"] == "template_fallback"
    assert "999" not in sections["limitations"]["body"]


# ── Full --no-llm run over a fixture bundle ──────────────────────────────────

def test_no_llm_run_writes_document_and_sidecar(tmp_path) -> None:
    slug = "fixture_topic"
    projects = tmp_path / "projects"
    dash = projects / slug / "dashboard"
    dash.mkdir(parents=True)
    (dash / "summary.json").write_text(json.dumps({
        "scientific_rules_version": RULES_VERSION,
        "totals": {"n_trials": 7, "n_rct": 5, "n_observational": 2,
                   "total_participants": 314},
        "charts": {"by_intervention": [
            {"name": "widgetol", "count": 7,
             "verdicts": {"favors_treatment": 3, "no_difference": 4}}]},
        "feasibility": {"recommendation": "Pooling is supported.",
                        "domains": [], "blockers": []},
    }), encoding="utf-8")
    (dash / "dashboard_rows.jsonl").write_text(json.dumps({
        "id": "10.1/x", "title": "Widgetol RCT", "year": 2021, "n": 100,
        "design_type": "RCT", "verdict": "favors_treatment",
    }) + "\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("topic:\n  name: Fixture Topic\n  description: a test\n",
                   encoding="utf-8")

    from ber_core.bundles import source_fingerprint, INPUT_MANIFEST
    import yaml
    (dash / INPUT_MANIFEST).write_text(json.dumps(source_fingerprint(projects / slug / "data", yaml.safe_load(cfg.read_text()))))
    env = dict(os.environ, BER_PROJECTS_DIR=str(projects))
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "steps/synthesize_review.py"),
         "--config", str(cfg), "--no-llm", "--depth", "brief"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr

    data = projects / slug / "data"
    doc = (data / "review_document.md").read_text(encoding="utf-8")
    assert "Fixture Topic" in doc
    assert "| Studies | 7 |" in doc
    meta = json.loads((data / "review_document.json").read_text(encoding="utf-8"))
    assert meta["depth"] == "brief"
    assert meta["llm_calls"] == 0
    assert all(s["source"] == "template" for s in meta["sections"])
    assert str(dash / "summary.json") in meta["source_fingerprint"]

    # Without --force a fresh document is kept, not rewritten.
    proc2 = subprocess.run(
        [sys.executable, str(REPO_ROOT / "steps/synthesize_review.py"),
         "--config", str(cfg), "--no-llm"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert proc2.returncode == 0
    assert "--force" in proc2.stdout


def test_sections_flag_filters_and_orders(tmp_path) -> None:
    slug = "fixture_topic"
    projects = tmp_path / "projects"
    dash = projects / slug / "dashboard"
    dash.mkdir(parents=True)
    (dash / "summary.json").write_text(
        json.dumps({"scientific_rules_version": RULES_VERSION, "totals": {"n_trials": 1}}), encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("topic:\n  name: Fixture Topic\n", encoding="utf-8")

    from ber_core.bundles import source_fingerprint, INPUT_MANIFEST
    import yaml
    (dash / INPUT_MANIFEST).write_text(json.dumps(source_fingerprint(projects / slug / "data", yaml.safe_load(cfg.read_text()))))
    env = dict(os.environ, BER_PROJECTS_DIR=str(projects))
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "steps/synthesize_review.py"),
         "--config", str(cfg), "--no-llm",
         "--sections", "limitations,key_findings"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr
    doc = (projects / slug / "data" / "review_document.md").read_text(
        encoding="utf-8")
    # Registry order wins over the order given on the CLI.
    assert doc.index("## Key findings") < doc.index("## Limitations")
    assert "## Pooled results" not in doc

    bad = subprocess.run(
        [sys.executable, str(REPO_ROOT / "steps/synthesize_review.py"),
         "--config", str(cfg), "--no-llm", "--sections", "bogus"],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert bad.returncode == 2
    assert "unknown section" in bad.stderr
