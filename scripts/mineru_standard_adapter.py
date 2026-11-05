#!/usr/bin/env python3
"""Pinned local MinerU 4.0 Standard benchmark adapter; never mutates paper stores.

Usage: ISOLATED_PYTHON mineru_standard_adapter.py INPUT_PDF OUTPUT_DIRECTORY
       --runtime-root ISOLATED_RUNTIME [--pages 10-12]

The runtime requires mineru==4.0.0, home/config.yaml and models-lock.json.
Use the benchmark manifest to verify the installed package, model and code pins.
The SDK interface was verified from the released mineru-4.0.0 wheel.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import importlib.metadata
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

PIN = "4.0.0"
DEFAULT_RUNTIME = os.environ.get("BER_MINERU_RUNTIME_ROOT")
# Canonical model-lock entries (repo/revision/relative file/hash/size), excluding
# installation-specific local paths. This pins the complete downloaded bundles.
MODEL_MANIFEST_SHA256 = "2db283b9d95f95502c2e232c8fd1f13b3839793930b787949558d7f5f3bd187c"


def write_json(path, value):
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_runtime(runtime):
    """Refuse changed models/configuration before manual rescue or a benchmark."""
    import yaml
    if importlib.metadata.version("mineru") != PIN:
        raise ValueError(f"Requires mineru=={PIN}")
    models = json.loads((runtime / "models-lock.json").read_text(encoding="utf-8"))
    metadata = [{"repo": m["repo"], "revision": m["revision"],
                 "files": [{k: f[k] for k in ("path", "sha256", "size")} for f in m["files"]]}
                for m in models]
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != MODEL_MANIFEST_SHA256:
        raise ValueError("Model manifest differs from the pinned Standard configuration")
    checked = 0
    for model in models:
        for item in model["files"]:
            path = Path(item["local_path"]).resolve()
            if not path.is_relative_to((runtime / "models").resolve()):
                raise ValueError("Model file lies outside the isolated runtime")
            if not path.is_file() or path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
                raise ValueError(f"Model file missing or changed: {path.name}")
            checked += 1
    config_path = runtime / "home" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    expected = {"model": {"base_dir": str(runtime / "models"), "source": "local", "small_backend": "onnx",
                          "vlm": {"engine": "llama-cpp", "server_url": ""}},
                "llm_aided": {"features": {"title_leveling": False, "cross_page_table_cell_merge": False}}}
    if config != expected:
        raise ValueError("Runtime configuration must use the pinned local ONNX/llama.cpp profile without LLM refinement")
    return {"model_manifest_sha256": digest, "model_files_verified": checked,
            "config_sha256": sha256(config_path), "verified": True}


def page_quality(expected_pages, requested_indices, returned_indices, is_full_document):
    missing = sorted(set(requested_indices) - set(returned_indices))
    selected = len(set(requested_indices)) < expected_pages
    warnings = []
    if missing:
        warnings.append("missing_pages:" + ",".join(str(n + 1) for n in missing))
    if selected:
        warnings.append("selected_page_rescue")
    elif is_full_document is not True:
        warnings.append("mineru_full_document_not_confirmed")
    return missing, warnings, bool(missing or selected or is_full_document is not True)


def table_warnings(markdown):
    result = []
    for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", markdown, re.I | re.S):
        cell = html.unescape(re.sub(r"<[^>]*>", "", cell))
        if re.search(r"\d", cell) and cell.count("[") != cell.count("]"):
            result.append("numeric_interval_split_across_cells")
            break
    return result


def render_with_html_tables(result):
    """Keep full table blocks, including captions/footnotes, as original HTML.

    Public DocVortex block rendering locates the exact native representation.
    When a renderer has combined blocks, keep both the original and HTML copy
    with an explicit warning instead of silently replacing unrelated text.
    """
    from mineru.config import config
    from mineru.render import render_html
    from docvortex.render.markdown import render_single_block
    markdown = result.markdown(asset_base_url="native/")
    warnings, html_tables = [], []
    for page in result.pages:
        for block in page.blocks:
            if str(block.type) != "table":
                continue
            rendered = render_single_block(block, delimiters=config.render.latex_delimiters,
                                           asset_base_url="native/")
            subset = result.middle_json.model_copy(update={
                "pages": [page.model_copy(update={"blocks": [block]})]})
            table_html = render_html(subset, standalone=False, asset_base_url="native/")
            html_tables.append(table_html)
            if rendered and markdown.count(rendered) == 1:
                markdown = markdown.replace(rendered, table_html, 1)
            else:
                warnings.append(f"table_html_placement_ambiguous:page_{page.page_idx + 1}")
                markdown += f"\n\n## Original table evidence, page {page.page_idx + 1}\n\n" + table_html
    return markdown, html_tables, warnings


def convert_child(pdf, output, runtime, pages_requested):
    started = time.monotonic()
    if importlib.metadata.version("mineru") != PIN:
        raise RuntimeError(f"Requires mineru=={PIN}")
    from mineru.parser import parse, ParseResult
    from mineru.parser.page_range import parse_page_range
    from mineru.parser.writer import FileBasedDataWriter
    import pypdfium2
    with pypdfium2.PdfDocument(str(pdf)) as source:
        expected_pages = len(source)
    requested_indices = parse_page_range(pages_requested, expected_pages)
    # Exact public 4.0 SDK. Local whole-document processing, without external LLM refinement.
    result = parse(pdf, tier="standard", ocr_mode="txt", image_analysis=True, page_range=pages_requested)
    raw = result.to_dict(skip_defaults=False)
    result.save(FileBasedDataWriter(str(output / "native")))
    native_result = ParseResult.from_dict(json.loads((output / "native" / "middle_json.json").read_text(encoding="utf-8")))
    markdown, table_html, serialization_warnings = render_with_html_tables(native_result)
    (output / "tables.html").write_text("\n\n".join(table_html), encoding="utf-8")
    (output / "body.md").write_text(markdown, encoding="utf-8")
    write_json(output / "document.json", raw)
    pages = sorted(page["page_idx"] for page in raw.get("pages", []))
    missing, page_warnings, partial = page_quality(expected_pages, requested_indices, pages, raw.get("is_full_document"))
    warnings = serialization_warnings + table_warnings("\n".join(table_html)) + page_warnings
    if not markdown.strip():
        warnings.append("empty_body")
    tables = []
    def visit(blocks, page_idx):
        for block in blocks:
            if isinstance(block, dict):
                if block.get("type") == "table":
                    tables.append({"source_page": page_idx + 1, "block": block})
                for value in block.values():
                    if isinstance(value, list):
                        visit(value, page_idx)
    for page in raw.get("pages", []):
        visit(page.get("blocks", []), page["page_idx"])
    write_json(output / "tables.json", {"schema_version": 1, "tables": tables})
    packages = {name: importlib.metadata.version(name) for name in (
        "mineru", "docvortex", "mineru-vl-utils", "mineru-llama-cpp", "onnxruntime", "numpy", "pypdfium2")}
    models = json.loads((runtime / "models-lock.json").read_text(encoding="utf-8"))
    write_json(output / "provenance.json", {
        "schema_version": 2, "source": {"filename": pdf.name, "sha256": sha256(pdf)},
        "extraction_mode": "mineru-standard", "components": {"body": "mineru", "tables": "mineru"},
        "conversion_status": "partial" if partial else ("degraded" if warnings else "complete"),
        "requires_review": bool(warnings), "warnings": warnings,
        "configuration": {"tier": "standard", "ocr_mode": "txt", "page_range": pages_requested,
                          "table_serialization": "native_html_with_captions_footnotes",
                          "image_analysis": True, "small_backend": "onnx", "vlm_engine": "llama-cpp",
                          "external_llm_refinement": False},
        "versions": packages, "expected_pages": expected_pages,
        "requested_pages": [n + 1 for n in requested_indices], "full_document": raw.get("is_full_document"), "page_numbers": [n + 1 for n in pages],
        "missing_pages": [n + 1 for n in missing], "table_count": len(tables),
        "processing_time_s": time.monotonic() - started,
        "models": [{"repo": item["repo"], "revision": item["revision"], "total_bytes": item["total_bytes"]} for item in models],
        "runtime": {"small_models": "onnx-cpu", "vlm": "llama.cpp (see runtime.log for device evidence)"},
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--pages", default="all", help="One-based selected pages for rescue, e.g. 10-12; default all")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.runtime_root is None:
        parser.error("--runtime-root or BER_MINERU_RUNTIME_ROOT is required")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    pdf, output, runtime = args.input.resolve(), args.output.resolve(), args.runtime_root.resolve()
    if args.child:
        convert_child(pdf, output, runtime, args.pages)
        return
    if not pdf.is_file():
        raise SystemExit("Input PDF missing")
    if output == pdf.parent or pdf.parent in output.parents:
        raise SystemExit("Output must be outside the source paper directory")
    if output.exists() and any(p.name not in {"job.json", "worker.log"} for p in output.iterdir()):
        raise SystemExit("Refusing to overwrite candidate artifacts")
    verification_started = time.monotonic()
    runtime_verification = verify_runtime(runtime)
    runtime_verification["verification_time_s"] = time.monotonic() - verification_started
    output.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINERU_")}
    env.update({"MINERU_HOME": str(runtime / "home"), "MINERU_CONFIG": str(runtime / "home" / "config.yaml"),
                "HF_HOME": str(runtime / "huggingface"), "HF_XET_CACHE": str(runtime / "xet-cache"),
                "HF_HUB_OFFLINE": "1", "TMPDIR": str(runtime), "XDG_CACHE_HOME": str(runtime / "cache"),
                "MINERU_MODEL_SOURCE": "local"})
    command = [sys.executable, str(Path(__file__).resolve()), str(pdf), str(output),
               "--runtime-root", str(runtime), "--pages", args.pages, "--child"]
    started = time.monotonic()
    failed = None
    with (output / "runtime.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            code = process.wait(timeout=args.timeout)
            if code:
                failed = f"MinerU worker exited with code {code}; see runtime.log"
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            failed = f"MinerU exceeded {args.timeout}s; entire process group terminated"
    if failed:
        write_json(output / "provenance.json", {"schema_version": 2, "conversion_status": "failed",
            "requires_review": True, "warnings": [failed], "extraction_mode": "mineru-standard",
            "processing_time_s": time.monotonic() - started,
            "source": {"filename": pdf.name, "sha256": sha256(pdf)}})
        print(json.dumps({"status": "failed", "error": failed, "output": str(output)}))
        raise SystemExit(1)
    evidence = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
    evidence["worker_wall_time_s"] = time.monotonic() - started
    evidence["runtime_verification"] = runtime_verification
    log_text = (output / "runtime.log").read_text(encoding="utf-8", errors="replace")
    evidence["runtime"]["gpu_log_lines"] = [line[-500:] for line in log_text.splitlines()
        if any(token in line.lower() for token in ("ggml_vulkan", "vulkan0", "nvidia geforce", "offloaded"))][:20]
    write_json(output / "provenance.json", evidence)
    print(json.dumps({"status": evidence["conversion_status"], "seconds": evidence["worker_wall_time_s"],
                      "pages": len(evidence["page_numbers"]), "tables": evidence["table_count"],
                      "warnings": evidence["warnings"], "output": str(output)}))


if __name__ == "__main__":
    main()
