"""Machine-readable run summaries (`utils.emit_run_summary`).

Replaces recovering cost by regexing `cost: $X.YZ` out of job logs and taking the
LAST match — which reports a mid-run cumulative when a run is killed, and only
the final step's total when a pipeline job appends every step to one log.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core.utils import emit_run_summary, parse_run_summaries  # noqa: E402


def _emit(**kw) -> str:
    """Emit and return what landed on stderr."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        emit_run_summary(**kw)
    return buf.getvalue()


def test_counters_round_trip_through_the_log() -> None:
    counter = {"done": 556, "total": 556, "total_cost": 6.3835,
               "input_tokens": 10, "output_tokens": 5,
               "start": time.monotonic() - 12.0}
    out = _emit(step="3_classify_type", counter=counter, model="haiku")
    (got,) = parse_run_summaries(out)

    assert got["step"] == "3_classify_type"
    assert got["total_cost"] == 6.3835
    assert got["done"] == 556
    assert got["model"] == "haiku"
    assert got["elapsed_s"] >= 12.0
    assert "start" not in got, "the monotonic clock is meaningless to a reader"


def test_one_line_only_so_a_line_reader_can_find_it() -> None:
    """The server scans the log line by line; an embedded newline would split
    the JSON across lines and silently lose the summary."""
    out = _emit(
        step="s",
        counter={"done": 1, "note": "multi\nline\tvalue"},
    )
    payloads = [l for l in out.splitlines() if "RUN_SUMMARY" in l]
    assert len(payloads) == 1
    assert json.loads(payloads[0].split("RUN_SUMMARY ", 1)[1])["note"] == "multi\nline\tvalue"


def test_a_pipeline_log_yields_one_entry_per_step() -> None:
    """The exact case the old last-match regex got wrong."""
    log = "\n".join(
        [
            "  -- Progress: 100/556 | cost: $1.10 --",
            _emit(step="a", counter={"total_cost": 0.5}).strip(),
            "  -- Progress: 300/556 | cost: $3.00 --",
            _emit(step="b", counter={"total_cost": 6.3835}).strip(),
        ]
    )
    got = parse_run_summaries(log)
    assert [g["step"] for g in got] == ["a", "b"]
    assert sum(g["total_cost"] for g in got) == 6.8835


def test_a_durable_copy_is_written_when_a_data_dir_is_given() -> None:
    data = Path(tempfile.mkdtemp())
    _emit(step="8_extract_pass1", counter={"done": 3, "total_cost": 1.5},
          data_dir=data)
    files = list((data / "_runs").glob("8_extract_pass1_*.summary.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["total_cost"] == 1.5


def test_an_unwritable_data_dir_never_fails_a_finished_run() -> None:
    """A summary is diagnostics. Losing it must not fail work already paid for."""
    out = _emit(step="s", counter={"done": 1},
                data_dir=Path("/proc/nonexistent/nope"))
    assert parse_run_summaries(out)[0]["done"] == 1


def test_garbage_lines_are_skipped_not_raised() -> None:
    assert parse_run_summaries("RUN_SUMMARY {not json\nplain line") == []
    assert parse_run_summaries("") == []


def test_every_llm_step_emits_a_summary() -> None:
    """Guards against a script being added or refactored without one — the
    server would silently report $0 for that stage."""
    expected = {
        "classify.py": "emit_run_summary",
        "extract_pass1.py": 'emit_run_summary("8_extract_pass1"',
        "extract_pass2.py": 'emit_run_summary(f"9_extract_pass2_',
    }
    for fname, needle in expected.items():
        text = (REPO_ROOT / "steps" / fname).read_text()
        assert needle in text, f"{fname} no longer emits a run summary"
