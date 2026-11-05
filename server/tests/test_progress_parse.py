"""parse_progress: every line shape the pipeline scripts emit.

The ETA contract matters to the UI: `eta_s` is present ONLY when the line
carried a real ETA — "ETA: ?" (no rate yet) and a missing ETA omit the key, so
the client can tell "unknown" from an actual zero. Legacy XhYYm-only lines (old
logs, unupgraded scripts) must keep parsing.
"""
from app.services.runner import parse_progress


def test_tqdm_line():
    p = parse_progress(" 45%|████▌     | 225/500 [00:30<00:37,  7.42it/s]")
    assert p == {
        "done": 225, "total": 500, "elapsed_s": 30, "eta_s": 37, "rate": 7.42,
    }


def test_legacy_hm_eta():
    p = parse_progress("  ── Progress: 123/500 | cost: $0.42 | ETA: 0h02m ──")
    assert p["done"] == 123 and p["total"] == 500
    assert p["eta_s"] == 120


def test_seconds_eta():
    p = parse_progress("  ── Progress: 4/6 | cost: $0.944 | ETA: 45s ──")
    assert p["eta_s"] == 45


def test_minutes_seconds_eta():
    p = parse_progress("  ── Progress: 1/6 | ETA: 12m30s ──")
    assert p["eta_s"] == 750


def test_hours_minutes_eta():
    p = parse_progress("  ── Progress: 10/900 | ETA: 1h02m ──")
    assert p["eta_s"] == 3720


def test_unknown_eta_omits_key():
    p = parse_progress("  ── Progress: 0/6 | ETA: ? ──")
    assert p["done"] == 0 and p["total"] == 6
    assert "eta_s" not in p


def test_missing_eta_omits_key():
    p = parse_progress("Progress: 3/6 | outcomes: 15")
    assert p["done"] == 3 and p["total"] == 6
    assert "eta_s" not in p


def test_frac_counter():
    assert parse_progress("processing [123/500] something") == {
        "done": 123, "total": 500,
    }


def test_non_progress_line():
    assert parse_progress("Found 14 papers in eligible_rct") is None
