"""The PRISMA flow diagram for the wizard — the screening funnel, live.

`ber_core/prisma.py::derive_counts` already computes the full multi-stage funnel
(identified → deduplicated → screened → eligible → included) from the same
pipeline CSVs everything else reads, and `to_visualize_json` shapes it into the
`{main_flow, exclusions, sources}` structure the diagram renders. Both are
tested. This service is the missing wire: resolve the review's workspace to a
`Project` and call them.

Ungated and tolerant of a partial pipeline — `derive_counts` guards every stage
on the CSV existing, so a review that has only searched yields the top of the
funnel and nothing below it, rather than an error. Never raises.
"""
from __future__ import annotations

from pathlib import Path


from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()

from ber_core.prisma import derive_counts, to_visualize_json  # noqa: E402


def _empty() -> dict:
    """A funnel with nothing in it — the same shape, so the client renders an
    empty diagram rather than special-casing a missing payload."""
    return {"main_flow": [], "exclusions": [], "sources": [], "counts": {}}


def build_prisma(workspace: Path) -> dict:
    """The screening funnel for this review, from whatever artifacts exist."""
    from . import pipeline_bridge

    try:
        project = pipeline_bridge.build_project(workspace)
    except Exception:  # noqa: BLE001 — a half-created workspace has no config
        return _empty()

    if not project.data_dir.exists():
        return _empty()

    counts = derive_counts(project)
    diagram = to_visualize_json(counts)
    # Carry the raw counts too: the `included_warning` (non-deduplicated count)
    # and per-reason exclusion breakdown are honesty signals the diagram's
    # summary layer surfaces, and they have no slot in the visualize shape.
    diagram["counts"] = counts
    return diagram
