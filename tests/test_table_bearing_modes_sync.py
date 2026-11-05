"""Every consumer must agree on which conversion modes carry tables.

The single definition is ``ber_core.conversion.TABLE_BEARING_MODES``. It used
to be copied into convert_pdfs.py (the convert-run summary),
redo_degraded_conversions.py (the repair tool) and
server/app/services/evidence.py (the per-paper evidence row), and the copies
drifted: `docling-html` had to be added to each when HTML conversion landed,
`marker-fallback` was missing from the convert side for a long time, and the
repair tool lacked both — so it would "repair" good conversions. A mode present
in one place but not another means the same conversion is reported as
table-bearing in one place and degraded in the other, and "degraded" is the
signal this codebase uses to decide a corpus needs re-converting.

The server side is checked by VALUE, so it may either import the shared set or
keep a literal copy of it.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from ber_core.conversion import TABLE_BEARING_MODES

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_sync_{Path(name).stem}", ROOT / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _evidence_side_modes():
    ev = ROOT / "server" / "app" / "services" / "evidence.py"
    if not ev.exists():
        pytest.skip("server package not present")
    sys.path.insert(0, str(ROOT / "server"))
    try:
        from app.services.evidence import _TABLE_BEARING_MODES
    except Exception as e:                     # pragma: no cover
        pytest.skip(f"evidence import failed: {e}")
    return set(_TABLE_BEARING_MODES)


def test_convert_step_uses_the_shared_set():
    assert set(_load("steps/convert_pdfs.py")._TABLE_BEARING_MODES) == set(TABLE_BEARING_MODES)


def test_repair_tool_uses_the_shared_set():
    assert set(_load("scripts/maintenance/redo_degraded_conversions.py").TABLE_BEARING_MODES) == set(TABLE_BEARING_MODES)


def test_server_evidence_matches_the_shared_set():
    evidence = _evidence_side_modes()
    shared = set(TABLE_BEARING_MODES)
    assert evidence == shared, (
        f"only in ber_core.conversion: {sorted(shared - evidence)}; "
        f"only in evidence.py: {sorted(evidence - shared)}")


def test_known_table_bearing_modes_are_registered():
    """Each of these demonstrably puts tables in body.md."""
    for mode in ("merged-grobid-docling",   # docling's tables win the merge
                 "docling-only",            # measured 4/4 table capture
                 "docling-html",            # HTML tables from publisher HTML
                 "marker-fallback"):        # marker embeds tables in markdown
        assert mode in TABLE_BEARING_MODES, f"{mode} missing from TABLE_BEARING_MODES"
    assert "grobid-only" not in TABLE_BEARING_MODES
