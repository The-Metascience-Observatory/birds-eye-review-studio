"""The live evidence table (services/evidence.py).

Two properties carry it. First, the merge is PROGRESSIVE and monotonic: a row
gains columns as later pipeline artifacts appear, and a blank value in a later
tier never erases a column an earlier tier filled — otherwise a half-written
extraction file would blank out biblio the screening step already produced.
Second, the exclusion file is the same shape the pipeline's shared loader reads,
so a studio exclusion actually reaches stage_eligible / export / meta.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_evidence_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server.app.services import evidence  # noqa: E402


# --- PICO composition, the one novel bit ------------------------------------

def test_pico_is_composed_defensively_from_arms_and_outcomes() -> None:
    rec = {
        "study_design": {"arms": [
            {"type": "intervention", "intervention_name": "Nattokinase"},
            {"type": "placebo", "intervention_name": "0.9% NaCl"},
        ]},
        "participants": {"key_inclusion_criteria": ["adults with hyperlipidemia"]},
        "outcomes": [{"name": "LDL cholesterol"}, {"name": "systolic BP"}],
    }
    pico = evidence.compose_pico(rec)
    assert pico["intervention"] == ["Nattokinase"]
    assert pico["comparator"] == ["0.9% NaCl"]
    assert pico["outcome"] == ["LDL cholesterol", "systolic BP"]
    assert "hyperlipidemia" in pico["population"]


def test_pico_population_falls_back_when_no_standard_field() -> None:
    """The participants schema is project-specific. Population must compose from
    whatever scalars exist rather than assume a `description` field."""
    rec = {"participants": {"mean_age_years": 54, "percent_female": 60,
                            "mean_age_years_source": "verbatim"}}
    pop = evidence.compose_pico(rec)["population"]
    assert "mean age years: 54" in pop
    assert "source" not in pop  # provenance keys are excluded


def test_a_review_paper_has_empty_pico_not_a_crash() -> None:
    """Reviews and in-vitro papers have no arms/outcomes — empty PICO is correct,
    and must not raise."""
    pico = evidence.compose_pico({"study_design": {}, "outcomes": []})
    assert pico["intervention"] == []
    assert pico["outcome"] == []
    assert pico["population"] is None


# --- the exclusion file: the shape the pipeline actually reads ---------------

def _fake_workspace(tmp_path: Path) -> Path:
    """A workspace whose data dir resolves. Monkeypatched _data_dir avoids
    needing a full config."""
    data = tmp_path / "projects" / "x" / "data"
    data.mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("topic:\n  name: x\n")
    return tmp_path


def test_paper_abstract_reads_the_escaped_doi_file(tmp_path) -> None:
    """The detail panel fetches an abstract on demand; the file is named by the
    --escaped DOI stem, which norm_paper_id reverses."""
    ws = _fake_workspace(tmp_path)  # slug "x" → projects/x/
    abstracts = ws / "projects" / "x" / "abstracts"
    abstracts.mkdir()
    # DOI 10.1/abc → escaped stem 10.1--abc
    (abstracts / "10.1--abc_abstract.md").write_text(
        "# A title\n\nThe abstract body.", encoding="utf-8")

    got = evidence.paper_abstract(ws, "10.1/abc")
    assert got is not None and "The abstract body." in got
    # The leading `# title` header is stripped (the panel shows the title).
    assert not got.startswith("#") and "A title" not in got
    # A DOI with no file → None, not an error.
    assert evidence.paper_abstract(ws, "10.9/none") is None


def test_paper_abstract_no_abstracts_dir_is_none(tmp_path) -> None:
    ws = _fake_workspace(tmp_path)  # has data/ but no abstracts/
    assert evidence.paper_abstract(ws, "10.1/abc") is None


def test_conversion_quality_flags_table_free_conversions(tmp_path) -> None:
    """A `grobid-only` conversion carries NO tables, so extraction silently
    misses table-only outcomes. The row must say so — directory existence alone
    (the old has_fulltext signal) made degraded and good text look identical."""
    good = tmp_path / "good"
    good.mkdir()
    (good / "provenance.json").write_text(
        json.dumps({"extraction_mode": "merged-grobid-docling"}))
    (good / "body.md").write_text(
        "text\n| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |\n")
    assert evidence._conversion_quality(good) == ("merged-grobid-docling", True)

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "provenance.json").write_text(
        json.dumps({"extraction_mode": "grobid-only"}))
    (bad / "body.md").write_text("prose only, tables unreachable\n")
    assert evidence._conversion_quality(bad) == ("grobid-only", False)

    # A table-bearing mode can still yield no tables (paper genuinely has none):
    # reported honestly rather than assumed from the mode.
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "provenance.json").write_text(
        json.dumps({"extraction_mode": "merged-grobid-docling"}))
    (empty / "body.md").write_text("no tables here\n")
    assert evidence._conversion_quality(empty) == ("merged-grobid-docling", False)

    # No provenance (pre-provenance conversions) → unknown, not a false claim.
    assert evidence._conversion_quality(tmp_path / "missing") == (None, None)


def test_structured_fulltext_reads_the_install_scripts_keys(tmp_path) -> None:
    """`install_structured.py` records publisher XML under its OWN keys.

    Deliberately not as an `extraction_mode` value: a mode the table gates don't
    recognize reads as degraded, which would flag the best source in the corpus.
    """
    with_xml = tmp_path / "with_xml"
    with_xml.mkdir()
    (with_xml / "provenance.json").write_text(json.dumps({
        "extraction_mode": "grobid-only",
        "structured_source": "xml",
        "supplementary_files": 3,
    }))
    assert evidence._structured_fulltext(with_xml) == ("xml", 3)

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "provenance.json").write_text(
        json.dumps({"extraction_mode": "merged-grobid-docling"}))
    assert evidence._structured_fulltext(plain) == (None, 0)

    assert evidence._structured_fulltext(tmp_path / "missing") == (None, 0)


def test_xml_availability_does_not_hide_pdf_conversion_quality(tmp_path) -> None:
    """A publisher XML stub may be rejected in favor of the converted PDF."""
    ws = _fake_workspace(tmp_path)
    proj = ws / "projects" / "x"
    paper = proj / "converted" / "10.1--abc"
    paper.mkdir(parents=True)
    (paper / "body.md").write_text("prose only, tables unreachable\n")
    (paper / "10.1--abc.xml").write_text("<article><body><p>x</p></body></article>")
    (paper / "provenance.json").write_text(json.dumps({
        "extraction_mode": "grobid-only",
        "structured_source": "xml",
        "supplementary_files": 2,
    }))

    rows = {"10.1/abc": {"doi": "10.1/abc"}}
    evidence._mark_fulltext(ws, rows)
    row = rows["10.1/abc"]

    assert row["has_fulltext"] is True
    assert row["conversion_mode"] == "grobid-only"   # unchanged, still honest
    assert row["conversion_degraded"] is True
    assert row["conversion_requires_review"] is True
    assert row["has_xml"] is True
    assert row["has_supplement"] is True
    assert row["structured_source"] == "xml"


def test_a_paper_without_xml_still_reports_degraded(tmp_path) -> None:
    ws = _fake_workspace(tmp_path)
    proj = ws / "projects" / "x"
    paper = proj / "converted" / "10.1--abc"
    paper.mkdir(parents=True)
    (paper / "body.md").write_text("prose only\n")
    (paper / "provenance.json").write_text(
        json.dumps({"extraction_mode": "grobid-only"}))

    rows = {"10.1/abc": {"doi": "10.1/abc"}}
    evidence._mark_fulltext(ws, rows)
    row = rows["10.1/abc"]

    assert row["conversion_degraded"] is True
    assert row["has_xml"] is False
    assert row["has_supplement"] is False
    assert "structured_source" not in row


def test_paper_extraction_returns_only_ok_records_for_the_doi(tmp_path) -> None:
    """The detail panel fetches the FULL pass-2 record on demand. Failed /
    quarantine-bound records must not leak into it, and matching uses the same
    paper_id keying (escaped stems, #arm suffixes) as the evidence merge."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    (data / "trial_extractions_pass2.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"paper_id": "10.1/abc", "outcomes": [{"name": "LDL"}]},
                {"paper_id": "10.1/abc#2", "outcomes": [{"name": "BP"}]},
                {"paper_id": "10.1/abc", "_status": "failed"},
                {"paper_id": "10.9/other", "outcomes": []},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    got = evidence.paper_extraction(ws, "10.1/abc")
    assert [r["paper_id"] for r in got] == ["10.1/abc", "10.1/abc#2"]
    assert evidence.paper_extraction(ws, "10.9/none") == []


class _FakeProj:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.base_pdf_dir = None
        self.slug = "x"


def test_save_uploaded_pdf_writes_the_canonical_path(tmp_path, monkeypatch) -> None:
    """A hand-uploaded PDF lands in the paper's own folder in the store, under
    <stem>.pdf — exactly where the downloader would have put it, so the convert
    step + has_fulltext pick it up. An unscreened paper has no article type, so
    it files under _unclassified until the screen gives it one."""
    proj_dir = tmp_path / "projects" / "x"
    proj_dir.mkdir(parents=True)
    from server.app.services import pipeline_bridge
    monkeypatch.setattr(pipeline_bridge, "build_project", lambda w: _FakeProj(proj_dir))

    dest = evidence.save_uploaded_pdf(tmp_path, "10.1234/Foo.Bar", b"%PDF-1.7\n...")
    # DOI is norm_doi-lowercased then /→-- escaped.
    assert dest == (proj_dir / "papers" / "_unclassified"
                    / "10.1234--foo.bar" / "10.1234--foo.bar.pdf")
    assert dest.read_bytes().startswith(b"%PDF")


def test_save_uploaded_pdf_reuses_the_papers_existing_folder(tmp_path, monkeypatch) -> None:
    """An upload for an already-screened paper must join its other artifacts,
    not start a second folder in a different bucket."""
    proj_dir = tmp_path / "projects" / "x"
    existing = proj_dir / "papers" / "Clinical_Trial" / "10.1234--foo.bar"
    existing.mkdir(parents=True)
    (existing / "10.1234--foo.bar.xml").write_text("<article/>")
    from server.app.services import pipeline_bridge
    monkeypatch.setattr(pipeline_bridge, "build_project", lambda w: _FakeProj(proj_dir))

    dest = evidence.save_uploaded_pdf(tmp_path, "10.1234/Foo.Bar", b"%PDF-1.7\n...")

    assert dest.parent == existing
    assert (existing / "10.1234--foo.bar.xml").exists()


def test_save_uploaded_pdf_rejects_a_non_pdf(tmp_path, monkeypatch) -> None:
    proj_dir = tmp_path / "projects" / "x"
    proj_dir.mkdir(parents=True)
    from server.app.services import pipeline_bridge
    monkeypatch.setattr(pipeline_bridge, "build_project", lambda w: _FakeProj(proj_dir))

    import pytest
    with pytest.raises(ValueError, match="not a PDF"):
        evidence.save_uploaded_pdf(tmp_path, "10.1/x", b"<html>not a pdf</html>")
    with pytest.raises(ValueError, match="valid DOI"):
        evidence.save_uploaded_pdf(tmp_path, "", b"%PDF-1.7")


def test_exclusions_round_trip_and_use_the_shared_shape(tmp_path, monkeypatch) -> None:
    from ber_core.utils import load_exclusion_dois

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)

    n = evidence.set_exclusions(ws, ["10.1/A", "10.2/B"], "off-topic", excluded=True)
    assert n == 2

    path = data / "user_exclusions.json"
    # The SAME loader the pipeline (stage_eligible, export, meta) uses must read it.
    loaded = load_exclusion_dois(path)
    assert "10.1/a" in loaded and "10.2/b" in loaded

    # Include one back.
    assert evidence.set_exclusions(ws, ["10.1/A"], "", excluded=False) == 1
    assert "10.1/a" not in load_exclusion_dois(path)
    assert "10.2/b" in load_exclusion_dois(path)


def test_excluding_an_already_excluded_doi_is_a_no_op(tmp_path, monkeypatch) -> None:
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    evidence.set_exclusions(ws, ["10.1/A"], "r", excluded=True)
    assert evidence.set_exclusions(ws, ["10.1/A"], "r", excluded=True) == 0


def test_inclusions_round_trip_and_use_the_shared_shape(tmp_path, monkeypatch) -> None:
    """The force-include file is the bare {doi: reason} shape the pipeline's shared
    load_inclusion_dois reads — so a studio force-include reaches stage_eligible."""
    from ber_core.utils import load_inclusion_dois

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)

    n = evidence.set_inclusions(ws, ["10.1/A", "10.2/B"], "want it", included=True)
    assert n == 2
    path = data / "user_inclusions.json"
    loaded = load_inclusion_dois(path)
    assert "10.1/a" in loaded and "10.2/b" in loaded

    # Remove one back (an exclude clears the force-include).
    assert evidence.set_inclusions(ws, ["10.1/A"], "", included=False) == 1
    assert "10.1/a" not in load_inclusion_dois(path)
    assert "10.2/b" in load_inclusion_dois(path)


# --- the progressive merge ---------------------------------------------------

def test_a_later_blank_tier_never_erases_an_earlier_filled_field(tmp_path, monkeypatch) -> None:
    """The load-bearing merge property. Screening writes a title; a pass-2
    record for the same paper with no title must NOT blank it."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "trial_screening.csv").write_text(
        "doi,paper_title,paper_authors,paper_journal,paper_year,trial_type,is_excluded,exclusion_reason\n"
        "10.1/a,A Real Trial,Smith J,Lancet,2020,rct,no,\n")
    # A pass-2 record with NO title/journal for the same paper.
    rec = {"paper_id": "10.1/a", "_status": "ok", "study_design": {"arms": []},
           "participants": {}, "sample_sizes": {}, "outcomes": [],
           "risk_of_bias": {}}
    (data / "trial_extractions_pass2.jsonl").write_text(json.dumps(rec) + "\n")

    out = evidence.build_evidence(ws)
    row = next(r for r in out["rows"] if r["doi"] == "10.1/a")
    assert row["title"] == "A Real Trial", "pass-2 blank erased the screening title"
    assert row["extracted"] is True
    assert out["stage_reached"] == "extract"


def test_search_raw_title_surfaces_when_nothing_else_has_one(tmp_path, monkeypatch) -> None:
    """A DOI whose enrichment found nothing and that never reached screening still
    shows the title the search captured in search_raw.csv — not a bare DOI."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,\n")
    (data / "search_raw.csv").write_text(
        "doi,pmid,title,source_api,source_query\n"
        "10.1/a,,A Search-Time Title,openalex,q\n")

    row = next(r for r in evidence.build_evidence(ws)["rows"] if r["doi"] == "10.1/a")
    assert row["title"] == "A Search-Time Title"


def test_a_real_enrichment_title_overrides_the_search_raw_title(tmp_path, monkeypatch) -> None:
    """The search-time title is the LOWEST-priority title: a real enrichment
    (or screening) title must win, so the fallback never masks better data."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_raw.csv").write_text(
        "doi,pmid,title,source_api,source_query\n"
        "10.1/a,,Rough Search Title,openalex,q\n")
    md = {"10.1/a": {"title": "The Proper Enriched Title", "authors": "Doe J",
                     "metadata_source": "crossref"}}

    row = evidence.build_evidence(ws, metadata=md)["rows"][0]
    assert row["title"] == "The Proper Enriched Title"


def test_rows_join_on_normalized_doi_across_tiers(tmp_path, monkeypatch) -> None:
    """search_dois and trial_screening for the same paper collapse to ONE row,
    even when one carries a `--`-escaped DOI shape."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,111\n")
    (data / "relevance.csv").write_text(
        "doi,is_relevant,confidence,studies_treatment\n10.1/a,yes,high,yes\n")

    out = evidence.build_evidence(ws)
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["pmid"] == "111" and row["relevance"] == "yes"
    assert out["stage_reached"] == "search"


def test_an_unbuilt_review_yields_an_empty_table_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(evidence, "_data_dir", lambda w: None)
    out = evidence.build_evidence(tmp_path)
    assert out == {"rows": [], "stage_reached": "setup", "counts": {}}


def test_manual_exclusion_marks_the_row_excluded(tmp_path, monkeypatch) -> None:
    """A DOI in user_exclusions.json wins over the pipeline verdict: the row is
    is_excluded=True with a reason, so the table shows red and it survives a
    refetch (the toggle isn't just optimistic client state)."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    # Two screened-in papers; the user manually excludes one of them.
    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,1\n10.1/b,2\n")
    (data / "user_exclusions.json").write_text(json.dumps({"10.1/b": "off-topic"}))

    out = evidence.build_evidence(ws)
    by_doi = {r["doi"]: r for r in out["rows"]}
    assert by_doi["10.1/b"]["is_excluded"] is True
    assert by_doi["10.1/b"]["exclusion_reason"] == "off-topic"
    # The other paper is untouched.
    assert not by_doi["10.1/a"].get("is_excluded")
    assert out["counts"]["excluded"] == 1


def test_screen_state_is_pending_after_abstract_screen_before_fulltext_verdict(tmp_path, monkeypatch) -> None:
    """Before the full-text eligibility screen (pass 1) rules, a paper screened IN
    on the abstract is PENDING, not Included: the green verdict comes from the
    full-text pass. Off-topic / undecided papers are screened_out, not pending."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,1\n10.1/b,2\n10.1/c,3\n10.1/d,4\n")
    (data / "relevance.csv").write_text(
        "doi,is_relevant,confidence\n"
        "10.1/a,yes,high\n"     # relevant, no pass-1 verdict yet → pending
        "10.1/b,no,high\n"      # off-topic → screened_out
        "10.1/c,unknown,low\n"  # undecided → screened_out
        # 10.1/d has no relevance row yet → screened_out
    )
    by_doi = {r["doi"]: r for r in evidence.build_evidence(ws)["rows"]}
    assert by_doi["10.1/a"]["screen_state"] == "pending"
    assert by_doi["10.1/a"]["included"] is False  # not green until pass-1 rules
    assert by_doi["10.1/b"]["screen_state"] == "screened_out"
    assert by_doi["10.1/c"]["screen_state"] == "screened_out"
    assert by_doi["10.1/d"]["screen_state"] == "screened_out"


def test_organism_from_pass1_overrides_the_abstract_guess(tmp_path, monkeypatch) -> None:
    """Tier-3 carries the abstract organism guess; Tier-5 (pass-1, full text)
    overrides it — it read the methods."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)
    monkeypatch.setattr(evidence, "_include_animal_studies", lambda w: True)  # don't gate here

    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type,is_excluded,organism\n"
        "10.1/a,yes,Clinical Trial,no,mouse\n")  # abstract guessed mouse
    (data / "trial_extractions.jsonl").write_text(json.dumps(
        {"paper_id": "10.1/a",
         "screening": {"eligible_for_pass2": True,
                       "organism": {"verdict": "human", "reasoning": "human RCT"}}}) + "\n")

    row = evidence.build_evidence(ws)["rows"][0]
    assert row["organism"] == "human"  # pass-1 wins


def test_animal_papers_are_screened_out_by_default(tmp_path, monkeypatch) -> None:
    """Human-only (default): a known non-human organism reads screened_out up
    front — matching the pass-1 gate — even before the full-text screen runs."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)
    monkeypatch.setattr(evidence, "_include_animal_studies", lambda w: False)

    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type,is_excluded,organism\n"
        "10.1/human,yes,Clinical Trial,no,human\n"
        "10.1/mouse,yes,Clinical Trial,no,mouse\n"
        "10.1/other,yes,Clinical Trial,no,other_animal\n")
    by_doi = {r["doi"]: r for r in evidence.build_evidence(ws)["rows"]}
    # Relevant human → pending (awaiting the full-text verdict); animals → out.
    assert by_doi["10.1/human"]["screen_state"] == "pending"
    assert by_doi["10.1/mouse"]["screen_state"] == "screened_out"
    assert by_doi["10.1/other"]["screen_state"] == "screened_out"


def test_include_animal_studies_toggle_stops_gating_organism(tmp_path, monkeypatch) -> None:
    """With the review opted into animal studies, organism no longer gates: an
    animal paper is treated like any other screened-in paper (pending)."""
    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)
    monkeypatch.setattr(evidence, "_include_animal_studies", lambda w: True)

    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type,is_excluded,organism\n"
        "10.1/mouse,yes,Clinical Trial,no,mouse\n")
    row = evidence.build_evidence(ws)["rows"][0]
    assert row["screen_state"] == "pending"  # not gated out anymore


def test_force_include_overrides_the_animal_gate(tmp_path, monkeypatch) -> None:
    """A manual force-include beats the default animal exclusion."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)
    monkeypatch.setattr(evidence, "_include_animal_studies", lambda w: False)

    (data / "classifications.csv").write_text(
        "doi,is_relevant,article_type,is_excluded,organism\n"
        "10.1/mouse,yes,Clinical Trial,no,mouse\n")
    (data / "user_inclusions.json").write_text(json.dumps({"10.1/mouse": "want this mouse study"}))

    row = evidence.build_evidence(ws)["rows"][0]
    assert row["screen_state"] == "included"


def test_fulltext_verdict_flips_pending_to_included_or_screened_out(tmp_path, monkeypatch) -> None:
    """Once pass-1 writes trial_extractions.jsonl, eligible→Included, not→
    screened_out. Key-presence of `eligible` distinguishes 'ran, ineligible' from
    'hasn't run' (pending)."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,1\n10.1/b,2\n10.1/c,3\n")
    (data / "relevance.csv").write_text(
        "doi,is_relevant,confidence\n10.1/a,yes,high\n10.1/b,yes,high\n10.1/c,yes,high\n")
    # Pass-1 ruled on a (eligible) and b (ineligible); c has no record → pending.
    (data / "trial_extractions.jsonl").write_text(
        json.dumps({"paper_id": "10.1/a", "screening": {"eligible_for_pass2": True}}) + "\n" +
        json.dumps({"paper_id": "10.1/b", "screening": {"eligible_for_pass2": False}}) + "\n")

    by_doi = {r["doi"]: r for r in evidence.build_evidence(ws)["rows"]}
    assert by_doi["10.1/a"]["screen_state"] == "included"
    assert by_doi["10.1/b"]["screen_state"] == "screened_out"
    assert by_doi["10.1/c"]["screen_state"] == "pending"


def test_force_include_overrides_the_fulltext_ineligible_verdict(tmp_path, monkeypatch) -> None:
    """A manual force-include (user_inclusions.json) wins over pass-1: a paper the
    full-text screen called ineligible becomes Included when the user includes it."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,1\n")
    (data / "relevance.csv").write_text("doi,is_relevant,confidence\n10.1/a,yes,high\n")
    (data / "trial_extractions.jsonl").write_text(
        json.dumps({"paper_id": "10.1/a", "screening": {"eligible_for_pass2": False}}) + "\n")
    (data / "user_inclusions.json").write_text(json.dumps({"10.1/a": "I want this one"}))

    row = evidence.build_evidence(ws)["rows"][0]
    assert row["screen_state"] == "included"
    assert row["included"] is True


def test_manually_excluding_a_relevant_paper_drops_its_included_flag(tmp_path, monkeypatch) -> None:
    """A relevant paper the user excludes is no longer included (red beats green)."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,1\n")
    (data / "relevance.csv").write_text("doi,is_relevant,confidence\n10.1/a,yes,high\n")
    (data / "user_exclusions.json").write_text(json.dumps({"10.1/a": "on second look, off-topic"}))

    row = evidence.build_evidence(ws)["rows"][0]
    assert row["is_excluded"] is True
    assert row["included"] is False


def test_manual_exclusion_uses_a_default_reason_when_none_given(tmp_path, monkeypatch) -> None:
    """An exclusion written with an empty reason still reads as excluded, with a
    human-legible fallback label rather than a blank."""
    import json

    ws = _fake_workspace(tmp_path)
    data = ws / "projects" / "x" / "data"
    monkeypatch.setattr(evidence, "_data_dir", lambda w: data)
    monkeypatch.setattr(evidence, "_mark_fulltext", lambda w, d: None)

    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,1\n")
    (data / "user_exclusions.json").write_text(json.dumps({"10.1/a": ""}))

    row = evidence.build_evidence(ws)["rows"][0]
    assert row["is_excluded"] is True
    assert row["exclusion_reason"] == "Manually excluded"


def test_partial_conversion_is_visible_even_with_tables(tmp_path):
    ws = _fake_workspace(tmp_path)
    paper = ws / "projects" / "x" / "papers" / "Clinical_Trial" / "10.1--partial"
    paper.mkdir(parents=True)
    (paper / "body.md").write_text("<table><tr><td>42</td></tr></table>")
    (paper / "provenance.json").write_text(json.dumps({
        "extraction_mode": "docling-only", "conversion_status": "partial",
        "warnings": ["missing page at /home/private/paper.pdf"], "tables_captured": 1,
    }))
    rows = {"10.1/partial": {"doi": "10.1/partial"}}
    evidence._mark_fulltext(ws, rows)
    row = rows["10.1/partial"]
    assert row["tables_captured"] is True
    assert row["conversion_degraded"] is True
    assert row["conversion_requires_review"] is True
    assert row["conversion_status"] == "partial"
    assert "/home/private" not in str(row["conversion_warnings"])


def test_studio_conversion_uses_content_profile_at_every_memory_level(monkeypatch):
    from app.services import runner
    for critical in (False, True):
        monkeypatch.setattr(runner.capability, "critical_memory", lambda: critical)
        monkeypatch.setattr(runner.capability, "convert_workers", lambda: 1)
        options = runner._conversion_options()
        assert options[options.index("--profile") + 1] == "content"


def test_malformed_conversion_metadata_does_not_break_evidence(tmp_path):
    ws = _fake_workspace(tmp_path)
    paper = ws / "projects" / "x" / "papers" / "Clinical_Trial" / "10.1--partial"
    paper.mkdir(parents=True)
    (paper / "body.md").write_text("<table><tr><td>42</td></tr></table>")
    for malformed in ("[]", '{"extraction_mode":[]}'):
        (paper / "provenance.json").write_text(malformed)
        rows = {"10.1/partial": {"doi": "10.1/partial"}}
        evidence._mark_fulltext(ws, rows)
        assert rows["10.1/partial"]["conversion_requires_review"] is True
