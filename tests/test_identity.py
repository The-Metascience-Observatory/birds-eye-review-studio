"""ber_core.identity: one DOI normalizer for every shape a DOI arrives in."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ber_core.identity import canonical_doi, decode_stem_escapes, join_key, stem_to_doi
from ber_core.utils import decode_artifact_stem, discover_papers, filename_to_doi

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("raw,expected", [
    ("10.1000/ABC.123", "10.1000/abc.123"),
    ("https://doi.org/10.1000/abc", "10.1000/abc"),
    ("http://dx.doi.org/10.1000/abc", "10.1000/abc"),
    ("doi.org/10.1000/abc", "10.1000/abc"),
    ("doi:10.1000/abc", "10.1000/abc"),
    ("DOI: 10.1000/abc", "10.1000/abc"),
    ("10.1000%2Fabc", "10.1000/abc"),
    ("  10.1000/abc.  ", "10.1000/abc"),
    ("10.1000/abc;", "10.1000/abc"),
    ("10.1000/abc#arm2", "10.1000/abc"),
    ("10.1000--abc", "10.1000/abc"),                     # this repo's stem
    ("10.1023--A~1022", "10.1023/a:1022"),               # fetchpdf stem
    ("10.1000--x~2d~y", "10.1000/x-y"),                  # fetchpdf hex escape
    ("10.1016/S0140-6736(20)30183-5", "10.1016/s0140-6736(20)30183-5"),
    ("(10.1000/abc)", None),                             # leading junk: not a DOI
    ("10.1000/abc)", "10.1000/abc"),                     # unbalanced close
    ("10.1002/(sici)1097(199709)", "10.1002/(sici)1097(199709)"),
    ("10.1234", None),
    ("not a doi", None),
    ("", None),
    (None, None),
    ("10.12/abc", None),                                 # registrant too short
    ("10.1000/has space", None),
])
def test_canonical_doi(raw, expected):
    assert canonical_doi(raw) == expected


def test_canonical_doi_keeps_a_real_double_dash():
    assert canonical_doi("10.1000/a--b") == "10.1000/a--b"


@pytest.mark.parametrize("raw,expected", [
    ("pmid_123", "pmid_123"),
    ("PMID:00123", "pmid_123"),
    ("pmid 42", "pmid_42"),
    ("10.1000/X#arm1", "10.1000/x"),
    ("https://doi.org/10.1000/x", "10.1000/x"),
    ("12345", None),
    ("cyberleninka:abc", None),
])
def test_join_key(raw, expected):
    assert join_key(raw) == expected


def test_join_key_bare_digits_opt_in():
    assert join_key("12345", bare_digits_are_pmids=True) == "pmid_12345"


def test_stem_helpers_preserve_case():
    assert decode_stem_escapes("10.1023--A~1022") == "10.1023--A:1022"
    assert stem_to_doi("10.1023--A~1022") == "10.1023/A:1022"
    assert stem_to_doi("pmid_123") == "pmid_123"


def test_utils_decoders_agree():
    assert filename_to_doi("10.1023--A~1022.pdf") == "10.1023/A:1022"
    assert filename_to_doi("10.1000--x_abstract.md") == "10.1000/x"
    assert decode_artifact_stem("10.1023--A~1022") == canonical_doi("10.1023--A~1022")


def test_paper_ref_decodes_fetchpdf_escapes(tmp_path):
    folder = tmp_path / "Review" / "10.1023--a~1022"
    folder.mkdir(parents=True)
    (folder / "body.md").write_text("x")
    (ref,) = discover_papers(tmp_path)
    assert ref.paper_id == "10.1023/a:1022"
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "10.1000--ABC.pdf").write_bytes(b"%PDF")
    (ref,) = discover_papers(flat)
    assert ref.paper_id == "10.1000/ABC"   # case preserved, as before


def test_search_normalize_doi_validates_but_keeps_stored_format():
    spec = importlib.util.spec_from_file_location("_s1_ident", ROOT / "steps/search_papers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.normalize_doi("https://doi.org/10.1000/ABC.") == "10.1000--abc"
    assert mod.normalize_doi("10.1234") == ""
    assert mod.normalize_doi("") == ""


def test_local_sciscinet_key():
    from ber_core import local_sciscinet
    assert local_sciscinet._doi_key("doi:10.1000/X") == "https://doi.org/10.1000/x"
    assert local_sciscinet._doi_key("") == ""
