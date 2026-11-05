"""The explicit CLI model must reach every assessment stage and its provenance."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def workflow():
    spec = importlib.util.spec_from_file_location('inspect_cli_test', Path(__file__).parents[1] / 'steps/systematic_review.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('requested,expected', [('luna', 'gpt-5.6-luna'), ('terra', 'gpt-5.6-terra')])
def test_explicit_model_reaches_all_stages_and_manifest(tmp_path, monkeypatch, requested, expected):
    module = workflow()
    project = SimpleNamespace(project_dir=tmp_path / 'me_cfs', config={'topic': {'name': 'ME/CFS'}})
    record = {'paper_id': '10.test/a', 'selection_status': 'candidate', 'texts': {'body': 'Source'}}
    monkeypatch.setenv('BER_LLM_PROVIDER', 'codex_cli')
    monkeypatch.setattr(module, 'load_project', lambda _: project)
    monkeypatch.setattr(module, 'collect_corpus', lambda *a: ([record], {'counts': {'union_reports': 1}}))
    monkeypatch.setattr(module, 'enrich_corpus', lambda records, *a, **kw: records)
    monkeypatch.setattr(module, 'prepare_visuals', lambda records, *a: records)
    seen = {}
    def capture(stage, result):
        def call(*a, **kw):
            seen[stage] = kw['model']
            return result
        return call
    monkeypatch.setattr(module, 'assess', capture('assessment', {}))
    monkeypatch.setattr(module, 'audit_scope', capture('scope', {}))
    monkeypatch.setattr(module, 'adjudicate', lambda *a, **kw: {})
    monkeypatch.setattr(module, 'write_manuscript', capture('manuscript', {}))
    monkeypatch.setattr(module, 'dashboard_assessments', capture('dashboard', {}))
    out = tmp_path / 'output'
    assert module.main(['--config', 'unused', '--out-dir', str(out), '--offline', '--model', requested]) == 0
    assert seen == {k: expected for k in ('assessment', 'scope', 'manuscript', 'dashboard')}
    assert json.loads((out / 'manifest.json').read_text())['model'] == expected


def test_unsupported_model_cannot_silently_use_default(tmp_path):
    with pytest.raises(SystemExit) as exc:
        workflow().main(['--config', 'unused', '--out-dir', str(tmp_path), '--model', 'unknown'])
    assert exc.value.code == 2
