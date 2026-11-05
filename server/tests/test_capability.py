"""Machine-aware fan-out sizing (services/capability.py + runner arg capping).

Context: the studio hardcoded ``--workers 4`` for PDF conversion against a script
documenting ~8 GB per worker (~32 GB), which crashed a 16 GB laptop whose WSL2 VM
was capped near 8 GB. These tests pin the two properties that matter:

* a big host keeps TODAY's numbers (this change must be a no-op there), and
* a small or busy host is scaled down rather than left unbounded.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import capability, runner  # noqa: E402


@pytest.fixture
def machine(monkeypatch):
    """Pretend to be a machine of a given size."""

    def _set(available_gb: float | None, cores: int, total_gb: float | None = None):
        monkeypatch.setattr(capability, "available_ram_gb", lambda: available_gb)
        monkeypatch.setattr(
            capability, "total_ram_gb", lambda: total_gb if total_gb else available_gb
        )
        monkeypatch.setattr(capability, "usable_cores", lambda: cores)

    return _set


# --- the no-regression case -------------------------------------------------

def test_a_large_host_keeps_the_previous_hardcoded_values(machine):
    """A workstation must derive exactly what was hardcoded before (4 and 10),
    so this change cannot slow down the machines that were already fine."""
    machine(available_gb=32.0, cores=20, total_gb=64.0)
    assert capability.convert_workers() == 4
    assert capability.extraction_workers() == 10
    assert capability.critical_memory() is False


# --- the crash case ---------------------------------------------------------

def test_a_small_wsl_laptop_is_scaled_all_the_way_down(machine):
    """A tester's box: 16 GB host, WSL2 capped ~8 GB, loaded. The old value of 4
    convert workers meant ~32 GB of demand here."""
    machine(available_gb=5.0, cores=10, total_gb=8.0)
    assert capability.convert_workers() == 1
    assert capability.extraction_workers() <= 3
    assert capability.critical_memory() is True  # drop the fallback ML tiers


def test_memory_tiers_escalate_in_the_right_order(machine):
    """Quality is only sacrificed when the fan-out knob has nothing left to give."""
    machine(available_gb=32.0, cores=20)
    assert not capability.critical_memory()

    machine(available_gb=9.0, cores=10)  # tight: fewer workers, same quality
    assert capability.convert_workers() == 1 and not capability.critical_memory()

    machine(available_gb=4.0, cores=10)  # below one worker's ~8 GB budget
    assert capability.critical_memory()


def test_extraction_is_bounded_by_cores_not_just_memory(machine):
    """A 2P+8E laptop reports 10 logical cores but has 2 real ones. Plenty of
    RAM must not license 10 concurrent `claude` processes on it."""
    machine(available_gb=64.0, cores=4)
    assert capability.extraction_workers() == 2


# --- degraded / hostile inputs ---------------------------------------------

def test_an_unmeasurable_machine_gets_the_small_configuration(machine):
    """If /proc/meminfo can't be read we must assume constrained, never
    unbounded — the failure mode of guessing high is a dead machine."""
    machine(available_gb=None, cores=2)
    assert capability.convert_workers() == 1
    assert capability.extraction_workers() == 3
    assert capability.critical_memory() is True


def test_env_override_wins_but_still_respects_the_ceiling(machine, monkeypatch):
    machine(available_gb=5.0, cores=10)
    monkeypatch.setenv("BER_CONVERT_WORKERS", "3")
    assert capability.convert_workers() == 3
    monkeypatch.setenv("BER_CONVERT_WORKERS", "999")
    assert capability.convert_workers() == capability.MAX_CONVERT_WORKERS
    monkeypatch.setenv("BER_CONVERT_WORKERS", "garbage")
    assert capability.convert_workers() == 1  # falls back to the measurement


def test_worker_counts_are_never_zero_or_negative(machine):
    """A zero would mean 'no parallelism' to some pools and 'unbounded' to
    others — always at least 1."""
    for avail, cores in [(0.1, 1), (0.0, 1), (1.0, 2)]:
        machine(available_gb=avail, cores=cores)
        assert capability.convert_workers() >= 1
        assert capability.extraction_workers() >= 2


# --- the dispatch-time cap --------------------------------------------------

class _Step:
    def __init__(self, script, args):
        self.script = script
        self.args = args


def test_an_excessive_worker_request_is_lowered(machine):
    machine(available_gb=5.0, cores=10)
    step = _Step("extract_pass2.py", ["--config", "c.yaml", "--workers", "10"])
    args = runner.cap_step_args(step)
    assert args[args.index("--workers") + 1] == str(capability.extraction_workers())


def test_a_modest_worker_request_is_left_alone(machine):
    """Never RAISE a deliberate choice — only lower an unsafe one."""
    machine(available_gb=64.0, cores=20)
    step = _Step("extract_pass2.py", ["--workers", "2"])
    assert runner.cap_step_args(step) == ["--workers", "2"]


def test_a_missing_workers_flag_is_added(machine):
    """Scripts fall back to the config's `extraction_workers` when no flag is
    passed, so the cap must be made explicit to take effect."""
    machine(available_gb=5.0, cores=10)
    step = _Step("extract_pass2.py", ["--config", "c.yaml"])
    args = runner.cap_step_args(step)
    assert "--workers" in args
    assert args[args.index("--workers") + 1] == str(capability.extraction_workers())


def test_unrelated_steps_are_untouched(machine):
    machine(available_gb=5.0, cores=10)
    step = _Step("search_papers.py", ["--config", "c.yaml"])
    assert runner.cap_step_args(step) == ["--config", "c.yaml"]


def test_a_malformed_worker_value_is_not_mangled(machine):
    machine(available_gb=5.0, cores=10)
    step = _Step("extract_pass2.py", ["--workers", "auto"])
    assert runner.cap_step_args(step) == ["--workers", "auto"]
