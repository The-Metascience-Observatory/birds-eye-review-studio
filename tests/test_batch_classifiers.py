"""Batch-classifier scaffolding, consolidation caching and indication routing."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ber_core import batch_classify as bc

ROOT = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeModel:
    """Stands in for claude_cli.call_claude; answers via a function of the prompt."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def __call__(self, prompt, model, label, timeout=None, use_stdin=True):
        self.prompts.append(prompt)
        return SimpleNamespace(text=json.dumps(self.answer(prompt)), cost_usd=0.01)


@pytest.fixture
def fake_model(monkeypatch):
    from ber_core import claude_cli

    def install(answer):
        fake = FakeModel(answer)
        monkeypatch.setattr(claude_cli, "call_claude", fake)
        monkeypatch.setattr(claude_cli, "parse_json_response",
                            lambda text, ident: json.loads(text) if text else None)
        return fake
    return install


def test_map_is_written_atomically_after_every_batch(tmp_path, fake_model):
    path = tmp_path / "m.json"
    snapshots = []
    fake_model(lambda prompt: {n: n.upper() for n in json.loads(prompt)})

    def accept(batch, parsed):
        return {n: parsed[n] for n in batch}

    store = {}
    orig = bc.write_map_atomic

    def spy(p, m, **kw):
        orig(p, m, **kw)
        snapshots.append(json.loads(Path(p).read_text()))
    bc.write_map_atomic, saved = spy, bc.write_map_atomic
    try:
        bc.classify_batches(["a", "b", "c"], batch_size=2, make_prompt=json.dumps, accept=accept,
                            model="m", label="t", store=store, store_path=path)
    finally:
        bc.write_map_atomic = saved
    assert snapshots == [{"a": "A", "b": "B"}, {"a": "A", "b": "B", "c": "C"}]
    assert not list(tmp_path.glob(".m.json.*"))  # no temp files left behind


def test_unparseable_batches_are_retried_then_left_for_next_run(tmp_path, fake_model):
    fake = fake_model(lambda prompt: {})  # {} is falsy -> treated as no JSON
    store = {}
    bc.classify_batches(["a"], batch_size=5, make_prompt=json.dumps, accept=lambda b, p: p,
                        model="m", label="t", store=store, store_path=tmp_path / "m.json",
                        retries=2)
    assert len(fake.prompts) == 2 and store == {}
    assert not (tmp_path / "m.json").exists()


def test_load_map_tolerates_garbage(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not json")
    assert bc.load_map(p) == {}
    p.write_text("[1, 2]")
    assert bc.load_map(p) == {}
    assert bc.load_map(tmp_path / "missing.json") == {}


def test_truncated_outer_mapping_retries_without_saving_nested_value(tmp_path, monkeypatch):
    from ber_core import claude_cli
    answer = {"10.1000/a": {"indication": "treatment"},
              "10.1000/b": {"indication": "exclude"}}
    replies = iter([json.dumps(answer)[:-1], json.dumps(answer)])
    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(text=next(replies), cost_usd=0.01)

    monkeypatch.setattr(claude_cli, "call_claude", call)
    store = {}
    path = tmp_path / "map.json"
    bc.classify_batches(list(answer), batch_size=2, make_prompt=json.dumps,
                        accept=lambda batch, parsed: {k: parsed[k] for k in batch if k in parsed},
                        model="m", label="indication", store=store, store_path=path)
    assert len(calls) == 2
    assert store == json.loads(path.read_text()) == answer


# ── 12_canonicalize: consolidation is cached ─────────────────────────────────

def _project(tmp_path, monkeypatch, names, cfg_extra=None):
    projects = tmp_path / "projects"
    data = projects / "topic_x" / "data"
    data.mkdir(parents=True)
    recs = [{"paper_id": f"10.1/{i}", "_status": "ok", "study_design": {"arms": [
        {"arm_id": 1, "type": "intervention", "intervention_name": n, "intervention_category": "other"}]}}
        for i, n in enumerate(names)]
    (data / "trial_extractions_pass2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    cfg = {"topic": {"name": "Topic X", "description": "d"},
           "intervention_categories": ["drug", "exercise", "other"], **(cfg_extra or {})}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    from ber_core import utils
    monkeypatch.setattr(utils, "_PROJECTS_DIR", projects, raising=False)
    monkeypatch.setenv("BER_PROJECTS_DIR", str(projects))
    return cfg_path, data


def _run(module, cfg_path, monkeypatch, *extra):
    from ber_core import claude_cli
    monkeypatch.setattr(claude_cli, "set_log_dest_dir", lambda p: None)
    monkeypatch.setattr(module, "set_log_dest_dir", lambda p: None, raising=False)
    monkeypatch.setattr(sys, "argv", ["x", "--config", str(cfg_path), *extra])
    return module.main()


def _names_in(prompt):
    start = prompt.rindex("[")
    return [e["name"] for e in json.loads(prompt[start:prompt.rindex("]") + 1])]


def test_consolidation_runs_only_for_new_labels(tmp_path, monkeypatch, fake_model):
    canon = _load("steps/canonicalize_interventions.py", "canon12")
    cfg_path, data = _project(tmp_path, monkeypatch, ["Metformin 500mg", "metformin"])
    fake = fake_model(lambda prompt: {n: "Metformin" for n in _names_in(prompt)})
    assert _run(canon, cfg_path, monkeypatch) == 0
    first_calls = len(fake.prompts)
    assert json.loads((data / "intervention_canonical_map.json").read_text()) == {
        "metformin": "Metformin", "Metformin 500mg": "Metformin"}
    # Unchanged corpus: no model call at all (consolidation cached).
    assert _run(canon, cfg_path, monkeypatch) == 0
    assert len(fake.prompts) == first_calls
    # A new name: pass-1 for it, then consolidation of only the new label,
    # sent alongside the settled label as an anchor; settled labels never move.
    rec = {"paper_id": "10.1/new", "_status": "ok", "study_design": {"arms": [
        {"arm_id": 1, "type": "intervention", "intervention_name": "Exercise"}]}}
    with open(data / "trial_extractions_pass2.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    fake.answer = lambda prompt: {n: ("Exercise training" if n.startswith("Exercise") else "RENAMED")
                                  for n in _names_in(prompt)}
    assert _run(canon, cfg_path, monkeypatch) == 0
    final = json.loads((data / "intervention_canonical_map.json").read_text())
    assert final["Exercise"] == "Exercise training"
    assert final["metformin"] == "Metformin"   # anchor not renamed
    cons = json.loads((data / "intervention_canonical_consolidation.json").read_text())
    assert cons["Exercise training"] == "Exercise training" and cons["Metformin"] == "Metformin"


def test_categorize_uses_config_vocabulary_and_remembers_other(tmp_path, monkeypatch, fake_model):
    cat = _load("steps/categorize_interventions.py", "cat_mod")
    cfg_path, data = _project(tmp_path, monkeypatch, ["Metformin", "Viral challenge", "Yoga"])
    fake = fake_model(lambda prompt: {"Metformin": "drug", "Viral challenge": "other",
                                      "Yoga": "vaccination"})
    assert _run(cat, cfg_path, monkeypatch) == 0
    assert "- drug\n- exercise\n- other" in fake.prompts[0]
    assert json.loads((data / "intervention_category_map.json").read_text()) == {"Metformin": "drug"}
    unresolved = json.loads((data / "intervention_category_unresolved.json").read_text())
    assert unresolved == {"Viral challenge": "other", "Yoga": "vaccination"}
    n = len(fake.prompts)
    assert _run(cat, cfg_path, monkeypatch) == 0
    assert len(fake.prompts) == n  # nothing re-sent


# ── Indication: generic prompt, per-project override, routing ───────────────

def test_generic_indication_prompt_has_no_topic_framing():
    text = (ROOT / "prompts" / "classify_study_indication.txt").read_text()
    assert "covid" not in text.lower() and "pasc" not in text.lower()
    for ph in ("{topic_name}", "{topic_description}", "{condition_field}", "{studies_json}"):
        assert ph in text


def test_a_per_project_prompt_overrides_the_default(tmp_path, monkeypatch):
    from ber_core.utils import load_prompt_template
    monkeypatch.delenv("BER_PROMPTS_DIR", raising=False)
    (tmp_path / "demo_topic").mkdir()
    (tmp_path / "demo_topic" / "classify_study_indication.txt").write_text("DEMO OVERRIDE")
    (tmp_path / "classify_study_indication.txt").write_text("DEFAULT")
    assert load_prompt_template("classify_study_indication.txt", tmp_path, slug="demo_topic") == "DEMO OVERRIDE"
    assert load_prompt_template("classify_study_indication.txt", tmp_path, slug="other") == "DEFAULT"


def test_condition_field_comes_from_config_or_the_corpus(tmp_path):
    ind = _load("steps/classify_intervention_indication.py", "ind_mod")
    (tmp_path / "trial_extractions_pass2.jsonl").write_text(
        json.dumps({"participants": {"long_covid_definition": "WHO"}}) + "\n")
    assert ind.detect_condition_field(tmp_path, None) == "long_covid_definition"
    assert ind.detect_condition_field(tmp_path, "rls_definition") == "rls_definition"
    (tmp_path / "trial_extractions_pass2.jsonl").write_text(json.dumps({"participants": {}}) + "\n")
    assert ind.detect_condition_field(tmp_path, None) == "condition_definition"


def test_indication_hints_join_encoded_dois_and_pubmed_records(tmp_path):
    ind = _load("steps/classify_intervention_indication.py", "ind_hint_mod")
    (tmp_path / "trial_screening.csv").write_text(
        "doi,pmid,studies_treatment\n"
        "10.1000--EXAMPLE,,Yes\n"
        "https://doi.org/10.1000/second,,No\n"
        ",12345,Unclear\n"
        ",,Yes\n", encoding="utf-8")
    hints = ind._load_treatment_hint(tmp_path)
    assert hints == {"10.1000/example": "yes", "10.1000/second": "no",
                     "pmid_12345": "unclear"}
    assert hints[ind.join_key("10.1000/EXAMPLE#arm2")] == "yes"


def test_indication_routing_follows_the_configured_focus():
    exp = _load("steps/export_dashboard.py", "export13_routing")
    recs = [{"paper_id": p} for p in ("t", "p", "b", "x", "u", "10.1/T#arm2")]
    ind = {"t": "treatment", "p": "prevention", "b": "both", "x": "exclude", "10.1/t": "prevention"}
    ids = lambda rs: [r["paper_id"] for r in rs]
    feed, side, kind, n_x = exp.route_by_indication(recs, ind, "treatment")
    assert ids(feed) == ["t", "b", "u"] and ids(side) == ["p", "b", "10.1/T#arm2"]
    assert kind == "prevention" and n_x == 1
    feed, side, kind, _ = exp.route_by_indication(recs, ind, "prevention")
    assert ids(feed) == ["p", "b", "u", "10.1/T#arm2"] and ids(side) == ["t", "b"] and kind == "treatment"
    feed, side, kind, _ = exp.route_by_indication(recs, ind, "all")
    assert ids(feed) == ["t", "p", "b", "u", "10.1/T#arm2"] and side == [] and kind is None
    assert exp.indication_focus({}) == ("treatment", False)
    assert exp.indication_focus({"topic": {"indication_focus": "All"}}) == ("all", True)
