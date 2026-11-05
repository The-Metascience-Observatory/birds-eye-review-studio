import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ber_core.conversion import conversion_quality, read_conversion_quality
from ber_core.supplement_import import file_hash, import_supplements


@pytest.mark.parametrize("status", ["partial", "degraded", "failed"])
def test_table_engine_does_not_hide_partial_conversion(status):
    quality = conversion_quality({"extraction_mode": "docling-only", "conversion_status": status,
                                  "tables_captured": 5})
    assert quality["requires_review"] and quality["degraded"]


def test_legacy_quality_is_unknown_and_no_tables_can_be_valid():
    quality = conversion_quality({"extraction_mode": "docling-only", "tables_captured": 0})
    assert quality["status"] == "unknown"
    assert not quality["requires_review"]


def test_serialization_warning_is_not_hidden_by_complete_status():
    quality = conversion_quality({"conversion_status": "complete", "extraction_mode": "docling-only",
                                  "warnings": ["HTML table serialization failed"]})
    assert quality["requires_review"]


def test_corrupt_provenance_is_explicit(tmp_path):
    (tmp_path / "provenance.json").write_text("[")
    assert read_conversion_quality(tmp_path)["requires_review"]


def test_default_content_and_explicit_legacy_mode():
    spec = importlib.util.spec_from_file_location("conversion_entry", Path(__file__).parents[1] / "steps/convert_pdfs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = dict(profile=None, mode=None, docling_only=False, no_auto_grobid=False, no_parallel=False)
    content = module._resolve_profile(SimpleNamespace(**base))
    assert content.profile == "content" and content.docling_only and content.no_auto_grobid
    legacy = module._resolve_profile(SimpleNamespace(**{**base, "mode": "fast"}))
    assert legacy.profile == "bibliographic" and not legacy.docling_only
    requested = module._resolve_profile(SimpleNamespace(**{**base, "profile": "bibliographic"}))
    assert requested.mode == "full-grobid" and not requested.docling_only


def supplement_fixture(tmp_path):
    project = tmp_path / "project"
    folder = project / "papers" / "Clinical_Trial" / "10.1000--a"
    folder.mkdir(parents=True)
    (folder / "body.md").write_text("main text")
    (folder / "provenance.json").write_text('{"extraction_mode":"docling-only"}')
    source = tmp_path / "s1.docx"
    source.write_bytes(b"native supporting table")
    entries = [{"paper_id": "10.1000/a", "source": str(source), "sha256": file_hash(source)}]
    return project, folder, source, entries


def test_supplement_import_is_explicit_idempotent_and_keeps_source(tmp_path):
    project, folder, source, entries = supplement_fixture(tmp_path)
    import_supplements(project, {}, entries)
    assert not (folder / "supplementary").exists()
    import_supplements(project, {}, entries, execute=True)
    assert (folder / "supplementary" / "s1.docx").read_bytes() == source.read_bytes()
    assert import_supplements(project, {}, entries, execute=True)[0]["action"] == "present"
    assert json.loads((folder / "provenance.json").read_text())["supplementary_files"] == 1
    assert json.loads((folder / "provenance.json").read_text())["extraction_mode"] == "docling-only"


def test_import_hash_failure_never_creates_destination(tmp_path):
    project, folder, _, entries = supplement_fixture(tmp_path)
    entries[0]["sha256"] = "wrong"
    with pytest.raises(ValueError, match="hash"):
        import_supplements(project, {}, entries, execute=True)
    assert not (folder / "supplementary").exists()


def test_import_refuses_different_destination_and_path_traversal(tmp_path):
    project, folder, _, entries = supplement_fixture(tmp_path)
    entries[0]["filename"] = "../body.md"
    with pytest.raises(ValueError, match="filename"):
        import_supplements(project, {}, entries, execute=True)
    del entries[0]["filename"]
    (folder / "supplementary").mkdir()
    (folder / "supplementary" / "s1.docx").write_bytes(b"different existing file")
    with pytest.raises(ValueError, match="replace"):
        import_supplements(project, {}, entries, execute=True)


def test_supplement_import_refuses_legacy_only_paper(tmp_path):
    project, folder, source, entries = supplement_fixture(tmp_path)
    legacy = project / "converted" / folder.name
    legacy.parent.mkdir()
    folder.rename(legacy)
    with pytest.raises(ValueError, match="canonical store"):
        import_supplements(project, {}, entries, execute=True)
    assert not (legacy / "supplementary").exists()
