"""Offline checks for the optional local rescue converter; no model imports."""
import copy
import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import mineru_standard_adapter as adapter


@pytest.mark.parametrize("cells", [
    "<td>76.26 [73.33–80.9</td><td>5]</td>",
    "<td>0.00 [-</td><td>0.302-0.334]</td>",
    "<td>76.26 &#91;73.33–80.9</td><td>5&#93;</td>",
])
def test_flags_split_intervals_without_repairing_numbers(cells):
    assert adapter.table_warnings(cells) == ["numeric_interval_split_across_cells"]


def test_complete_interval_is_not_flagged():
    assert not adapter.table_warnings("<td>76.26 [73.33–80.95]</td>")


class Model(SimpleNamespace):
    def model_copy(self, *, update):
        result = copy.copy(self)
        result.__dict__.update(update)
        return result


@pytest.mark.parametrize("repeated", [False, True])
def test_tables_keep_spans_captions_footnotes_and_ambiguous_native_text(monkeypatch, repeated):
    pipe = "| Outcome | Value |\n|---|---|\n| FEV1 | 76.26 [73.33–80.95] |"
    table_html = ('<figure><figcaption>Table 3</figcaption><table><tr>'
                  '<th colspan="2">Six months</th></tr><tr><td>FEV1</td>'
                  '<td>76.26 [73.33–80.95]</td></tr></table>'
                  '<footer>Median [IQR], n=20.</footer></figure>')
    table = Model(type="table")
    page = Model(page_idx=10, blocks=[table])
    text = "Results\n\n" + pipe + ("\n\n" + pipe if repeated else "") + "\nEnd"
    result = Model(pages=[page], middle_json=Model(pages=[page]), markdown=lambda **_: text)
    renderer = Mock(return_value=table_html)
    monkeypatch.setitem(sys.modules, "mineru.config", SimpleNamespace(config=Model(render=Model(latex_delimiters={}))))
    monkeypatch.setitem(sys.modules, "mineru.render", SimpleNamespace(render_html=renderer))
    monkeypatch.setitem(sys.modules, "docvortex.render.markdown", SimpleNamespace(render_single_block=lambda *_, **__: pipe))
    markdown, tables, warnings = adapter.render_with_html_tables(result)
    assert tables == [table_html]
    assert table_html in markdown and "Results" in markdown and "End" in markdown
    assert renderer.call_args.args[0].pages[0].page_idx == 10
    if repeated:
        assert text in markdown  # Do not guess which occurrence belongs to this source table.
        assert warnings == ["table_html_placement_ambiguous:page_11"]
    else:
        assert pipe not in markdown and not warnings


@pytest.mark.parametrize("destination", ["source", "source/child", "old_candidate"])
def test_refuses_overwriting_source_or_candidate(monkeypatch, tmp_path, destination):
    source = tmp_path / "source"
    source.mkdir()
    pdf = source / "paper.pdf"
    pdf.write_bytes(b"pdf")
    out = tmp_path / destination
    if destination == "old_candidate":
        out.mkdir()
        (out / "body.md").write_text("existing evidence", encoding="utf-8")
    launch = Mock(side_effect=AssertionError("must refuse before starting a converter"))
    monkeypatch.setattr(adapter.subprocess, "Popen", launch)
    monkeypatch.setattr(sys, "argv", ["adapter", str(pdf), str(out), "--runtime-root", str(tmp_path)])
    with pytest.raises(SystemExit, match="outside|overwrite"):
        adapter.main()
    assert not launch.called
    assert pdf.read_bytes() == b"pdf"


def test_benchmark_metadata_allowed_and_selected_pages_forwarded(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pdf = source / "paper.pdf"
    pdf.write_bytes(b"pdf")
    output = tmp_path / "candidate"
    output.mkdir()
    (output / "job.json").write_text("{}", encoding="utf-8")
    (output / "worker.log").touch()
    commands = []

    def fake_launch(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert kwargs["start_new_session"]
        adapter.write_json(output / "provenance.json", {
            "runtime": {}, "conversion_status": "complete", "page_numbers": [10, 11, 12],
            "table_count": 2, "warnings": [],
        })
        return Model(wait=lambda **_: 0)

    monkeypatch.setattr(adapter.subprocess, "Popen", fake_launch)
    monkeypatch.setattr(adapter, "verify_runtime", lambda _: {"verified": True})
    monkeypatch.setattr(sys, "argv", ["adapter", str(pdf), str(output), "--runtime-root", str(tmp_path), "--pages", "10-12"])
    adapter.main()
    assert commands[0][commands[0].index("--pages") + 1] == "10-12"
    assert json.loads((output / "provenance.json").read_text(encoding="utf-8"))["conversion_status"] == "complete"


def test_selected_page_rescue_is_partial_even_when_all_requested_pages_return():
    missing, warnings, partial = adapter.page_quality(35, [9, 10, 11], [9, 10, 11], False)
    assert not missing and partial and warnings == ["selected_page_rescue"]
    assert adapter.page_quality(2, [0, 1], [0, 1], True) == ([], [], False)


@pytest.mark.parametrize("changed", ["weight", "manifest", "configuration"])
def test_runtime_rejects_changed_weights_model_lock_or_network_configuration(monkeypatch, tmp_path, changed):
    import yaml
    weight = tmp_path / "models" / "weights.onnx"
    weight.parent.mkdir()
    weight.write_bytes(b"pinned model")
    item = {"path": weight.name, "sha256": adapter.sha256(weight), "size": weight.stat().st_size}
    metadata = [{"repo": "example/local", "revision": "abc123", "files": [item]}]
    fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    monkeypatch.setattr(adapter, "MODEL_MANIFEST_SHA256", fingerprint)
    monkeypatch.setattr(adapter.importlib.metadata, "version", lambda _: adapter.PIN)
    metadata[0]["files"][0]["local_path"] = str(weight)
    adapter.write_json(tmp_path / "models-lock.json", metadata)
    (tmp_path / "home").mkdir()
    config = {"model": {"base_dir": str(tmp_path / "models"), "source": "local", "small_backend": "onnx",
                        "vlm": {"engine": "llama-cpp", "server_url": ""}},
              "llm_aided": {"features": {"title_leveling": False, "cross_page_table_cell_merge": False}}}
    config_path = tmp_path / "home" / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert adapter.verify_runtime(tmp_path)["model_files_verified"] == 1
    if changed == "weight":
        weight.write_bytes(b"changed model")
    elif changed == "manifest":
        metadata[0]["revision"] = "different"
        adapter.write_json(tmp_path / "models-lock.json", metadata)
    else:
        config["model"]["vlm"]["server_url"] = "https://example.invalid"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="changed|manifest|configuration"):
        adapter.verify_runtime(tmp_path)
