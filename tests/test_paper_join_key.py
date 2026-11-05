"""`ber_core.identity.paper_join_key` replaced three private DOI normalizers.

export_dashboard's feed-merge dedupe key, ber_core.inspect_sources.paper_key
and the server's metadata fetch argument each normalized DOIs their own way.
They now share one key. The reference implementations below are the removed
private ones, kept verbatim so this file pins that every id shape the corpora
actually contain still produces the SAME key (checked against all 6,747
paper ids on disk at the time of the switch: zero differences).
"""
import importlib.util
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from ber_core.identity import paper_join_key
from ber_core.utils import filename_to_doi

ROOT = Path(__file__).resolve().parents[1]


def _old_export_norm(pid):
    doi = filename_to_doi(pid) if pid and "--" in pid else (pid or "")
    return doi.strip().lower()


def _old_inspect_paper_key(value):
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value or "").strip(), flags=re.I)
    return unquote(value).split("#")[0].replace("--", "/").lower().strip()


EXISTING_IDS = [
    "10.1001/jama.2021.1234",
    "10.1001/JAMA.2021.1234",
    "10.1001--jama.2021.1234",
    "10.1016/S0140-6736(20)30183-5",
    "10.1016--s0140-6736(20)30183-5",
    "10.1023--a~3a~1022",          # fetchpdf escape inside a stem
    "10.3346--jkms.2026.41.e279",
    "10.1101/2021.06.01.21258176",
    "pmid_12345678",
    " 10.1186/s13063-020-04800-x ",
    "NCT04280705",                  # not a DOI or PMID: falls back
    "",
]


@pytest.mark.parametrize("pid", [i for i in EXISTING_IDS if i])
def test_export_dedupe_unchanged_for_existing_ids(export_mod, pid):
    """`_union_records` drops an observational record whose key the pass-2 feed
    already holds. Each id spelled the two ways the old key equated (stem vs.
    DOI, upper vs. lower) must still dedupe."""
    other = _old_export_norm(pid)
    pass2 = [_rec(pid)]
    obs = [_rec(other.upper() if other != pid else pid.lower())]
    _, stats = export_mod._union_records(pass2, obs)
    old_same = _old_export_norm(obs[0]["paper_id"]) == _old_export_norm(pid)
    assert (stats["added_observational"] == 0) == old_same


@pytest.mark.parametrize("pid", [i for i in EXISTING_IDS if "~" not in i] + [
    "https://doi.org/10.1001/jama.2021.1234", "10.1001/jama.2021.1234#arm_b",
    "https://doi.org/10.TEST/A#arm"])
def test_inspect_paper_key_unchanged_for_existing_ids(pid):
    from ber_core.inspect_sources import paper_key
    assert paper_key(pid) == _old_inspect_paper_key(pid)


def test_inspect_paper_key_now_decodes_fetchpdf_escapes():
    """The one deliberate change: the old inspect key left fetchpdf's ``~3a~``
    escape in place, so it disagreed with the export (which decoded it)."""
    from ber_core.inspect_sources import paper_key
    assert paper_key("10.1023--a~3a~1022") == "10.1023/a:1022" == _old_export_norm("10.1023--a~3a~1022")


def test_arms_of_one_paper_stay_distinct_records(export_mod):
    """The export dedupes on this key; two arms are two records."""
    _, stats = export_mod._union_records([_rec("10.1/x#a")],
                                         [_rec("10.1/x#b"), _rec("10.1--X#A")])
    assert stats["added_observational"] == 1


def test_shapes_that_now_join():
    """What the shared key adds: URL/doi:-prefixed and punctuated forms join."""
    want = "10.1001/jama.2021.1234"
    for v in ("https://doi.org/10.1001/jama.2021.1234", "doi:10.1001/JAMA.2021.1234",
              "10.1001/jama.2021.1234.", "10.1001%2Fjama.2021.1234"):
        assert paper_join_key(v) == want
    assert paper_join_key("PMID:42") == "pmid_42"
    assert paper_join_key(None) == ""


# --- helpers ------------------------------------------------------------------

def _rec(pid):
    # `study_design` makes the record publishable (see is_unpublishable_record).
    return {"paper_id": pid, "study_design": {"design_type": "rct"}}


@pytest.fixture(scope="module")
def export_mod():
    spec = importlib.util.spec_from_file_location(
        "_export_for_keys", ROOT / "steps/export_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
