"""The local SciSciNet tier: a read-first fast path in front of the API waterfall.

Two properties carry it. First, it is strictly an OPTIMIZATION — unset, missing, or
broken, every entry point degrades to the previous pure-API behaviour rather than
raising into a pipeline run. Second, write-back must make the SECOND run free: once
the API-only fields (volume/issue/pages) are persisted, a complete local row short-
circuits the waterfall with zero network calls. That second property is asserted
directly, with a fetcher that raises if called, rather than inferred from timing.

Write-back is also refused on FUSE filesystems (SQLite WAL locking is unreliable
there, and the dev copy of journals.db is ~1.5 TB), so these tests build a small
fixture on the local ext4 tmpdir instead of touching a real database.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _build_fixture(d: Path) -> None:
    """A miniature SciSciNet: one paper, with a duplicated author row."""
    d.mkdir(parents=True, exist_ok=True)
    j = sqlite3.connect(d / "journals.db")
    j.execute("CREATE TABLE paper_summary(paperid VARCHAR, title VARCHAR, doi VARCHAR,"
              " year BIGINT, cited_by_count BIGINT, sourceid VARCHAR, source_name VARCHAR)")
    j.execute("CREATE INDEX idx_paper_summary_doi ON paper_summary(doi)")
    j.execute("CREATE TABLE paper_authors(paperid VARCHAR, authorid VARCHAR,"
              " display_name VARCHAR, author_position VARCHAR, institutionid VARCHAR,"
              " institution_name VARCHAR)")
    j.execute("INSERT INTO paper_summary VALUES"
              "('W1','Test Paper','https://doi.org/10.1/test',2021,5,'S1','Test Journal')")
    # Duplicate rows are real in the upstream data (one sampled paper had 956).
    for name in ("Alice Smith", "Bob Jones", "Alice Smith"):
        j.execute("INSERT INTO paper_authors VALUES('W1','A',?,'middle',NULL,NULL)", (name,))
    j.commit()
    j.close()

    a = sqlite3.connect(d / "abstracts.db")
    a.execute("CREATE TABLE paper_abstracts(paperid TEXT PRIMARY KEY, title TEXT,"
              " abstract TEXT, language TEXT)")
    a.execute("INSERT INTO paper_abstracts VALUES('W1','Test Paper',?,'en')", ("x" * 300,))
    a.commit()
    a.close()

    p = sqlite3.connect(d / "papers_extra.db")
    p.execute("CREATE TABLE paper_pubmed(paperid VARCHAR, pmid BIGINT, pmcid BIGINT)")
    p.execute("CREATE TABLE paper_core(paperid VARCHAR, title VARCHAR, doi VARCHAR,"
              " publication_date VARCHAR, doc_type VARCHAR, language VARCHAR,"
              " cited_by_count BIGINT, is_retracted BIGINT)")
    p.execute("INSERT INTO paper_pubmed VALUES('W1',12345,999)")
    p.commit()
    p.close()


@pytest.fixture()
def tier(monkeypatch):
    """A configured, writable local tier over a throwaway fixture."""
    d = Path(tempfile.mkdtemp(prefix="ber_sciscinet_")) / "data"
    _build_fixture(d)
    monkeypatch.setenv("BER_SCISCINET_DIR", str(d))
    monkeypatch.setenv("BER_SCISCINET_WRITE", "1")
    from ber_core import local_sciscinet

    importlib.reload(local_sciscinet)  # module caches connections + latches
    return local_sciscinet


@pytest.fixture()
def tier_off(monkeypatch):
    """No tier configured at all — the default for anyone without the DBs."""
    monkeypatch.delenv("BER_SCISCINET_DIR", raising=False)
    monkeypatch.delenv("BER_SCISCINET_WRITE", raising=False)
    from ber_core import local_sciscinet

    importlib.reload(local_sciscinet)
    return local_sciscinet


# --- it must never become a dependency ---------------------------------------

def test_unconfigured_tier_is_inert(tier_off):
    """With no DBs, every entry point returns empty rather than raising."""
    assert tier_off.available() is False
    assert tier_off.writable() is False
    assert tier_off.lookup_metadata(doi="10.1/test") is None
    assert tier_off.lookup_abstract(doi="10.1/test") is None
    assert tier_off.lookup_batch(["10.1/test"]) == {}
    # Writes are silent no-ops, not errors.
    tier_off.save_metadata("10.1/test", {"volume": "1"})
    tier_off.save_abstract(doi="10.1/test", abstract="y" * 100)
    assert tier_off.flush() == 0


def test_missing_directory_degrades_quietly(monkeypatch):
    monkeypatch.setenv("BER_SCISCINET_DIR", "/nonexistent/path/xyz")
    from ber_core import local_sciscinet

    importlib.reload(local_sciscinet)
    assert local_sciscinet.available() is False
    assert local_sciscinet.lookup_metadata(doi="10.1/test") is None


def test_write_is_refused_on_fuse(tier, monkeypatch):
    """journals.db is ~1.5 TB on a FUSE mount in dev; WAL locking is unreliable
    there, so the flag alone must not be enough to enable writes."""
    monkeypatch.setattr(tier, "_fstype", lambda p: "fuseblk")
    assert tier.writable() is False
    assert tier.available() is True  # reads still work


# --- reads --------------------------------------------------------------------

def test_metadata_lookup_maps_the_schema(tier):
    m = tier.lookup_metadata(doi="10.1/test")
    assert m["title"] == "Test Paper"
    assert m["journal"] == "Test Journal"   # source_name, not a journals table join
    assert m["year"] == "2021"
    assert m["pmid"] == "12345"
    # author_position is only first/middle/last, so order comes from rowid; and
    # duplicate rows must collapse.
    assert m["authors"] == "Alice Smith; Bob Jones"
    # Absent upstream — this is exactly what the API waterfall tops up.
    assert (m["volume"], m["issue"], m["pages"]) == (None, None, None)


def test_doi_matching_is_case_and_form_insensitive(tier):
    """search_dois.csv stores the `--` form; stored DOIs are lowercase URLs."""
    for form in ("10.1/test", "10.1--test", "10.1/TEST", "https://doi.org/10.1/test"):
        assert tier.lookup_metadata(doi=form) is not None, form


def test_pmid_lookup_and_misses(tier):
    assert tier.lookup_metadata(doi="10.404/absent") is None
    assert tier.lookup_abstract(doi="10.404/absent") is None


def test_abstract_lookup_honors_the_length_floor(tier):
    assert tier.lookup_abstract(doi="10.1/test")[1].startswith("xxx")
    tier.save_abstract(doi="10.5/short", title="T", abstract="tiny")
    tier.flush()
    assert tier.lookup_abstract(doi="10.5/short") is None


def test_batch_lookup_is_keyed_by_normalized_doi(tier):
    out = tier.lookup_batch(["10.1--test", "10.404/absent"])
    assert set(out) == {"10.1/test"}


# --- write-back: the point is that run 2 is free ------------------------------

def test_biblio_writeback_completes_the_row(tier):
    """The three API-only fields persist into paper_summary and read back."""
    m = tier.lookup_metadata(doi="10.1/test")
    tier.save_metadata("10.1/test", {**m, "volume": "397", "issue": "10269",
                                     "pages": "99-111"})
    assert tier.flush() >= 1

    importlib.reload(tier)  # fresh process would see the same
    again = tier.lookup_metadata(doi="10.1/test")
    assert (again["volume"], again["issue"], again["pages"]) == ("397", "10269", "99-111")
    # Upstream columns untouched.
    assert again["title"] == "Test Paper" and again["journal"] == "Test Journal"


def test_writeback_never_overwrites_upstream_values(tier):
    tier.save_metadata("10.1/test", {"title": "WRONG", "journal": "WRONG",
                                     "volume": "1", "issue": "2", "pages": "3"})
    tier.flush()
    importlib.reload(tier)
    m = tier.lookup_metadata(doi="10.1/test")
    assert m["title"] == "Test Paper" and m["journal"] == "Test Journal"
    assert m["volume"] == "1"


def test_papers_absent_upstream_round_trip(tier):
    """A paper SciSciNet doesn't have goes to our own table — otherwise it would
    be re-fetched from the network on every single run, defeating write-back."""
    tier.save_metadata("10.9/new", {"title": "Brand New", "journal": "J New",
                                    "year": "2026", "authors": "Carol Lee",
                                    "volume": "12", "issue": "3", "pages": "45-67"})
    tier.save_abstract(doi="10.9/new", title="Brand New", abstract="z" * 300)
    tier.flush()

    importlib.reload(tier)
    m = tier.lookup_metadata(doi="10.9/new")
    assert m is not None and m["title"] == "Brand New" and m["pages"] == "45-67"
    assert tier.lookup_abstract(doi="10.9/new")[1].startswith("zzz")


def test_second_run_makes_zero_network_calls(tier):
    """The whole point of write-back, asserted directly: after the gaps are
    filled, fetch_metadata_from_doi must return WITHOUT touching the network."""
    from ber_core import metadata_fetcher

    m = tier.lookup_metadata(doi="10.1/test")
    tier.save_metadata("10.1/test", {**m, "volume": "397", "issue": "10269",
                                     "pages": "99-111"})
    tier.flush()
    importlib.reload(tier)

    def explode(*a, **k):  # any API attempt is a test failure
        raise AssertionError("network was called on a complete local hit")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(metadata_fetcher, "_fetch_metadata_via_apis", explode)
    try:
        got = metadata_fetcher.fetch_metadata_from_doi("10.1/test")
    finally:
        monkey.undo()
    assert got["title"] == "Test Paper" and got["pages"] == "99-111"


def test_middle_initials_do_not_force_a_network_call():
    """Regression: is_complete() counts "Andrew E. Simor" as abbreviated, so
    gating the local short-circuit on it sent perfectly good SciSciNet author
    lists back to the APIs on EVERY run, silently defeating write-back. Measured
    on real data this was 6 of 40 papers."""
    from ber_core import metadata_fetcher as MF

    # A real SciSciNet list: mostly full first names, with one initials-only
    # author near the end. is_complete()'s all-or-nothing rule rejects the WHOLE
    # list for that one entry, so the paper re-hit the APIs on every run.
    real = ("Andrew E. Simor; Emma Jane Phillips; Allison McGeer; Ana Konvalinka; "
            "Mark Loeb; H. R. Devlin")
    assert MF._authors_have_abbreviations(real) is True   # the false positive
    assert MF._authors_are_usable(real) is True           # the new gate accepts it

    # Genuinely initials-only lists are still worth an API call.
    assert MF._authors_are_usable("A. E. Simor; M. Loeb") is False
    assert MF._authors_are_usable("") is False


def test_partial_hit_still_calls_apis_and_merges(tier):
    """Without the biblio fields the waterfall must run — but only to fill gaps,
    keeping the local title/authors/journal/year."""
    from ber_core import metadata_fetcher

    calls = []

    def fake_apis(doi, *a, **k):
        calls.append(doi)
        return {"authors": None, "title": None, "journal": None, "volume": "9",
                "issue": "2", "pages": "1-10", "year": None, "url": None}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(metadata_fetcher, "_fetch_metadata_via_apis", fake_apis)
    try:
        got = metadata_fetcher.fetch_metadata_from_doi("10.1/test")
    finally:
        monkey.undo()

    assert calls == ["10.1/test"]
    assert got["title"] == "Test Paper"      # local value preserved
    assert got["authors"] == "Alice Smith; Bob Jones"
    assert got["pages"] == "1-10"            # gap filled by the API


def test_fetcher_is_unaffected_when_tier_is_off(tier_off):
    """The pure-API path must behave exactly as before the tier existed."""
    from ber_core import metadata_fetcher

    sentinel = {"title": "From API", "authors": "X Y", "journal": "J",
                "year": "2020", "volume": None, "issue": None, "pages": None,
                "url": None}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(metadata_fetcher, "_fetch_metadata_via_apis",
                   lambda doi, *a, **k: sentinel)
    try:
        assert metadata_fetcher.fetch_metadata_from_doi("10.1/test") == sentinel
    finally:
        monkey.undo()


def test_shouty_caps_surnames_are_recased():
    """Some publishers deposit family names in ALL CAPS in Crossref
    ('Ji Young KIM'). Only all-caps runs of 3+ letters are recased, so initials
    and Roman-numeral suffixes keep their capitals."""
    from ber_core import metadata_fetcher as MF

    assert MF._fix_author_caps(
        "Ji Young KIM; Si Nae GUM; Kazuya OGASAWARA"
    ) == "Ji Young Kim; Si Nae Gum; Kazuya Ogasawara"
    assert MF._fix_author_caps("Smith JK; A. H. Lee") == "Smith JK; A. H. Lee"
    assert MF._fix_author_caps("John Smith III") == "John Smith III"
    assert MF._fix_author_caps("GARCÍA") == "García"
    assert MF._fix_author_caps(None) is None


def test_fetched_authors_are_recased(tier_off):
    """The recasing is applied on the pure-API exit path of
    fetch_metadata_from_doi, before the result reaches callers."""
    from ber_core import metadata_fetcher

    meta = {"title": "T", "authors": "Ji Young KIM; Sungha PARK",
            "journal": None, "year": None, "volume": None, "issue": None,
            "pages": None, "url": None}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(metadata_fetcher, "_fetch_metadata_via_apis",
                   lambda doi, *a, **k: meta)
    try:
        got = metadata_fetcher.fetch_metadata_from_doi("10.1/test")
    finally:
        monkey.undo()
    assert got["authors"] == "Ji Young Kim; Sungha Park"


# --- the abstract waterfall ---------------------------------------------------

def test_local_source_short_circuits_the_abstract_waterfall(tier):
    """`local` runs before pubmed; a hit must return without calling any source."""
    # fetch_abstracts pulls biopython, which the lean SERVER venv deliberately
    # lacks (pipeline scripts run on the base interpreter — see CLAUDE.md), so
    # this assertion only runs where those deps exist.
    pytest.importorskip("Bio", reason="pipeline deps not in the server venv")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fetch_abstracts_mod", REPO_ROOT / "steps/fetch_abstracts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def explode(*a, **k):
        raise AssertionError("a network source was called despite a local hit")

    mod.fetch_europepmc = explode
    mod.fetch_crossref = explode
    mod.fetch_openalex = explode

    source, title, abstract = mod._fetch_one(
        "10.1/test", "", {}, ["local", "pubmed", "europepmc", "crossref", "openalex"])
    assert source == "local"
    assert title == "Test Paper"
    assert abstract.startswith("xxx")
