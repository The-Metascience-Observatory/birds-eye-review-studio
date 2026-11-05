"""Read a review's synthesized narrative document (review_document.md).

Modeled on ``bundle.py``: read-only, locates the artifact by globbing the
workspace's single project dir, and computes staleness so the UI can offer a
regenerate instead of quietly showing prose about superseded data.

The document's inputs are recorded by ``synthesize_review.py`` in its sidecar's
``source_fingerprint`` ({path: mtime_ns} at generation time) — staleness is
"any recorded input file has a newer mtime now", which catches both a rebuilt
dashboard bundle and re-run extraction without hardcoding the input list twice.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()
from ber_core.synthesize_review import (  # noqa: E402 — the script owns these literals
    AUDIENCES,
    DEPTHS,
    MODELS,
    SECTION_IDS,
)

# Re-exported: the routers validate synthesis options against these.
__all__ = ["AUDIENCES", "DEPTHS", "MODELS", "SECTION_IDS", "DOC"]

DOC = "review_document.md"
SIDECAR = "review_document.json"


def _data_dir(workspace: Path) -> Path | None:
    """The project data dir holding the document, or None if never generated.

    Glob like ``bundle.bundle_dir``: the project slug is derived from the topic
    name, so recomputing it here could point at a renamed-away directory.
    """
    projects = Path(workspace) / "projects"
    if not projects.is_dir():
        return None
    for child in sorted(projects.iterdir()):
        d = child / "data"
        if (d / DOC).exists():
            return d
    return None


def _read_sidecar(data_dir: Path) -> dict:
    p = data_dir / SIDECAR
    if not p.exists():
        return {}
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _staleness(meta: dict) -> tuple[bool, str | None]:
    fingerprint = meta.get("source_fingerprint")
    if not isinstance(fingerprint, dict):
        return False, None
    for path_str, mtime_ns in fingerprint.items():
        p = Path(path_str)
        try:
            if p.exists() and p.stat().st_mtime_ns > int(mtime_ns):
                return True, (f"{p.name} changed after this document was "
                              "generated")
        except (OSError, TypeError, ValueError):
            continue
    return False, None


def document_state(workspace: Path) -> dict:
    """Everything GET /reviews/{id}/review-document returns (ReviewDocumentOut)."""
    d = _data_dir(workspace)
    if d is None:
        return {"exists": False}

    try:
        markdown = (d / DOC).read_text(encoding="utf-8")
    except OSError:
        return {"exists": False}

    meta = _read_sidecar(d)
    stale, stale_reason = _staleness(meta)
    return {
        "exists": True,
        "stale": stale,
        "stale_reason": stale_reason,
        "generated_at": meta.get("generated_at"),
        "depth": meta.get("depth"),
        "audience": meta.get("audience"),
        "model": meta.get("model"),
        "provider": meta.get("provider"),
        "word_count": meta.get("word_count"),
        "sections": meta.get("sections"),
        "markdown": markdown,
    }
