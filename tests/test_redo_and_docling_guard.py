"""redo_degraded_conversions, the docling re-exec guard, and 6_convert's probe."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from ber_core import docling_guard

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_t_{Path(name).stem}", ROOT / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def redo():
    return _load("scripts/maintenance/redo_degraded_conversions.py")


def _folder(store: Path, bucket: str, stem: str, mode: str) -> Path:
    d = store / bucket / stem
    d.mkdir(parents=True)
    (d / f"{stem}.pdf").write_bytes(b"%PDF")
    (d / f"{stem}.xml").write_text("<article/>")
    (d / "references.json").write_text("[]")
    (d / "supplementary").mkdir()
    (d / "supplementary" / "s1.docx").write_bytes(b"si")
    (d / "body.md").write_text("old table-free body")
    (d / "provenance.json").write_text(json.dumps({"extraction_mode": mode}))
    return d


def test_find_degraded_honours_configured_store(redo, tmp_path):
    project = tmp_path / "projects" / "demo"
    (project / "data").mkdir(parents=True)
    store = tmp_path / "elsewhere"
    _folder(store, "Clinical_Trial", "10.1--a", "grobid-only")
    _folder(store, "Clinical_Trial", "10.1--b", "docling-html")
    (project / "data" / "trial_extractions_pass2.jsonl").write_text(
        "\n".join(json.dumps({"paper_id": p, "_status": "ok"})
                  for p in ("10.1/a", "10.1/b")) + "\n")
    work = redo.find_degraded(project, store)
    assert work == {"rct": [("10.1--a", "grobid-only")]}


def test_reconvert_stages_a_scratch_store_and_keeps_only_table_bearing(redo, tmp_path, monkeypatch):
    project = tmp_path / "projects" / "demo"
    store = project / "papers"
    _folder(store, "Clinical_Trial", "10.1--a", "grobid-only")
    _folder(store, "Clinical_Trial", "10.1--b", "grobid-only")
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        scratch = Path(cmd[cmd.index("--papers-dir") + 1])
        for stem, mode in (("10.1--a", "docling-only"), ("10.1--b", "grobid-only")):
            folder = scratch / "redo" / stem
            assert (folder / f"{stem}.pdf").exists()
            (folder / "body.md").write_text(f"new body {stem} <table></table>")
            (folder / "provenance.json").write_text(json.dumps({"extraction_mode": mode}))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(redo.subprocess, "run", run)
    good = redo.reconvert(project, store, ["10.1--a", "10.1--b"], "cfg.yaml", 1,
                          scratch=tmp_path / "scratch")
    assert list(good) == ["10.1--a"]
    assert "--input-dir" not in seen["cmd"] and "--output-dir" not in seen["cmd"]
    assert "--no-auto-migrate" in seen["cmd"]


def test_install_replaces_only_conversion_outputs(redo, tmp_path):
    project = tmp_path / "projects" / "demo"
    store = project / "papers"
    home = _folder(store, "Clinical_Trial", "10.1--a", "grobid-only")
    copy = project / "eligible_rct" / "10.1--a"
    copy.mkdir(parents=True)
    (copy / "body.md").write_text("old copy")
    linked = project / "eligible_observational"
    linked.mkdir()
    (linked / "10.1--a").symlink_to(os.path.relpath(home, linked), target_is_directory=True)
    new = tmp_path / "new" / "10.1--a"
    new.mkdir(parents=True)
    (new / "body.md").write_text("new body")
    (new / "provenance.json").write_text(json.dumps({"extraction_mode": "docling-only"}))
    (new / "references.json").write_text('["should not be copied"]')
    (home / "conversion_failed.marker").write_text("x")

    written = redo.install(project, store, "10.1--a", new)

    assert set(written) == {home, copy}
    assert (home / "body.md").read_text() == "new body"
    assert (home / "10.1--a.xml").read_text() == "<article/>"
    assert (home / "references.json").read_text() == "[]"
    assert (home / "supplementary" / "s1.docx").exists()
    assert (home / "10.1--a.pdf").exists()
    assert not (home / "conversion_failed.marker").exists()
    assert (copy / "body.md").read_text() == "new body"
    assert (linked / "10.1--a").is_symlink()


def test_drop_records_keeps_backup(redo, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "trial_extractions_pass2.jsonl"
    path.write_text(json.dumps({"paper_id": "10.1/A"}) + "\n" + json.dumps({"paper_id": "10.1/b"}) + "\n")
    assert redo.drop_records(data, "rct", ["10.1--a"]) == 1
    assert json.loads(path.read_text())["paper_id"] == "10.1/b"
    assert len(list(data.glob("*.bak"))) == 1


def test_dry_run_is_the_default(redo, tmp_path, monkeypatch):
    project = tmp_path / "projects" / "demo"
    (project / "data").mkdir(parents=True)
    _folder(project / "papers", "Clinical_Trial", "10.1--a", "grobid-only")
    (project / "data" / "trial_extractions_pass2.jsonl").write_text(
        json.dumps({"paper_id": "10.1/a", "_status": "ok"}) + "\n")
    monkeypatch.setattr(redo, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(redo, "load_config", lambda _: {"topic": {"name": "demo"}})
    monkeypatch.setattr(redo, "reconvert", lambda *a, **k: pytest.fail("must not convert"))
    redo.main(["--config", "x.yaml"])
    with pytest.raises(SystemExit):
        redo.main(["--config", "x.yaml", "--dry-run", "--execute"])


# ── B16: never repair + re-exec twice ──────────────────────────────────────

def test_restarted_process_does_not_repair_again(monkeypatch):
    monkeypatch.setattr(docling_guard, "docling_importable", lambda: (False, "ImportError: x"))
    monkeypatch.setattr(docling_guard, "attempt_repair",
                        lambda *a, **k: pytest.fail("repaired twice"))
    monkeypatch.setenv("BER_FIX_DOCLING", "1")
    monkeypatch.setenv(docling_guard.REEXEC_ENV, "1")
    with pytest.raises(SystemExit) as exc:
        docling_guard.require_docling(needed=True)
    assert exc.value.code == 1


def test_reexec_marks_the_environment_once(monkeypatch):
    calls = []
    monkeypatch.delenv(docling_guard.REEXEC_ENV, raising=False)
    monkeypatch.setattr(docling_guard.os, "execv", lambda *a: calls.append(a))
    docling_guard._reexec()
    assert os.environ[docling_guard.REEXEC_ENV] == "1" and len(calls) == 1
    with pytest.raises(SystemExit):
        docling_guard._reexec()
    assert len(calls) == 1


# ── B17: HTML conversion probes docling first ──────────────────────────────

def test_html_conversion_probes_docling_before_converting(tmp_path, monkeypatch):
    conv = _load("steps/convert_pdfs.py")
    from ber_core import html_convert
    project = tmp_path / "projects" / "demo"
    folder = project / "papers" / "Review" / "10.1--a"
    folder.mkdir(parents=True)
    (folder / "10.1--a.fulltext.html").write_text("<html><body>x</body></html>")
    monkeypatch.setattr(docling_guard, "docling_importable", lambda: (False, "ImportError: x"))
    monkeypatch.setattr(conv, "docling_importable", lambda: (False, "ImportError: x"))
    monkeypatch.setattr(html_convert, "html_to_markdown",
                        lambda *_: pytest.fail("converted without docling"))
    monkeypatch.delenv(docling_guard.REEXEC_ENV, raising=False)
    monkeypatch.delenv("BER_FIX_DOCLING", raising=False)
    monkeypatch.setattr(docling_guard.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit):
        conv._convert_html_papers(project, project / "papers")
    # --allow-degraded skips the HTML phase instead of failing it.
    assert conv._convert_html_papers(project, project / "papers",
                                     allow_degraded=True) == (0, 0, 0)


def test_every_html_failure_is_a_failed_run(tmp_path, monkeypatch):
    conv = _load("steps/convert_pdfs.py")
    from ber_core import html_convert
    project = tmp_path / "projects" / "demo"
    folder = project / "papers" / "Review" / "10.1--a"
    folder.mkdir(parents=True)
    (folder / "10.1--a.fulltext.html").write_text("<html></html>")
    monkeypatch.setattr(conv, "require_docling", lambda **k: None)

    def boom(_):
        raise RuntimeError("docling crashed")

    monkeypatch.setattr(html_convert, "html_to_markdown", boom)
    assert conv._convert_html_papers(project, project / "papers") == (1, 0, 1)


def test_repair_preserves_native_source_metadata_and_copies_table_evidence(redo, tmp_path):
    project = tmp_path / "projects" / "demo"
    store = project / "papers"
    home = _folder(store, "Clinical_Trial", "10.1--a", "grobid-only")
    (home / "provenance.json").write_text(json.dumps({
        "extraction_mode": "grobid-only", "structured_source": "xml", "supplementary_files": 2,
    }))
    new = tmp_path / "new"
    new.mkdir()
    (new / "body.md").write_text("<table></table>")
    (new / "provenance.json").write_text(json.dumps({
        "extraction_mode": "docling-only", "conversion_status": "complete",
    }))
    (new / "tables.json").write_text('{"tables": []}')
    redo.install(project, store, "10.1--a", new)
    data = json.loads((home / "provenance.json").read_text())
    assert data["structured_source"] == "xml" and data["supplementary_files"] == 2
    assert data["conversion_status"] == "complete"
    assert (home / "tables.json").exists()


@pytest.mark.parametrize("old_provenance", ["not json", "[]", '{"extraction_mode":[]}'])
def test_repair_archives_corrupt_provenance_and_retires_stale_evidence(redo, tmp_path, old_provenance):
    project = tmp_path / "projects" / "demo"
    store = project / "papers"
    home = _folder(store, "Clinical_Trial", "10.1--a", "grobid-only")
    (home / "provenance.json").write_text(old_provenance)
    (home / "alternatives.json").write_text('["old numerical evidence"]')
    assert redo._mode_of(home) is None
    _load("steps/convert_pdfs.py")._report_conversion_modes({"10.1--a": home})
    new = tmp_path / "replacement"
    new.mkdir()
    (new / "body.md").write_text("new table content")
    (new / "provenance.json").write_text('{"extraction_mode":"docling-only","conversion_status":"complete"}')
    redo.install(project, store, "10.1--a", new)
    provenance = json.loads((home / "provenance.json").read_text())
    archived = home / provenance["previous_conversion_archive"]
    assert (archived / "body.md").read_text() == "old table-free body"
    assert (archived / "provenance.json").read_text() == old_provenance
    assert (archived / "alternatives.json").exists()
    assert not (home / "alternatives.json").exists()
    assert (home / "body.md").read_text() == "new table content"


def test_repair_validates_before_overwriting(redo, tmp_path):
    project = tmp_path / "projects" / "demo"
    store = project / "papers"
    home = _folder(store, "Clinical_Trial", "10.1--a", "grobid-only")
    new = tmp_path / "replacement"
    new.mkdir()
    (new / "body.md").write_text("new table content")
    (new / "provenance.json").write_text("[]")
    with pytest.raises(ValueError):
        redo.install(project, store, "10.1--a", new)
    assert (home / "body.md").read_text() == "old table-free body"
