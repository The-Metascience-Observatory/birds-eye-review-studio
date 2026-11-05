"""Papers with no abstract must not be re-screened on every run.

`unknown` is deliberately a RETRY value in the done-check, so a paper is
re-screened once step 2 fetches real text. But for a title-only stub that text
never arrives, so the row re-queued forever and cost an LLM call each run --
772 papers on the long_covid corpus. The fix skips them while the file is still
a stub, and must re-admit them the moment it is not.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("_c3", ROOT / "steps/classify.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


class TestIsAbstractStub:
    def test_stub_detected(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("# Some Title\n\n*Abstract not available.*\n")
        assert _mod()._is_abstract_stub(f) is True

    def test_real_abstract_is_not_a_stub(self, tmp_path):
        f = tmp_path / "b.md"
        f.write_text("# Title\n\nBackground: this is a real abstract. " * 20)
        assert _mod()._is_abstract_stub(f) is False

    def test_missing_path_is_treated_as_stub(self):
        assert _mod()._is_abstract_stub(None) is True


class TestLoadNoAbstractDois:
    def test_reads_no_abstract_rows_only(self, tmp_path):
        csv_path = tmp_path / "classifications.csv"
        csv_path.write_text(
            "doi,article_type,is_relevant\n"
            "10.1/a,no_abstract,unknown\n"
            "10.1/b,Original Research,yes\n"
            "10.1/c,no_abstract,unknown\n")
        got = _mod()._load_no_abstract_dois(csv_path)
        assert got == {"10.1/a", "10.1/c"}

    def test_missing_file_is_empty(self, tmp_path):
        assert _mod()._load_no_abstract_dois(tmp_path / "nope.csv") == set()


def test_a_stub_paper_is_skipped_but_a_filled_one_returns(tmp_path):
    """The whole point: skip while stubbed, re-admit when real text lands."""
    m = _mod()
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("doi,article_type,is_relevant\n10.1/x,no_abstract,unknown\n")
    prior = m._load_no_abstract_dois(csv_path)

    stub = tmp_path / "x.md"
    stub.write_text("# T\n\n*Abstract not available.*\n")
    assert ("10.1/x" in prior and m._is_abstract_stub(stub)) is True   # skipped

    stub.write_text("# T\n\nBackground: a real abstract arrived. " * 20)
    assert ("10.1/x" in prior and m._is_abstract_stub(stub)) is False  # re-admitted
