"""Cross-file DOI joins must be case-insensitive.

`utils.norm_paper_id` is the pipeline's canonical join key, and `utils.py`'s own
contract note warns that comparing a RAW paper_id against a normalized map
silently never matches. Several joins reached past it anyway.

The worst was the dashboard metadata lookup: an uppercase `paper_id` missed the
lowercase-keyed metadata map, so the row lost its journal and authors AND
`_year_of` fell through to the DOI-string regex — reporting a **wrong** year
rather than a blank one. Nothing logged it.

Measured across every corpus on disk at the time of the fix, 0 of 2,243 pass-2
records and 0 of 10,122 converted dirs actually carried uppercase, so this was a
landmine rather than an active bug. These tests keep it defused.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core import dashboard_summary as ds  # noqa: E402
from ber_core.utils import norm_paper_id  # noqa: E402


def _record(paper_id: str) -> dict:
    return {
        "paper_id": paper_id,
        "study_design": {"design_type": "rct", "arms": []},
        "outcomes": [],
        "risk_of_bias": {"overall_judgment": "low"},
    }


def test_metadata_joins_regardless_of_doi_case() -> None:
    """An uppercase paper_id must still find its lowercase-keyed metadata.

    Pins the exact failure: before the fix this returned year=2020 (scraped from
    the DOI string by the fallback regex) with empty journal/authors — silently
    WRONG, which is worse than silently missing.
    """
    meta = {"10.1001/jama.2020.1234": {"year": 2019, "journal": "JAMA",
                                       "authors": "Smith"}}
    rows, _ = ds.build_rows([_record("10.1001/JAMA.2020.1234")], metadata=meta)
    row = rows[0]

    assert row["year"] == 2019, "must use the metadata year, not the DOI-string year"
    assert row["journal"] == "JAMA"
    assert row["authors"] == "Smith"


def test_lowercase_doi_is_unaffected() -> None:
    """The common path must behave exactly as before."""
    meta = {"10.1001/jama.2020.1234": {"year": 2019, "journal": "JAMA",
                                       "authors": "Smith"}}
    rows, _ = ds.build_rows([_record("10.1001/jama.2020.1234")], metadata=meta)
    assert rows[0]["year"] == 2019 and rows[0]["journal"] == "JAMA"


def test_arm_suffix_is_stripped_before_the_metadata_join() -> None:
    """Export ids can carry an `#arm` suffix; the metadata map never does."""
    meta = {"10.1001/jama.2020.1234": {"year": 2019, "journal": "JAMA"}}
    rows, _ = ds.build_rows([_record("10.1001/jama.2020.1234#arm1")], metadata=meta)
    assert rows[0]["year"] == 2019


def test_screened_out_membership_is_normalized() -> None:
    """`exclusion_reason` tests membership in a norm_paper_id-keyed set."""
    screened = {norm_paper_id("10.1001/jama.2020.1234")}
    assert ds.exclusion_reason(_record("10.1001/JAMA.2020.1234"),
                               screened) == "screened_out"
    assert ds.exclusion_reason(_record("10.9999/other"), screened) != "screened_out"
