"""Regressions for two silent data-corruption bugs in the CLI pipeline layer.

Both were found by auditing live job logs, and both are the same shape: a guard
that looks like it handles missing data but doesn't handle the data that
actually arrives.

1. `extract_pass2._postprocess` crashed with "'NoneType' object has no
   attribute 'get'" whenever a list contained a null ELEMENT. The `or []`
   guards only cover a null LIST. A model declining an ineligible paper emits
   schema-shaped output like `"outcomes": [null]`, which is valid JSON and
   truthy — so it slipped past both `or []` and `_is_null_extraction`, then
   crashed AFTER the paid Sonnet call. Because `load_done_ids_jsonl` treats the
   resulting `_status: "error"` as NOT done, the paper was re-extracted and
   re-billed on every later run: a permanent, self-repeating charge.

2. `utils.dedup_csv(preserve_extra_columns=True)` was a no-op — the branch body
   was literally `pass`. Combined with keep-the-last-row, a re-screen's
   blank-enrichment row silently erased an enriched title/year. `pipeline.py`
   runs the screen before enrichment on the same file, so this fired routinely.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_pass2():
    """Import the numbered pass-2 script (not importable by name)."""
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "_pass2_under_test", REPO_ROOT / "steps/extract_pass2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- 1. null list ELEMENTS must not crash postprocessing -------------------

@pytest.mark.parametrize("record", [
    pytest.param({"study_design": {"arms": [None]}, "sample_sizes": {},
                  "outcomes": [{"x": 1}]}, id="null-arm"),
    pytest.param({"study_design": {"arms": []}, "sample_sizes": {},
                  "outcomes": [None]}, id="null-outcome"),
    pytest.param({"study_design": {"arms": []},
                  "sample_sizes": {"per_arm": [None]},
                  "outcomes": [{"a": 1}]}, id="null-per-arm"),
    pytest.param({"study_design": {"arms": []}, "sample_sizes": {},
                  "outcomes": [{"arm_results": [None]}]}, id="null-arm-result"),
])
def test_postprocess_survives_null_list_elements(record):
    """The exact crash seen in the job logs, one shape per known call site."""
    pass2 = _load_pass2()
    result = pass2._postprocess(dict(record), "10.1/x", "md", "rct")
    assert isinstance(result, dict)


def test_postprocess_still_reports_real_orphans_around_nulls():
    """Skipping nulls must not skip the real entries beside them."""
    pass2 = _load_pass2()
    result = pass2._postprocess(
        {
            "study_design": {"arms": [None, {"arm_id": "A"}]},
            "sample_sizes": {"per_arm": [None, {"arm_id": "Z", "n_randomized": 5}]},
            "outcomes": [None, {"arm_results": [None, {"arm_id": "Q"}]}],
        },
        "10.1/x", "md", "rct",
    )
    warnings = result.get("_warnings") or []
    # "Z" and "Q" are not declared in study_design.arms, so both are orphans.
    assert any("orphan_arm_id=Z" in w for w in warnings)
    assert any("orphan_arm_id=Q" in w for w in warnings)


def test_all_null_lists_count_as_a_decline_not_an_extraction():
    """`"outcomes": [null]` is the model declining, wearing a truthy shape.

    Treating it as content billed the record as a real extraction and carried
    empty sections into the evidence table.
    """
    pass2 = _load_pass2()
    assert pass2._is_null_extraction(
        {"study_design": None, "sample_sizes": None, "outcomes": [None]})
    # A record with genuine content must still NOT be called a decline.
    assert not pass2._is_null_extraction(
        {"study_design": {"arms": [{"arm_id": "A"}]}, "sample_sizes": None,
         "outcomes": []})


# --- 2. dedup_csv must merge, not clobber ----------------------------------

def test_dedup_preserves_enrichment_against_a_later_blank_row(tmp_path):
    """The reported failure: a re-screen must not erase enrichment."""
    sys.path.insert(0, str(REPO_ROOT))
    from ber_core.utils import dedup_csv

    csv_path = tmp_path / "classifications.csv"
    csv_path.write_text(
        "doi,is_relevant,paper_title,paper_year\n"
        "10.1/a,yes,The Real Title,2021\n"   # enriched earlier
        "10.1/a,yes,,\n"                     # re-screen appends, no enrichment
    )
    dedup_csv(csv_path, id_column="doi", preserve_extra_columns=True)
    body = csv_path.read_text()
    assert "The Real Title" in body
    assert "2021" in body


def test_dedup_still_lets_a_later_real_value_win(tmp_path):
    """Merging must not freeze the first value — only blanks are ignored."""
    sys.path.insert(0, str(REPO_ROOT))
    from ber_core.utils import dedup_csv

    csv_path = tmp_path / "c.csv"
    csv_path.write_text(
        "doi,is_relevant,paper_title\n"
        "10.1/a,no,Old Title\n"
        "10.1/a,yes,New Title\n"
    )
    dedup_csv(csv_path, id_column="doi", preserve_extra_columns=True)
    body = csv_path.read_text()
    assert "New Title" in body and "Old Title" not in body
    assert "yes" in body


def test_dedup_without_the_flag_keeps_last_row_wins(tmp_path):
    """Default callers want a straight dedup; that behaviour is unchanged."""
    sys.path.insert(0, str(REPO_ROOT))
    from ber_core.utils import dedup_csv

    csv_path = tmp_path / "c.csv"
    csv_path.write_text(
        "doi,is_relevant,paper_title\n"
        "10.1/a,yes,Title\n"
        "10.1/a,yes,\n"
    )
    n = dedup_csv(csv_path, id_column="doi")
    assert n == 1
    assert "Title" not in csv_path.read_text()


# --- 3. resume must retry undecided rows -----------------------------------

def test_resume_retries_unknown_rows_when_the_status_column_is_named(tmp_path):
    """`unknown` rows were counted as done and never re-screened.

    The helper's legacy fallback guessed at `is_long_covid` / `article_type`,
    neither of which a current classifications.csv has, so the "unknown"
    skip-value could never match.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from ber_core.utils import load_done_dois_csv

    csv_path = tmp_path / "classifications.csv"
    csv_path.write_text(
        "doi,is_relevant,relevance_confidence,article_type\n"
        "10.1/yes,yes,high,rct\n"
        "10.1/unknown,unknown,,\n"
        "10.1/blank,,,\n"
    )
    done = load_done_dois_csv(
        csv_path, id_column="doi",
        skip_values={"parse_error", "unknown", ""},
        status_column="is_relevant",
    )
    assert "10.1/yes" in done            # decided: stays skipped
    assert "10.1/unknown" not in done    # undecided: retried
    assert "10.1/blank" not in done      # undecided: retried
