"""Non-article DOI filtering (utils.is_fetchable_article).

A search legitimately returns Zenodo/figshare deposits and supplementary
components -- they really do mention the topic -- but none has an article to
fetch, screen or extract. Each one burns a download slot and lands in the
evidence table with a blank title. One long COVID corpus carried 1,819 of them,
392 marked relevant.

The Dove Press cases are the point of this file. `10.2147/amep.s342386` is a
REAL PAPER (Crossref type "journal-article") that matches a naive
"dot-s-digits" supplement pattern; 247 of that corpus's 409 pattern hits were
Dove Press articles. A filter without the parent-has-digits check silently
deletes them.
"""
import pytest

from ber_core.utils import is_fetchable_article, is_supplement_doi


@pytest.mark.parametrize("doi", [
    "10.2147/amep.s342386",      # Dove Press: .sNNNNN IS the article DOI
    "10.2147/ccide.s353514",
    "10.2147/cia.s393732",
])
def test_dove_press_articles_are_not_supplements(doi):
    """Regression: the naive pattern would discard 247 real papers."""
    assert is_supplement_doi(doi) is False
    assert is_fetchable_article(doi) is True


@pytest.mark.parametrize("doi", [
    "10.5546/aap.2021.s198",              # Arch Argent Pediatr
    "10.5747/cs.2020.v4.n4.s116",         # Colloquium Series
    "10.48087/bjmsoa.2022.s914",
    "10.1590/s1677-5538.ibju.2020.s124",  # SciELO / Int Braz J Urol
    "10.29271/jcpsp.2020.supp2.s171",
    "10.6938/tcmiid2024.s008",
    "10.18006/2020.8(spl-1-sars-cov-2).s158.s175",
])
def test_supplement_issue_page_numbers_are_not_components(doi):
    """Smaller journals use .sNNN as a supplement-ISSUE page number.

    Crossref types these "journal-article". A parent-has-digits rule with no
    publisher allow-list mislabelled 4 of a 40-DOI sample as supplements --
    every one a real paper that would have been silently dropped.
    """
    assert is_supplement_doi(doi) is False
    assert is_fetchable_article(doi) is True


def test_unknown_publishers_default_to_article():
    """The safe direction: a wasted fetch beats a dropped paper."""
    assert is_fetchable_article("10.99999/newjournal.2026.001.s001") is True


@pytest.mark.parametrize("doi", [
    "10.1371/journal.pone.0330793.s008",   # PLOS supporting information
    "10.1371/journal.pone.0330793.s013",
    "10.1021/acsomega.5c04858.s003",       # ACS supporting information
    "10.1021/acs.biochem.5c00550.s001",
])
def test_supplementary_components_are_filtered(doi):
    assert is_supplement_doi(doi) is True
    assert is_fetchable_article(doi) is False


@pytest.mark.parametrize("doi", [
    "10.5281/zenodo.18799149",
    "10.6084/m9.figshare.12345678",
    "10.5283/some-repository-record",
    "10.22032/dbt.12345",
])
def test_data_repository_deposits_are_filtered(doi):
    assert is_fetchable_article(doi) is False


@pytest.mark.parametrize("doi", [
    "10.1186/s12982-026-01548-3",   # BMC - note the leading s in the journal id
    "10.1093/ofid/ofae123",
    "10.1016/j.jacc.2026.03.177",
    "10.3390/jcm15135202",
    "10.1002/jmv.70970",
])
def test_ordinary_articles_pass(doi):
    assert is_fetchable_article(doi) is True


def test_case_insensitive_and_blank():
    assert is_fetchable_article("10.5281/ZENODO.1") is False
    assert is_fetchable_article("10.1371/JOURNAL.PONE.0330793.S008") is False
    assert is_fetchable_article("") is False
    assert is_fetchable_article(None) is False


def test_bare_journal_prefix_is_not_treated_as_a_parent():
    """`10.2147/amep` has no article number, so `.sNNN` off it is the article."""
    assert is_supplement_doi("10.2147/amep.s1") is False
    # ...whereas a numbered parent means the suffix really is a component.
    assert is_supplement_doi("10.1371/journal.abc.2024.001.s001") is True
