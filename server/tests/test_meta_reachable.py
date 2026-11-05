"""Meta-analysis must be reachable from a server-launched run.

It was not. `worker.py` calls `runner.execute_pipeline`, whose signature carries
no `generate_meta_analysis` kwarg, and `execute_pipeline` calls
`canonical_steps_for(workspace, since=..., fresh=...)`. The bridge *accepted* the
flag and forwarded it, but nothing ever passed True — so
`pipeline.canonical_steps`'s `if generate_meta_analysis: args.append("--meta-analysis")`
never fired outside the CLI. Every server-built bundle silently lacked
`meta_analysis.json`, and the wizard's meta stage could not ship.

The fix reads it from config inside `canonical_steps_for`, exactly as
`include_observational` already is. These tests pin the behaviour that matters:
the flag reaches the step's argv, and it does so on EVERY path that builds
steps — including resumes and single-step runs, which is why a per-run kwarg
would have been the wrong fix.
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_meta_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import pipeline_bridge as pb  # noqa: E402
from server.app.services import workspace as ws  # noqa: E402

_counter = itertools.count()

BUNDLE_STEP = "13_build_bundle"


def _workspace(**overrides) -> Path:
    n = next(_counter)
    cfg = ws.build_config_dict(
        topic_name=f"Meta Test {n}",
        synonyms=["thing"],
        description="test",
        not_about=[],
        include_observational=overrides.pop("include_observational", True),
        generate_meta_analysis=overrides.pop("generate_meta_analysis", False),
    )
    cfg.update(overrides)
    return ws.create_review_workspace(f"metatest{n}", cfg)


def _bundle_args(workspace: Path, **opts) -> list[str]:
    steps = pb.canonical_steps_for(workspace, **opts)
    step = next(s for s in steps if s.id == BUNDLE_STEP)
    return list(step.args)


# --- the regression ----------------------------------------------------------

def test_config_flag_reaches_the_bundle_steps_argv() -> None:
    """The whole bug in one assertion: without this, no server run ever pooled."""
    assert "--meta-analysis" in _bundle_args(_workspace(generate_meta_analysis=True))


def test_pooling_is_off_by_default() -> None:
    """Most topics cannot pool (nattokinase: 0 of 61 outcomes). Generating a
    forest plot nobody asked for is the over-claiming failure, so the default
    must stay off."""
    assert "--meta-analysis" not in _bundle_args(_workspace())


def test_a_config_written_before_the_flag_existed_still_works() -> None:
    """Reviews created earlier have no such key. Absence must read as False, not
    raise — there is no migration step in this codebase."""
    workspace = _workspace()
    cfg_path = workspace / "config.yaml"
    import yaml

    cfg = yaml.safe_load(cfg_path.read_text())
    del cfg["generate_meta_analysis"]
    cfg_path.write_text(yaml.safe_dump(cfg))

    assert "--meta-analysis" not in _bundle_args(workspace)


# --- why config, and not a per-run kwarg -------------------------------------

def test_the_flag_survives_a_resume() -> None:
    """`13_build_bundle` re-runs on every resume. A per-run kwarg would let a
    resumed run rebuild the bundle WITHOUT meta_analysis.json, silently
    replacing a dashboard that had a forest plot with one that does not."""
    workspace = _workspace(generate_meta_analysis=True)
    # A resume passes only since/fresh — exactly what execute_pipeline forwards.
    assert "--meta-analysis" in _bundle_args(workspace, since="2020-01-01")
    assert "--meta-analysis" in _bundle_args(workspace, fresh=True)


def test_the_flag_survives_a_single_step_run() -> None:
    """`runner.execute_step` resolves via `find_step`, which passes no options.
    A per-run kwarg would leave single-step runs of the bundle permanently
    meta-less."""
    workspace = _workspace(generate_meta_analysis=True)
    step = pb.find_step(workspace, BUNDLE_STEP)
    assert step is not None
    assert "--meta-analysis" in step.args


def test_an_explicit_kwarg_still_wins_for_cli_callers() -> None:
    """The kwarg stays so run_pipeline.py is untouched.
    Config is a fallback, never an override."""
    assert "--meta-analysis" in _bundle_args(
        _workspace(), generate_meta_analysis=True
    )


# --- turning pooling on AFTER creation ---------------------------------------

def test_the_flag_can_be_turned_on_after_the_review_exists() -> None:
    """The wizard's "Pool these trials" button PATCHes this flag mid-review.

    It had to: you only learn pooling is feasible once extraction has run, but
    the flag was previously settable only at creation — so every review that
    turned out poolable was stuck with no way to pool it. `normalize_config`
    must therefore preserve a later-added flag rather than resetting it to the
    creation-time default.
    """
    from server.app.services import workspace as ws

    # A review created WITHOUT pooling (the default) …
    created = ws.normalize_config({"topic": {"name": "X"}})
    assert not created.get("generate_meta_analysis")

    # … then switched on later, exactly as the config PATCH does.
    patched = ws.normalize_config({**created, "generate_meta_analysis": True})
    assert patched["generate_meta_analysis"] is True

    # And that lands in the argv the bundle step actually runs with.
    assert "--meta-analysis" in _bundle_args(_workspace(**patched))


def test_the_meta_stage_owns_the_flag_so_toggling_it_stales_only_that_stage() -> None:
    """Scoped staleness: `config_keys` decides which stage a config edit
    invalidates. If no stage owned `generate_meta_analysis`, toggling pooling
    would either invalidate nothing or (worse) something unrelated."""
    from server.app.services.stages import STAGES

    owners = [s.slug for s in STAGES if "generate_meta_analysis" in s.config_keys]
    assert owners == ["meta"], owners
