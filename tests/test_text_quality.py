"""Tests for the converted-text quality guard (utils.looks_like_boilerplate).

The burn case, from a real run (DOI 10.1159/000205051 in the nattokinase
review): the "converted paper" was five repetitions of a library download
stamp. Over 200 chars, not garbled — so it passed every existing check and
cost three full-price Sonnet calls whose only possible answer was "the paper
content is empty".

The guard is deliberately conservative: it must NEVER reject a real paper, so
it requires >=2 boilerplate markers up front AND zero substantive words
(method/results/patients/…) anywhere, or the verified heading and contact
address of a publisher permissions guide.
"""
import pytest
from ber_core.utils import looks_like_boilerplate, read_best_text, PaperRef

DOWNLOAD_STAMP = (
    "Downloaded by: Dahlgree Med. Library, Georgetown Univ.\n"
    "IP address 141.161.0.1. Date of download: 3/4/2009. "
    "For personal use only. All rights reserved. Terms of use apply.\n"
) * 5

PERMISSIONS_GUIDE = (
    "Lippincott Journal Portfolio\nAuthor Permission Guidelines\npermissions@lww.com\n"
    "ARTICLES\nAll requests to reuse your Final Published Article must be submitted "
    "via RightsLink. WHAT IS A FINAL PEER-REVIEWED MANUSCRIPT?\n"
    "Eligible portions include figures, tables, illustrations, a 400 word excerpt or Abstract."
)


@pytest.mark.parametrize('prefix', ['', '# Body\n\n## ', '\n  \n'])
def test_permissions_guide_is_not_research_despite_abstract_keyword(prefix):
    assert looks_like_boilerplate(prefix + PERMISSIONS_GUIDE)


def test_incidental_reference_to_permissions_guide_does_not_reject_paper():
    assert not looks_like_boilerplate(
        'Research article\nMethods: patients were enrolled. Results: improvement.\n'
        + PERMISSIONS_GUIDE)


def test_permissions_guide_is_unavailable_text_not_a_screening_exclusion(tmp_path):
    body = tmp_path / 'body.md'
    body.write_text('# Body\n\n## ' + PERMISSIONS_GUIDE)
    ref = PaperRef(paper_id='10.1000/requested-paper', stem='requested-paper',
                   layout='nested', md_path=body, xml_path=None, pdf_path=None,
                   dir=tmp_path)
    assert read_best_text(ref, reject_boilerplate=True) == (None, 'boilerplate')


def test_download_stamp_page_is_flagged():
    assert looks_like_boilerplate(DOWNLOAD_STAMP)


def test_real_short_abstract_is_not_flagged():
    text = (
        "Effects of nattokinase on blood pressure: a randomized controlled "
        "trial. Methods: 86 participants with prehypertension were randomized "
        "to nattokinase or placebo for 8 weeks. Results: systolic blood "
        "pressure decreased significantly in the intervention group."
    )
    assert not looks_like_boilerplate(text)


def test_paper_with_incidental_boilerplate_header_is_not_flagged():
    """Real papers often carry a 'Downloaded from …' banner above the actual
    article — the substantive-marker condition must keep them."""
    text = (
        "Downloaded from journals.example.org. All rights reserved.\n\n"
        "Abstract\nWe conducted a study of 120 patients using standard "
        "methods, and the results showed a significant reduction."
    )
    assert not looks_like_boilerplate(text)


def test_single_marker_is_not_enough():
    text = "All rights reserved. " * 30
    assert not looks_like_boilerplate(text)


def test_read_best_text_reports_boilerplate_source(tmp_path):
    md = tmp_path / "10.1000--stamp"
    md.mkdir()
    body = md / "body.md"
    body.write_text(DOWNLOAD_STAMP, encoding="utf-8")
    ref = PaperRef(paper_id="10.1000/stamp", stem="10.1000--stamp",
                   layout="nested", md_path=body, xml_path=None, pdf_path=None,
                   dir=md)

    text, source = read_best_text(ref, reject_boilerplate=True)
    assert text is None
    assert source == "boilerplate"

    # Default (opt-out) behaviour unchanged: other callers still get the text.
    text, source = read_best_text(ref)
    assert text is not None
    assert source == "md"
