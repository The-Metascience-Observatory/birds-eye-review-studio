#!/usr/bin/env python3
"""Inventory, freeze, run, and score an isolated PDF conversion benchmark."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ber_core import conversion_benchmark as benchmark
from ber_core.storage import atomic_write_json


def save_new(path, value):
    path = Path(path)
    if path.exists():
        raise benchmark.BenchmarkError(f"Refusing to replace evidence: {path}")
    atomic_write_json(path, value)
    print(json.dumps({"output": str(path.resolve())}, ensure_ascii=False))


def adapter_template():
    runtime = benchmark.runtime_probe()
    versions = runtime["packages"]
    spec = importlib.util.find_spec("pdf4llm")
    code = ([benchmark.artifact(p, "converter_code") for p in sorted(Path(spec.origin).parent.rglob("*.py"))]
            if spec and spec.origin else [])
    adapters = [{"id": "current-snapshot", "kind": "snapshot", "python": sys.executable,
                 "packages": {"benchmark_snapshot": "1"}, "configuration": {"copy_existing_artifacts": True},
                 "requires_cuda": False}]
    for mode in ("fast", "accurate"):
        adapters.append({"id": f"docling-{mode}", "kind": "pdf4llm", "python": sys.executable,
                         "packages": {name: versions.get(name, "pending")
                                      for name in ("pdf4llm", "docling", "docling-core", "torch")},
                         "model_artifacts": [], "code_artifacts": code,
                         "environment": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
                         "requires_cuda": True, "timeout_seconds": 600,
                         "configuration": {"conversion_profile": "content", "docling_table_mode": mode,
                                           "docling_device": "cuda", "no_ocr": False,
                                           "crossref_enrich_threshold": 0, "use_marker_fallback": False,
                                           "use_pymupdf_fallback": False, "docling_timeout": 300}})
    adapters.append({"id": "pymupdf4llm", "kind": "pymupdf4llm", "python": sys.executable,
                     "packages": {name: versions.get(name, "pending") for name in ("pymupdf4llm", "PyMuPDF")},
                     "configuration": {"to_markdown": {}}, "requires_cuda": False, "timeout_seconds": 180})
    return {"schema_version": 1, "instructions": "Select adapters explicitly. Add hashes of all used model weights/configs. "
            "External MinerU/Marker wrappers must accept input/output argv and produce body.md/provenance.json; "
            "pin the actual installed versions and model files. No upstream CLI flags are assumed.",
            "adapters": adapters,
            "external_example": {"id": "mineru-standard", "kind": "external", "python": sys.executable,
                                 "packages": {"mineru": "pending"}, "model_artifacts": [], "code_artifacts": [],
                                 "argv": [], "configuration": {"tier": "standard"},
                                 "requires_cuda": True, "timeout_seconds": 600}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inventory", help="Nominate staged RCT candidates, without claiming verification")
    p.add_argument("--project", required=True)
    p.add_argument("--config")
    p.add_argument("--papers-root")
    p.add_argument("--pilot")
    p.add_argument("--seed", default="v1")
    p.add_argument("--sentinel", action="append", default=[], metavar="DOI",
                   help="Known-difficult development report to nominate even when not staged (repeatable)")
    p.add_argument("--output", required=True)
    p = sub.add_parser("split", help="Group source-verified RCT reports by verified trial family")
    p.add_argument("--inventory", required=True)
    p.add_argument("--dev", type=int, default=20)
    p.add_argument("--holdout", type=int, default=40)
    p.add_argument("--output", required=True)
    p = sub.add_parser("templates", help="Write pending annotation templates and adapter/evaluation examples")
    p.add_argument("--inventory", required=True)
    p.add_argument("--output-dir", required=True)
    p = sub.add_parser("preflight", help="Probe the actual subprocess runtime; never install packages")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--output", required=True)
    p = sub.add_parser("evaluation", help="Freeze the real resolved pass-2 prompt and exact model without LLM calls")
    p.add_argument("--config", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--provider", default="codex_cli")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--packaging", choices=("legacy", "table-aware"), default="legacy")
    p.add_argument("--source-policy", choices=("pdf_only", "production"), default="pdf_only")
    p.add_argument("--input-budget", type=int, default=80000)
    p.add_argument("--max-batches", type=int, default=6)
    p.add_argument("--output", required=True)
    p = sub.add_parser("freeze")
    p.add_argument("--cohort", required=True)
    p.add_argument("--adapters", required=True)
    p.add_argument("--adapter", action="append", required=True)
    p.add_argument("--evaluation", required=True)
    p.add_argument("--split", choices=("dev", "holdout"), default="dev")
    p.add_argument("--output", required=True)
    p = sub.add_parser("run")
    p.add_argument("--frozen", required=True)
    p.add_argument("--adapter", action="append")
    p.add_argument("--output-dir", required=True)
    p = sub.add_parser("seal", help="Seal extraction predictions and pre-score flags without reading gold")
    p.add_argument("--frozen", required=True)
    p.add_argument("--predictions", required=True)
    p.add_argument("--run-results", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("extract", help="Stage three paired pass-2 repetitions; --execute starts LLM calls")
    p.add_argument("--frozen", required=True)
    p.add_argument("--run-results", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--timeout-per-report", type=float, default=1800)
    p = sub.add_parser("map", help="Verify explicit semantic mapping and JSON-pointer numerical provenance")
    p.add_argument("--frozen", required=True)
    p.add_argument("--mapping", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("score")
    p.add_argument("--frozen", required=True)
    p.add_argument("--sealed", required=True)
    p.add_argument("--dev-gold-map", help="Optional paper_id:path mapping for development only")
    p.add_argument("--baseline", help="Scored baseline on the same source cohort and extraction config")
    p.add_argument("--output", required=True)
    p = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p.add_argument("job")
    sub.add_parser("_resolve-prompt", help=argparse.SUPPRESS)
    p = sub.add_parser("_supervised-exec", help=argparse.SUPPRESS)
    p.add_argument("argv_json")
    args = parser.parse_args(argv)
    try:
        command = args.command
        if command == "inventory":
            config = None
            if args.config:
                import yaml
                config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
            result = benchmark.inventory(args.project, config=config, papers_root=args.papers_root,
                                         pilot=args.pilot, seed=args.seed, sentinels=args.sentinel)
            save_new(args.output, result)
            print(json.dumps(result["summary"]))
        elif command == "split":
            save_new(args.output, benchmark.split_inventory(benchmark.read_json(args.inventory),
                                                            dev_size=args.dev, holdout_size=args.holdout))
        elif command == "templates":
            data = benchmark.read_json(args.inventory)
            dest = Path(args.output_dir)
            if dest.exists():
                raise benchmark.BenchmarkError("Template destination already exists")
            dest.mkdir(parents=True)
            for paper in data["papers"]:
                if paper.get("split", paper.get("proposed_split")) in {"dev", "holdout"}:
                    atomic_write_json(dest / "annotations" / (benchmark.identifier(paper["paper_id"]) + ".json"),
                                      benchmark.annotation_template(paper))
            atomic_write_json(dest / "adapters.json", adapter_template())
            atomic_write_json(dest / "evaluation.json", {"model": "SET_EXACT_MODEL",
                              "prompt_sha256": "SET_SHA256_OF_FROZEN_PROMPT", "source_policy": "pdf_only",
                              "input_budget": 80000, "packaging": "fixed-current-body",
                              "extraction_options": {"all_outcomes": True, "per_arm_sample_detail": True,
                                                     "subgroup_analyses": True, "provenance_quotes": True}})
            print(json.dumps({"output": str(dest.resolve()), "status": "pending verification and model pins"}))
        elif command == "preflight":
            result = benchmark.runtime_probe(args.python, cuda=True)
            result["interpretation"] = "Visibility applies to this subprocess environment; sandbox isolation can hide an available host GPU."
            save_new(args.output, result)
            print(json.dumps(result))
        elif command == "evaluation":
            save_new(args.output, benchmark.prepare_evaluation(args.config, model=args.model, provider=args.provider,
                     packaging=args.packaging, input_budget=args.input_budget, max_batches=args.max_batches,
                     python=args.python, source_policy=args.source_policy))
        elif command == "freeze":
            adapters = benchmark.read_json(args.adapters)["adapters"]
            requested = set(args.adapter)
            selected = [a for a in adapters if a["id"] in requested]
            if {a["id"] for a in selected} != requested:
                raise benchmark.BenchmarkError("Unknown requested adapter")
            save_new(args.output, benchmark.freeze(benchmark.read_json(args.cohort), selected,
                      benchmark.read_json(args.evaluation), split=args.split))
        elif command == "run":
            result = benchmark.run(benchmark.read_json(args.frozen), args.output_dir, adapter_ids=args.adapter)
            print(json.dumps({"reports": len(result["results"]), "output": str(Path(args.output_dir).resolve())}))
        elif command == "seal":
            save_new(args.output, benchmark.seal_predictions(benchmark.read_json(args.predictions),
                     benchmark.read_json(args.frozen), benchmark.read_json(args.run_results)))
        elif command == "extract":
            result = benchmark.prepare_extraction(benchmark.read_json(args.frozen), benchmark.read_json(args.run_results),
                     args.output_dir, execute=args.execute, repetitions=args.repetitions,
                     timeout_per_report=args.timeout_per_report)
            print(json.dumps({"jobs": len(result["jobs"]), "executed": args.execute,
                              "output": str(Path(args.output_dir).resolve())}))
        elif command == "map":
            save_new(args.output, benchmark.map_predictions(benchmark.read_json(args.mapping), benchmark.read_json(args.frozen)))
        elif command == "score":
            save_new(args.output, benchmark.score(benchmark.read_json(args.sealed), benchmark.read_json(args.frozen),
                     gold_paths=benchmark.read_json(args.dev_gold_map) if args.dev_gold_map else None,
                     baseline=benchmark.read_json(args.baseline) if args.baseline else None))
        elif command == "_worker":
            benchmark.worker(benchmark.read_json(args.job))
        elif command == "_resolve-prompt":
            print(json.dumps(benchmark.resolve_prompt_job(json.load(sys.stdin))))
        elif command == "_supervised-exec":
            return benchmark.supervised_exec(json.loads(args.argv_json))
        return 0
    except (benchmark.BenchmarkError, OSError, KeyError, ValueError) as exc:
        print(f"Benchmark stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
