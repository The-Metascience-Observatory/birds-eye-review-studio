"""search_papers grouping/checkpoints, 4_enrich strict JSONL, and redaction."""
from __future__ import annotations

import importlib.util
import json
import logging
import time
from pathlib import Path

import pytest

from ber_core.utils import SecretRedactingFilter, redact_secrets

ROOT = Path(__file__).resolve().parents[1]


def _load(script, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def search():
    return _load("steps/search_papers.py", "_search_t")


# ── S7: keys never reach a log ─────────────────────────────────────────────

def test_redacts_query_parameter_keys():
    msg = ("HTTPSConnectionPool(host='api.openalex.org'): Max retries exceeded with "
           "url: /works?search=x&api_key=SEKRIT123&mailto=a@b.org")
    out = redact_secrets(msg)
    assert "SEKRIT123" not in out and "api_key=REDACTED" in out
    assert "mailto=a@b.org" in out
    assert "REDACTED" in redact_secrets("esummary.fcgi?db=pubmed&id=1&api_key=abc123def")


def test_redacts_auth_headers_and_env_values(monkeypatch):
    monkeypatch.setenv("SCOPUS_API_KEY", "scopus-secret-value")
    out = redact_secrets("{'X-ELS-APIKey': 'abc', 'Authorization': 'Bearer tok123'} "
                         "and later scopus-secret-value")
    assert "abc" not in out.split("X-ELS-APIKey")[1][:12]
    assert "tok123" not in out and "scopus-secret-value" not in out


def test_logging_filter_redacts_records(caplog):
    logger = logging.getLogger("_redact_test")
    logger.addFilter(SecretRedactingFilter())
    with caplog.at_level(logging.WARNING, logger="_redact_test"):
        logger.warning("Request error for %s", "https://x/?api_key=TOPSECRET9")
    assert "TOPSECRET9" not in caplog.text and "REDACTED" in caplog.text


def test_metadata_fetcher_logger_redacts():
    from ber_core import metadata_fetcher
    assert any(isinstance(f, SecretRedactingFilter) for f in metadata_fetcher.logger.filters)


def test_search_log_redacts(search, capsys):
    search._log("Request error: https://api.openalex.org/works?api_key=ZZZ999ZZZ")
    assert "ZZZ999ZZZ" not in capsys.readouterr().out


# ── B10: torn checkpoint tolerated, writes atomic ──────────────────────────

def test_torn_progress_file_is_a_warning(search, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(search, "PROGRESS_DIR", tmp_path)
    monkeypatch.setattr(search, "PROGRESS_FILE", tmp_path / "search_progress.json")
    (tmp_path / "search_progress.json").write_text('{"completed_queries": ["a"')
    assert search.load_progress() == {"completed_queries": []}
    assert "unreadable" in capsys.readouterr().out
    search.save_progress({"completed_queries": ["x"]})
    assert search.load_progress()["completed_queries"] == ["x"]
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("tmp*"))


# ── B39: linear grouping, same groups ──────────────────────────────────────

def test_group_records_merges_through_shared_keys(search):
    long_title = "A randomized trial of something rather long"
    rows = [
        {"doi": "10.1/A", "pmid": "", "title": ""},
        {"doi": "", "pmid": "7", "title": long_title},
        {"doi": "10.1/a", "pmid": "7", "title": ""},          # joins rows 0 and 1
        {"doi": "10.1/b", "pmid": "", "title": "short"},
        {"doi": "", "pmid": "", "title": long_title.upper()},  # joins via title
    ]
    groups = search.group_records(rows)
    assert [len(g) for g in groups] == [4, 1]
    assert groups[0][0]["doi"] == "10.1/A"   # first DOI in file order
    assert groups[1][0]["doi"] == "10.1/b"


def test_group_records_is_not_quadratic(search):
    n = 40_000
    rows = [{"doi": f"10.1000/{i // 2}", "pmid": str(i), "title": ""} for i in range(n)]
    start = time.perf_counter()
    groups = search.group_records(rows)
    assert len(groups) == n // 2
    assert time.perf_counter() - start < 5


def test_pubmed_needs_a_contact_email(search, tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(search, "ENTREZ_EMAIL", "")
    monkeypatch.setattr(search, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(search, "load_config", lambda _: {
        "topic": {"name": "demo"}, "search": {"pubmed": ["x"]}})
    args = SimpleNamespace(max_per_query=1, since=None, fresh=False, config="c",
                           backfill_cyberleninka_abstracts=False, sources="pubmed",
                           parallel=False)
    assert search.main(args) == 2


def test_search_no_longer_creates_a_legacy_pdfs_dir(search, tmp_path, monkeypatch):
    monkeypatch.setattr(search, "PROJECTS_DIR", tmp_path)
    search.configure_project_paths("demo")
    assert not (tmp_path / "demo" / "pdfs").exists()
    assert not hasattr(search, "rename_pdfs")


# ── PubMed's 10,000-record paging cap; OpenAlex phrase/type handling ───────

def _fake_pubmed(monkeypatch, search, entry_dates):
    """Stand in for the history server: one record per entry date in the list."""
    import datetime as dt
    calls = []

    def esearch(query, mindate=None, maxdate=None):
        lo = dt.datetime.strptime(mindate or "1800/01/01", "%Y/%m/%d").date()
        hi = dt.datetime.strptime(maxdate or "3000/12/31", "%Y/%m/%d").date()
        hits = [d for d in entry_dates if lo <= d <= hi]
        calls.append((lo, hi, len(hits)))
        return "env", (lo, hi), len(hits)

    monkeypatch.setattr(search, "_pubmed_esearch", esearch)
    monkeypatch.setattr(search, "NCBI_DELAY", 0)
    monkeypatch.setattr(search, "SEARCH_RUN", search.QueryLedger())
    search.SEARCH_RUN.begin("pubmed:q")
    return calls


def test_pubmed_query_over_the_paging_cap_is_split_by_entry_date(search, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(search, "PUBMED_WINDOW_MAX", 100)
    monkeypatch.setattr(search, "SINCE_DATE", None)
    monkeypatch.setattr(search, "MAX_PER_QUERY", 10**6)
    today = dt.date.today()
    dates = [today - dt.timedelta(days=i % 400) for i in range(950)]
    _fake_pubmed(monkeypatch, search, dates)
    windows, total = search.search_pubmed("q")
    assert total == 950
    assert sum(w[2] for w in windows) == 950          # nothing lost past the cap
    assert all(0 < w[2] <= 100 for w in windows)
    spans = sorted(w[1] for w in windows)             # disjoint date windows
    assert all(a[1] < b[0] for a, b in zip(spans, spans[1:]))
    assert search.SEARCH_RUN.complete("pubmed:q")


def test_pubmed_small_query_is_one_window_and_since_bounds_it(search, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(search, "MAX_PER_QUERY", 10**6)
    monkeypatch.setattr(search, "SINCE_DATE", "2026-01-01")
    calls = _fake_pubmed(monkeypatch, search,
                         [dt.date(2025, 6, 1), dt.date(2026, 3, 1)])
    windows, total = search.search_pubmed("q")
    assert total == 1 and [w[2] for w in windows] == [1]
    assert calls[0][0] == dt.date(2026, 1, 1)


def test_pubmed_single_day_over_the_cap_is_flagged_not_hidden(search, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(search, "PUBMED_WINDOW_MAX", 10)
    monkeypatch.setattr(search, "SINCE_DATE", None)
    monkeypatch.setattr(search, "MAX_PER_QUERY", 10**6)
    _fake_pubmed(monkeypatch, search, [dt.date(2024, 5, 5)] * 25)
    windows, total = search.search_pubmed("q")
    assert total == 25 and sum(w[2] for w in windows) == 10
    assert not search.SEARCH_RUN.complete("pubmed:q")


def test_openalex_multiword_keyword_becomes_a_phrase(search):
    assert search._openalex_term("long COVID") == '"long COVID"'
    assert search._openalex_term("melatonin") == "melatonin"
    # explicit syntax is the author's and is left alone
    assert search._openalex_term('"long COVID"') == '"long COVID"'
    assert search._openalex_term("melatonin AND insomnia") == "melatonin AND insomnia"
    assert search._openalex_term('("post-COVID" OR PASC)') == '("post-COVID" OR PASC)'


def test_openalex_searches_reviews_and_preprints(search, monkeypatch):
    seen = []
    monkeypatch.setattr(search, "OPENALEX_TITLE_SEARCHES", ["long COVID"])
    monkeypatch.setattr(search, "OPENALEX_ABSTRACT_SEARCHES", [])
    monkeypatch.setattr(search, "SINCE_DATE", None)
    monkeypatch.setattr(search, "IGNORE_PROGRESS", True)
    monkeypatch.setattr(search, "_openalex_paginate",
                        lambda params, label, max_results=None: (seen.append(params), ([], True))[1])
    search.run_openalex_searches({"completed_queries": []})
    flt = seen[0]["filter"]
    assert 'title.search:"long COVID"' in flt
    assert "review" in flt and "preprint" in flt


def test_query_drafter_does_not_exclude_preprints():
    from ber_core.llm_search import _build_prompt
    prompt = _build_prompt(topic_name="x", description="", synonyms=[],
                           mesh_terms=[], not_about=[])
    assert "Do NOT add" in prompt and "preprints" in prompt


# ── D5: enrich refuses to rewrite a file it could not fully read ───────────

def test_enrich_refuses_malformed_jsonl(tmp_path):
    enrich = _load("steps/enrich_metadata.py", "_enrich_t")
    path = tmp_path / "x.jsonl"
    path.write_text(json.dumps({"paper_id": "10.1/a"}) + "\n{torn")
    with pytest.raises(SystemExit):
        enrich.load_jsonl(path)
    path.write_text(json.dumps({"paper_id": "10.1/a"}) + "\n")
    assert enrich.load_jsonl(path) == [{"paper_id": "10.1/a"}]


def test_read_jsonl_modes(tmp_path, capsys):
    from ber_core.utils import read_jsonl
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n[1, 2]\nnot json\n\n{"b": 2}\n')
    with pytest.raises(ValueError, match=r"x.jsonl:2"):
        read_jsonl(path)
    assert read_jsonl(path, on_error="skip") == [{"a": 1}, {"b": 2}]
    assert capsys.readouterr().err == ""
    assert read_jsonl(path, on_error="warn") == [{"a": 1}, {"b": 2}]
    assert "x.jsonl:3" in capsys.readouterr().err
    assert read_jsonl(tmp_path / "missing.jsonl") == []
    with pytest.raises(ValueError):
        read_jsonl(path, on_error="ignore")
