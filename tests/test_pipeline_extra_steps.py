"""Config-declared ``extra_steps`` replace the old per-slug special case."""
from pathlib import Path

import pytest

from ber_core import pipeline
from ber_core import projects as proj_lib


def _project(tmp_path, extra):
    cfg_dir = tmp_path / "review"
    cfg_dir.mkdir()
    (cfg_dir / "derive.py").write_text("print('ok')\n")
    cfg = {"topic": {"name": "Fixture topic"}, "extra_steps": extra}
    return proj_lib.Project(slug="fixture_topic", config_path=cfg_dir / "config.yaml",
                            config=cfg, project_dir=tmp_path / "projects" / "fixture_topic")


def test_extra_step_runs_before_export_with_script_relative_to_config(tmp_path):
    project = _project(tmp_path, [{"id": "derive", "script": "derive.py", "label": "Derive"}])
    steps = pipeline.canonical_steps(project)
    ids = [s.id for s in steps]
    assert "extra:derive" in ids
    assert ids.index("extra:derive") < ids.index("13_build_bundle")
    step = steps[ids.index("extra:derive")]
    assert Path(step.script) == (tmp_path / "review" / "derive.py").resolve()
    assert step.args[0] == "--config"


def test_no_extra_steps_by_default(tmp_path):
    project = _project(tmp_path, [])
    assert not [s for s in pipeline.canonical_steps(project) if s.id.startswith("extra:")]


@pytest.mark.parametrize("entry", [{"id": "x"}, {"script": "derive.py"}, {"id": "x", "script": "missing.py"}])
def test_malformed_extra_step_fails_loudly(tmp_path, entry):
    with pytest.raises((ValueError, FileNotFoundError)):
        pipeline.extra_steps(_project(tmp_path, [entry]))
