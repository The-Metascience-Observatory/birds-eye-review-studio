"""Size the pipeline's fan-out to the machine it is actually running on.

Every worker count in this stack used to be a hardcoded constant chosen for a
workstation: ``runner.py`` asked ``convert_pdfs.py`` for **4** parallel papers
against a script whose own ``--help`` says *"each uses ~8 GB RAM"* — ~32 GB of
demand. On a 15 W laptop under WSL2 (whose VM defaults to ~50% of host RAM) that
does not swap gracefully; it takes the machine down.

Two properties drive the design:

* **Measure AVAILABLE memory, not total.** ``MemAvailable`` is the kernel's own
  estimate of what a new workload can claim without swapping — it already nets
  out other tenants' usage. On a shared production box (other services, variable
  load) ``MemTotal`` would happily promise memory that is not there.
* **Measure at call time, not import time.** Nothing here is cached, so a job
  starting when the box is busy gets a smaller fan-out than one starting when it
  is idle. This is why the helpers are functions rather than module constants.

Deliberately dependency-free: the lean server venv has no ``psutil``, and this is
imported on the request path. Every probe degrades to a conservative default
rather than raising — a machine we cannot measure gets the *small* configuration,
never an unbounded one.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Documented cost of one ``convert_pdfs.py`` worker (its own --help says
#: "each uses ~8 GB RAM"). The divisor for convert fan-out.
CONVERT_GB_PER_WORKER = 8.0

#: Fallbacks used when a probe fails. These are the SMALL configuration on
#: purpose — an unmeasurable machine is treated as constrained.
_FALLBACK_CONVERT_WORKERS = 1
_FALLBACK_EXTRACTION_WORKERS = 3

#: Ceilings preserving today's behaviour on a large host, so this change is a
#: no-op on the machines that were already fine.
MAX_CONVERT_WORKERS = 4
MAX_EXTRACTION_WORKERS = 10


def _env_int(name: str) -> int | None:
    """A positive int from the environment, or None. Never raises."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def available_ram_gb() -> float | None:
    """Memory a new workload can claim, in GB, or None if unmeasurable.

    Reads ``MemAvailable`` — not ``MemTotal`` — so memory already spoken for by
    other tenants is excluded. Inside WSL2 this is the VM's figure, which is the
    real ceiling (the VM caps at ~50% of host RAM by default), not the host's.
    """
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024.0 * 1024.0)  # kB -> GB
    except (OSError, ValueError, IndexError):
        return None
    return None


def total_ram_gb() -> float | None:
    """Total RAM in GB, or None. For reporting only — sizing uses available."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024.0 * 1024.0)
    except (OSError, ValueError, IndexError):
        return None
    return None


def usable_cores() -> int:
    """Cores available to this process, floored at 1.

    Prefers the scheduler affinity mask over ``os.cpu_count()`` so a container
    pinned to a subset of CPUs is measured correctly.

    NOTE: this counts efficiency cores as equals — a 2P+8E laptop reports 10.
    Callers sizing CPU-bound work should halve it (see ``extraction_workers``).
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def convert_workers() -> int:
    """Parallel papers for ``convert_pdfs.py`` (``--workers``).

    Derived from that script's own documented ~8 GB per worker against currently
    AVAILABLE memory. Override with ``BER_CONVERT_WORKERS``.
    """
    override = _env_int("BER_CONVERT_WORKERS")
    if override is not None:
        return min(override, MAX_CONVERT_WORKERS)
    ram = available_ram_gb()
    if ram is None:
        return _FALLBACK_CONVERT_WORKERS
    return _clamp(int(ram // CONVERT_GB_PER_WORKER), 1, MAX_CONVERT_WORKERS)


def extraction_workers() -> int:
    """Parallel LLM extraction threads (``extract_pass2.py --workers``).

    Each thread spawns its own ``claude`` CLI subprocess, so this is bounded by
    BOTH cores and memory: cores are halved (E-cores are not full cores, and the
    box has other work), and we allow roughly one thread per 1.5 GB available.
    Override with ``BER_EXTRACTION_WORKERS``.
    """
    override = _env_int("BER_EXTRACTION_WORKERS")
    if override is not None:
        return min(override, MAX_EXTRACTION_WORKERS)
    by_cpu = usable_cores() // 2
    ram = available_ram_gb()
    by_ram = MAX_EXTRACTION_WORKERS if ram is None else int(ram // 1.5)
    if ram is None and usable_cores() <= 2:
        return _FALLBACK_EXTRACTION_WORKERS
    return _clamp(min(by_cpu, by_ram), 2, MAX_EXTRACTION_WORKERS)


#: Below this, even ONE convert worker (~8 GB documented) does not fit and the
#: heavyweight ML tiers have to go.
CRITICAL_MEMORY_GB = 6.0


def critical_memory() -> bool:
    """Below one convert worker's documented footprint.

    The worker floor is 1 and one worker is budgeted at ~8 GB, so under this
    threshold the fan-out knob has nothing left to give — the only remaining
    lever is dropping the heavyweight extraction tiers (marker-pdf is an entire
    second ML stack loaded purely as a fallback). Unmeasurable counts as yes.
    """
    ram = available_ram_gb()
    return True if ram is None else ram < CRITICAL_MEMORY_GB


def node_heap_mb() -> int:
    """``--max-old-space-size`` for ``next dev``.

    Node otherwise targets a fraction of visible RAM with no cgroup limit, which
    on a constrained box competes with the pipeline for the same memory.
    """
    ram = available_ram_gb()
    if ram is None:
        return 1536
    return _clamp(int(ram * 1024 * 0.25), 1024, 4096)


def summary() -> str:
    """One human-readable line for startup output and logs."""
    total, avail = total_ram_gb(), available_ram_gb()
    ram_txt = (
        f"{avail:.1f}GB free of {total:.1f}GB"
        if total is not None and avail is not None
        else "RAM unknown"
    )
    return (
        f"{usable_cores()} cores, {ram_txt} -> "
        f"convert={convert_workers()} extraction={extraction_workers()}"
        f"{' (low-memory: fast conversion, no marker fallback)' if critical_memory() else ''}"
    )
