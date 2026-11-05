"""An empty staged bucket is a no-op, not an unfinished (or failed) step.

The bug this pins (nattokinase review): `include_observational: true` keeps
`9_pass2_observational` canonical, but the corpus staged zero observational
papers — so the step's declared output never exists. `_step_status` read that
as `pending`, the extract stage rolled up `partial` forever, and the wizard's
`current_stage` (and the timeline's you-are-here marker) stayed pinned on
Extract for a review the user had finished end-to-end. The runner had the twin
problem: `verify_produces` would fail the next Extract run with "expected
output ... (missing)".

Both layers now recognize the case by the step's bound `--input` bucket:
EXISTS AND EMPTY -> nothing was routed to this design -> `n/a` status and a
tolerated verify. MISSING stays pending/failing — that means staging never ran.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_bucket_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import runner  # noqa: E402
from server.app.services import stages as st  # noqa: E402
from ber_core.execution import input_bucket_empty  # noqa: E402


class _Step:
    def __init__(self, produces: list[str], args: list[str]):
        self.produces = produces
        self.args = args
        self.id = "9_pass2_observational"


def _step(tmp: Path, *, bucket: str | None) -> _Step:
    out = tmp / "observational_extractions.jsonl"
    args = ["--config", "x.yaml", "--design-type", "observational"]
    if bucket is not None:
        args += ["--input", str(tmp / bucket)]
    return _Step(produces=[str(out)], args=args)


# --- stages._step_status -----------------------------------------------------

def test_existing_empty_bucket_is_na(tmp_path: Path) -> None:
    (tmp_path / "eligible_observational").mkdir()
    assert st._step_status(_step(tmp_path, bucket="eligible_observational")) == "n/a"


def test_missing_bucket_stays_pending(tmp_path: Path) -> None:
    assert st._step_status(_step(tmp_path, bucket="eligible_observational")) == "pending"


def test_populated_bucket_with_output_is_done(tmp_path: Path) -> None:
    bucket = tmp_path / "eligible_observational"
    (bucket / "10.1--x").mkdir(parents=True)
    (tmp_path / "observational_extractions.jsonl").write_text('{"a":1}\n')
    assert st._step_status(_step(tmp_path, bucket="eligible_observational")) == "done"


def test_populated_bucket_without_output_stays_pending(tmp_path: Path) -> None:
    bucket = tmp_path / "eligible_observational"
    (bucket / "10.1--x").mkdir(parents=True)
    assert st._step_status(_step(tmp_path, bucket="eligible_observational")) == "pending"


def test_steps_without_input_are_untouched(tmp_path: Path) -> None:
    step = _step(tmp_path, bucket=None)
    assert st._step_status(step) == "pending"
    (tmp_path / "observational_extractions.jsonl").write_text('{"a":1}\n')
    assert st._step_status(step) == "done"


def test_na_rolls_the_stage_up_to_done() -> None:
    """The point of the fix: rct done + observational n/a must read done, so
    current_stage advances off Extract."""
    assert st._roll_up(["done", "n/a", "n/a"]) == "done"


# --- runner._input_bucket_empty / verify tolerance ---------------------------

def test_runner_tolerates_existing_empty_bucket(tmp_path: Path) -> None:
    (tmp_path / "eligible_observational").mkdir()
    step = _step(tmp_path, bucket="eligible_observational")
    assert input_bucket_empty(step) is True
    # CLI and Studio now share the same empty-input contract.
    assert runner.verify_produces(step) is None


def test_runner_does_not_tolerate_missing_bucket(tmp_path: Path) -> None:
    step = _step(tmp_path, bucket="eligible_observational")
    assert input_bucket_empty(step) is False
    assert runner.verify_produces(step) is not None


def test_runner_does_not_tolerate_populated_bucket(tmp_path: Path) -> None:
    bucket = tmp_path / "eligible_observational"
    (bucket / "10.1--x").mkdir(parents=True)
    step = _step(tmp_path, bucket="eligible_observational")
    assert input_bucket_empty(step) is False


def test_runner_ignores_steps_without_input(tmp_path: Path) -> None:
    assert input_bucket_empty(_step(tmp_path, bucket=None)) is False


def test_non_eligible_input_dirs_are_not_affected(tmp_path: Path) -> None:
    """The scope guard: classify binds --input abstracts/, and an empty
    abstracts dir means 'upstream hasn't run' — that must stay pending (and
    keep failing verify), never become n/a."""
    (tmp_path / "abstracts").mkdir()
    step = _Step(
        produces=[str(tmp_path / "classifications.csv")],
        args=["--config", "x.yaml", "--input", str(tmp_path / "abstracts")],
    )
    assert st._step_status(step) == "pending"
    assert input_bucket_empty(step) is False
