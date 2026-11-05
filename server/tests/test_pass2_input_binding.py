"""The pass-2 steps must bind ``--input <eligible bucket>`` in their argv.

Companion to ``test_pass2_input_gate.py`` (which pins the SCRIPT's behaviour).
This pins the other half of the fix: the canonical step definitions in
``pipeline.py`` pass the staged bucket explicitly, so the dependency on
``stage_eligible.py``'s output lives in the argv — visible in logs and job
records — rather than in an optional ``dirs.*`` config key nothing sets.

If someone removes the ``--input`` binding, the script's own bucket default
would still save studio runs, but a hand-written config with a stale
``dirs.eligible_trials`` would silently win again. Both layers stay pinned.
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_pass2_argv_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import pipeline_bridge as pb  # noqa: E402
from server.app.services import workspace as ws  # noqa: E402

_counter = itertools.count()


def _workspace(**overrides) -> Path:
    n = next(_counter)
    cfg = ws.build_config_dict(
        topic_name=f"Pass2 Argv Test {n}",
        synonyms=["thing"],
        description="test",
        not_about=[],
        include_observational=overrides.pop("include_observational", True),
    )
    cfg.update(overrides)
    return ws.create_review_workspace(f"pass2argv{n}", cfg)


def _step_args(workspace: Path, step_id: str) -> list[str]:
    steps = pb.canonical_steps_for(workspace)
    step = next(s for s in steps if s.id == step_id)
    return list(step.args)


def _input_value(args: list[str]) -> str:
    assert "--input" in args, f"--input missing from argv: {args}"
    return args[args.index("--input") + 1]


def test_rct_step_binds_the_rct_bucket() -> None:
    args = _step_args(_workspace(), "9_pass2_rct")
    value = _input_value(args)
    assert Path(value).name == "eligible_rct", args
    assert Path(value).is_absolute(), (
        "the bucket path must be absolute so the subprocess CWD is irrelevant")


def test_observational_step_binds_the_observational_bucket() -> None:
    args = _step_args(_workspace(include_observational=True),
                      "9_pass2_observational")
    value = _input_value(args)
    assert Path(value).name == "eligible_observational", args


def test_no_pass2_step_points_at_converted() -> None:
    """The regression itself: converted/ must never appear in pass-2 argv."""
    workspace = _workspace(include_observational=True)
    for step in pb.canonical_steps_for(workspace):
        if not step.id.startswith("9_pass2"):
            continue
        for arg in step.args:
            assert Path(str(arg)).name != "converted", (
                f"step {step.id} argv references converted/: {step.args}")
