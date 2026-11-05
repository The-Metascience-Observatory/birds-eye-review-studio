from ber_core.utils import gather_papers


def test_source_identifiers_reach_abstract_screening(tmp_path):
    for name in (
        "10.1000--example_abstract.md",
        "pmid_12345_abstract.md",
        "cyberleninka:stimulation-sleep_abstract.md",
        "notes_abstract.md",
    ):
        (tmp_path / name).write_text("Study abstract", encoding="utf-8")
    papers = gather_papers([tmp_path])
    assert {paper_id for paper_id, _, _ in papers} == {
        "10.1000/example", "pmid_12345", "cyberleninka:stimulation-sleep",
    }
    assert all(abstract.is_file() for _, abstract, _ in papers)
