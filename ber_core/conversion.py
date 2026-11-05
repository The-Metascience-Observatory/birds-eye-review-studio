"""Shared facts about PDF/HTML -> body.md conversions.

ONE definition of which pdf4llm ``extraction_mode`` values put TABLES into
``body.md``. It used to be copied into ``convert_pdfs.py`` (the run summary),
``redo_degraded_conversions.py`` (the repair tool) and the server's evidence
row, and the copies drifted: the repair tool lacked ``docling-html`` and
``marker-fallback`` and so treated good conversions as degraded. "Degraded" is
the signal this codebase uses to decide a corpus needs re-converting, so a
disagreement here means paying to redo work that was fine — or trusting work
that was not.
"""
from __future__ import annotations

import json
from pathlib import Path

#: Conversion modes that capture table structure. Anything else (notably
#: ``grobid-only``) produces text whose tables are unreachable downstream.
TABLE_BEARING_MODES: frozenset[str] = frozenset({
    # docling's sections/tables win the GROBID+docling merge outright.
    "merged-grobid-docling",
    "docling-only",
    "docling-only-fallback",
    "pymupdf4llm-fallback",
    # Publisher HTML converted by html_convert.py: tables are embedded as
    # literal <table> markup, which keeps row/col spans pipe tables cannot.
    "docling-html",
    # marker embeds its tables in the body markdown.
    "marker-fallback",
})


def is_table_bearing(mode: str | None) -> bool:
    """True when a conversion in *mode* can have captured tables."""
    return isinstance(mode, str) and mode in TABLE_BEARING_MODES


def conversion_quality(provenance: dict | None) -> dict:
    """Interpret explicit conversion failures without certifying old outputs.

    A table-capable engine is not evidence of complete conversion. Legacy
    provenance remains ``unknown`` until reconverted; a table count of zero
    alone is not an error because many valid papers contain no tables.
    """
    data = provenance if isinstance(provenance, dict) else {}
    mode = data.get("extraction_mode")
    mode = mode if isinstance(mode, str) else None
    status = data.get("conversion_status", "unknown")
    status = status if isinstance(status, str) else "unknown"
    warnings = data.get("warnings") or []
    warnings = list(warnings) if isinstance(warnings, (list, tuple)) else [warnings]
    warnings = [str(w) for w in warnings if w]
    if data.get("extraction_mode") is not None and mode is None:
        warnings.append("Invalid conversion mode metadata")
    if data.get("conversion_status") is not None and not isinstance(data["conversion_status"], str):
        warnings.append("Invalid conversion status metadata")
    bad_status = status in {"partial", "partial_success", "degraded", "failed", "failure"}
    degraded = bool((mode is not None and not is_table_bearing(mode)) or bad_status)
    if bad_status:
        warnings.append(f"Conversion status: {status}")
    if mode is not None and not is_table_bearing(mode):
        warnings.append(f"Conversion mode {mode} does not preserve table structure")
    return {
        "mode": mode,
        "status": status,
        "degraded": degraded,
        "requires_review": bool(degraded or warnings or data.get("requires_review")),
        "warnings": list(dict.fromkeys(warnings)),
        "tables_captured": data.get("tables_captured"),
    }


def read_conversion_quality(paper_dir: Path) -> dict:
    """Read additive quality metadata; malformed provenance needs attention."""
    path = paper_dir / "provenance.json"
    if not path.exists():
        return conversion_quality(None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("provenance must be an object")
    except (OSError, ValueError):
        data = {"conversion_status": "degraded", "warnings": ["Unreadable conversion provenance"]}
    return conversion_quality(data)
