"""download_pdfs: the generic extra-PDF-source plugin hook and exit status."""
from __future__ import annotations

import importlib.metadata
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dl(monkeypatch):
    spec = importlib.util.spec_from_file_location("_dl_plugins", ROOT / "steps/download_pdfs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeEP:
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def load(self):
        return self._fn


def _install_eps(monkeypatch, eps):
    def entry_points(group=None, **kw):
        return [ep for ep in eps if group == "ber.pdf_sources"]
    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)


def test_module_import_does_not_import_fetchpdf(dl):
    # Plugins must get to install themselves before fetchpdf binds anything.
    assert dl.batch_fetch_pdfs is None


def test_plugins_load_and_report_labels(dl, monkeypatch):
    calls = []
    _install_eps(monkeypatch, [FakeEP("alpha", lambda: calls.append("a") or "v1.2"),
                               FakeEP("beta", lambda: calls.append("b"))])
    assert dl.load_extra_sources(enabled=True) == ["alpha v1.2", "beta"]
    assert calls == ["a", "b"]


def test_disabled_loads_nothing(dl, monkeypatch):
    _install_eps(monkeypatch, [FakeEP("alpha", lambda: pytest.fail("loaded"))])
    assert dl.load_extra_sources(enabled=False) == []


def test_broken_plugin_is_reported_and_skipped(dl, monkeypatch, capsys):
    def boom():
        raise RuntimeError("nope")
    _install_eps(monkeypatch, [FakeEP("bad", boom), FakeEP("ok", lambda: None)])
    assert dl.load_extra_sources(enabled=True) == ["ok"]
    assert "'bad' failed to load" in capsys.readouterr().err


def test_no_extra_sources_flag(dl):
    parser = dl.build_parser()
    assert parser.parse_args(["--config", "c.yaml", "--no-extra-sources"]).no_extra_sources
    assert not parser.parse_args(["--config", "c.yaml"]).no_extra_sources


def test_config_is_required(dl):
    with pytest.raises(SystemExit):
        dl.build_parser().parse_args([])


def _args(classifications, **kw):
    base = dict(config="unused", input=str(classifications), output_dir=None,
                no_auto_migrate=True, types=None, relevant_only=False, structured=False,
                supplementary=False, refresh_supplementary=False, data_artifacts=False,
                max_si_mb=None, only_dois=None, limit=0, workers=1, delay=0.0,
                verbose=False, no_extra_sources=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _setup(dl, tmp_path, monkeypatch, order):
    classifications = tmp_path / "classifications.csv"
    classifications.write_text("doi,article_type,is_relevant\n10.1000/x,Clinical Trial,yes\n")
    monkeypatch.setattr(dl, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(dl, "load_config", lambda _: {"topic": {"name": "Fixture"}})
    monkeypatch.setattr(dl, "doi_to_safe_filename", lambda d: d.replace("/", "--"))
    _install_eps(monkeypatch, [FakeEP("extra", lambda: order.append("plugin"))])
    return classifications


def test_plugins_install_before_the_batch_and_zero_results_fail(dl, tmp_path, monkeypatch):
    order = []
    classifications = _setup(dl, tmp_path, monkeypatch, order)
    monkeypatch.setattr(dl, "batch_fetch_pdfs", lambda **kw: order.append("fetch"))
    with pytest.raises(SystemExit) as exc:
        dl.main(_args(classifications))
    assert exc.value.code == 1  # 0 of 1 papers yielded anything
    assert order == ["plugin", "fetch"]


def test_success_exits_cleanly_and_no_extra_sources_skips_plugins(dl, tmp_path, monkeypatch):
    order = []
    classifications = _setup(dl, tmp_path, monkeypatch, order)

    def fetch(dois, output_dir, **kw):
        order.append("fetch")
        d = Path(output_dir) / "10.1000--x"
        d.mkdir(parents=True, exist_ok=True)
        (d / "10.1000--x.pdf").write_bytes(b"%PDF")

    monkeypatch.setattr(dl, "batch_fetch_pdfs", fetch)
    dl.main(_args(classifications, no_extra_sources=True))
    assert order == ["fetch"]
    store = tmp_path / "projects" / "fixture" / "papers"
    assert (store / "Clinical_Trial" / "10.1000--x" / "10.1000--x.pdf").exists()
