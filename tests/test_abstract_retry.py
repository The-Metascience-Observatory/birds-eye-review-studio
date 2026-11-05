"""fetch_abstracts: transient failures are retried, confirmed absences are not,
and classify re-screens a paper once its real abstract arrives."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("Bio", reason="fetch_abstracts needs biopython")


def _load(script, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fa(monkeypatch):
    mod = _load("steps/fetch_abstracts.py", "_fa_retry")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "ENTREZ_EMAIL", "test@example.org")
    return mod


class R:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_request_distinguishes_absent_from_unreachable(fa, monkeypatch):
    import requests
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: R(404))
    assert fa._request("u") == (None, fa.ABSENT)
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: R(503))
    assert fa._request("u") == (None, fa.ERROR)

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(fa.requests, "get", boom)
    assert fa._request("u") == (None, fa.ERROR)


def test_fetch_reports_error_vs_no_abstract(fa, monkeypatch):
    sources = ["europepmc", "crossref", "openalex"]
    monkeypatch.setattr(fa, "fetch_europepmc_outcome", lambda **k: ("T", "", fa.ABSENT))
    monkeypatch.setattr(fa, "fetch_crossref_outcome", lambda doi: ("", "", fa.ABSENT))
    monkeypatch.setattr(fa, "fetch_openalex_outcome", lambda **k: ("", "", fa.ABSENT))
    assert fa._fetch_with_status("10.1/a", "", {}, sources) == ("none", "T", "", "no_abstract")
    monkeypatch.setattr(fa, "fetch_crossref_outcome", lambda doi: ("", "", fa.ERROR))
    assert fa._fetch_with_status("10.1/a", "", {}, sources)[3] == "error"
    # A legacy 3-tuple caller still works.
    assert fa._fetch_one("10.1/a", "", {}, sources) == ("none", "T", "")


def _run(fa, tmp_path, monkeypatch, outcome):
    projects = tmp_path / "projects"
    monkeypatch.setattr(fa, "PROJECTS_DIR", projects)
    monkeypatch.setattr(fa, "load_config", lambda _: {"topic": {"name": "demo"}})
    data = projects / "demo" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "search_dois.csv").write_text("doi,pmid\n10.1/a,\n")
    monkeypatch.setattr(fa, "_fetch_with_status", lambda *a, **k: outcome)
    args = SimpleNamespace(config="x", workers=1, limit=0, sources="europepmc,openalex",
                           redo=False, no_skip_classified=False)
    fa.main(args)
    return projects / "demo"


def test_transient_stub_is_retried_and_replaced(fa, tmp_path, monkeypatch):
    proj = _run(fa, tmp_path, monkeypatch, ("none", "Title", "", "error"))
    stub = proj / "abstracts" / "10.1--a_abstract.md"
    assert fa.ABSTRACT_STUB in stub.read_text()
    assert fa.TRANSIENT_NOTE in stub.read_text()
    with open(proj / "data" / "abstracts_log.csv", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["status"] == "error" and row["reason"] and row["fetched_at"]

    # Next run: the stub does not count as fetched, so the real abstract lands.
    _run(fa, tmp_path, monkeypatch, ("europepmc", "Title", "A real abstract. " * 10, ""))
    assert "A real abstract" in stub.read_text()


def test_confirmed_absence_is_not_retried(fa, tmp_path, monkeypatch):
    _run(fa, tmp_path, monkeypatch, ("none", "Title", "", "no_abstract"))
    calls = []
    monkeypatch.setattr(fa, "_fetch_with_status",
                        lambda *a, **k: calls.append(a) or ("none", "", "", "no_abstract"))
    args = SimpleNamespace(config="x", workers=1, limit=0, sources="europepmc",
                           redo=False, no_skip_classified=False)
    fa.main(args)
    assert not calls


def test_transient_retries_are_capped(fa, tmp_path):
    log = tmp_path / "abstracts_log.csv"
    with open(log, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fa.LOG_FIELDS)
        w.writeheader()
        for _ in range(fa.MAX_TRANSIENT_ATTEMPTS):
            w.writerow({"doi": "10.1/a", "status": "error"})
        w.writerow({"doi": "10.1/b", "status": "error"})
        w.writerow({"doi": "10.1/b", "status": "ok"})
    streak = fa.transient_failures(log)
    assert streak == {"10.1--a": fa.MAX_TRANSIENT_ATTEMPTS}


def test_legacy_log_header_is_upgraded(fa, tmp_path):
    log = tmp_path / "abstracts_log.csv"
    log.write_text("doi,pmid,source,status,title,chars\n10.1/a,,none,no_abstract,T,0\n")
    fa._ensure_log_schema(log)
    with open(log, newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == fa.LOG_FIELDS
        assert next(reader)["status"] == "no_abstract"


def test_email_is_required_for_ncbi_and_crossref(fa, monkeypatch):
    monkeypatch.setattr(fa, "ENTREZ_EMAIL", "")
    with pytest.raises(SystemExit) as exc:
        fa._require_email(["pubmed"])
    assert exc.value.code == 2
    fa._require_email(["europepmc", "openalex"])  # no contact needed


def test_quick_screen_readmits_a_paper_once_its_abstract_arrives(tmp_path):
    c3 = _load("steps/classify.py", "_c3_readmit")
    abst = tmp_path / "10.1--a_abstract.md"
    abst.write_text("# T\n\n*Abstract not available.*\n<!-- note -->\n")
    assert c3._is_abstract_stub(abst)
    abst.write_text("# T\n\n" + "A real abstract. " * 20)
    assert not c3._is_abstract_stub(abst)
