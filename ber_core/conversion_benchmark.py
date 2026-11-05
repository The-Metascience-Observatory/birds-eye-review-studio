"""Isolated, explicitly verified PDF conversion benchmark.

Inventory is a nomination list, never an eligibility verdict or gold standard.
Conversion workers receive source paths and adapter configuration, not gold.
No function writes to a paper store or invokes an extraction model.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from ber_core.identity import join_key
from ber_core.storage import atomic_write_json

SCHEMA_VERSION = 1
FACT_FIELDS = (
    "population", "subgroup", "arm", "outcome", "timepoint", "n", "denominator",
    "statistic", "value", "uncertainty_type", "uncertainty_value",
    "uncertainty_lower", "uncertainty_upper", "uncertainty_level", "units",
    "adjustment",
)
NUMERIC_FIELDS = {"n", "denominator", "value", "uncertainty_value",
                  "uncertainty_lower", "uncertainty_upper", "uncertainty_level"}
REQUIRED_TEXT = {"population", "arm", "outcome", "timepoint", "statistic", "units",
                 "adjustment", "uncertainty_type"}


class BenchmarkError(ValueError):
    """Invalid, changed, unverified, or contaminated benchmark input."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact(path, role):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size,
            "role": role}


def check_artifact(item):
    path = Path(item["path"])
    if not path.is_file() or sha256(path) != item["sha256"]:
        raise BenchmarkError(f"Source/artifact changed: {path}")


def identifier(paper_id):
    """Stable filename without interpolating a DOI into a path."""
    return hashlib.sha256(paper_id.encode()).hexdigest()[:20]


def inventory(project, *, config=None, papers_root=None, pilot=None, seed="v1", sentinels=()):
    from ber_core import paper_store
    project = Path(project).resolve()
    index = paper_store.build_index(project, root=papers_root, config=config)
    staged = project / "eligible_rct"
    keys = {join_key(p.name) for p in staged.iterdir()} if staged.is_dir() else set()
    keys.discard(None)
    # Known-difficult development reports to nominate even when not staged.
    sentinels = {join_key(s) for s in sentinels}
    if pilot:
        sentinels.update(join_key(p["paper_id"]) for p in read_json(pilot)
                         if p.get("is_rct") is True)
    staged_keys = set(keys)
    keys.update(k for k in sentinels if k)
    papers = []
    for key in sorted(keys):
        directory = index.get(key)
        if directory is None:
            continue
        pdfs = sorted(directory.glob("*.pdf"))
        matching = [p for p in pdfs if join_key(p.stem) == key]
        pdf = matching[0] if len(matching) == 1 else (pdfs[0] if len(pdfs) == 1 else None)
        def source_file(path, role):
            return {**artifact(path, role), "relative_path": path.relative_to(directory).as_posix()}
        sources = [source_file(pdf, "main_pdf")] if pdf else []
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix.lower() in {".xml", ".html", ".htm"}:
                sources.append(source_file(path, "main_xml" if path.suffix.lower() == ".xml" else "main_html"))
        for path in sorted(directory.rglob("*")):
            if path.is_file() and "supplementary" in path.relative_to(directory).parts:
                if path.name in {"references.md", "abstract.md"}:
                    continue
                if path.suffix.lower() in {".pdf", ".docx", ".xlsx", ".csv", ".html", ".htm", ".xml", ".md", ".txt"}:
                    sources.append(source_file(path, "supplement"))
                elif path.name == "provenance.json":
                    sources.append(source_file(path, "supplement_provenance"))
        body = directory / "body.md"
        baseline = [artifact(directory / name, "baseline")
                    for name in ("body.md", "provenance.json", "tables.json", "document.json")
                    if (directory / name).is_file()]
        text = body.read_text(encoding="utf-8", errors="replace") if body.is_file() else ""
        papers.append({"paper_id": key, "paper_dir": str(directory.resolve()),
                       "nomination_basis": ("eligible_rct bucket; unverified" if key in staged_keys
                                            else "previously inspected RCT candidate; unverified"),
                       "sources": sources, "baseline": baseline,
                       "dev_sentinel": key in sentinels,
                       "observations": {"pdf_candidates": len(pdfs), "body_characters": len(text),
                                        "body_over_80000": len(text) > 80000,
                                        "html_tables": text.lower().count("<table"),
                                        "has_xml": bool(list(directory.glob("*.xml")))},
                       "verification": {"is_rct": None, "identity_verified": False,
                                        "family_verified": False, "trial_family": "",
                                        "verified_by": "", "evidence_refs": []},
                       "gold_path": None})
    result = {"schema_version": SCHEMA_VERSION, "project": str(project), "seed": seed,
              "paper_store_root": str(paper_store.papers_root(project, papers_root, config).resolve()),
              "status": "proposal_unverified", "papers": papers}
    proposal = assign_splits(papers, seed=seed, verified_only=False)
    for paper in papers:
        paper["proposed_split"] = proposal.get(paper["paper_id"], "reserve")
    result["summary"] = {"nominated": len(papers),
                         "pdf_available": sum(any(s["role"] == "main_pdf" for s in p["sources"]) for p in papers),
                         "proposed": dict(Counter(p["proposed_split"] for p in papers)),
                         "verified": 0}
    return result


def verified(paper):
    v = paper.get("verification", {})
    return (v.get("is_rct") is True and v.get("identity_verified") is True
            and v.get("family_verified") is True and bool(str(v.get("trial_family", "")).strip())
            and bool(str(v.get("verified_by", "")).strip()) and bool(v.get("evidence_refs"))
            and sum(s.get("role") == "main_pdf" for s in paper.get("sources", [])) == 1)


def assign_splits(papers, *, seed, dev_size=20, holdout_size=40, verified_only=True):
    if dev_size < 1 or holdout_size < 1:
        raise BenchmarkError("Development and holdout targets must both be positive")
    groups = defaultdict(list)
    seen = set()
    for paper in papers:
        if paper["paper_id"] in seen:
            raise BenchmarkError("Duplicate paper ID in cohort")
        seen.add(paper["paper_id"])
        if verified_only and not verified(paper):
            continue
        if not any(s.get("role") == "main_pdf" for s in paper.get("sources", [])):
            continue
        family = paper.get("verification", {}).get("trial_family") or paper["paper_id"]
        groups[family].append(paper)
    total = sum(map(len, groups.values()))
    if total < dev_size + holdout_size:
        dev_size = max(1, math.floor(total * dev_size / (dev_size + holdout_size)))
        holdout_size = max(0, total - dev_size)
    ordered = sorted(groups, key=lambda f: (not any(p.get("dev_sentinel") for p in groups[f]),
                                            digest([seed, f])))
    result, counts = {}, Counter()
    for family in ordered:
        members = groups[family]
        sentinel = any(p.get("dev_sentinel") for p in members)
        split = ("dev" if sentinel or counts["dev"] < dev_size else
                 "holdout" if counts["holdout"] < holdout_size else "reserve")
        for paper in members:
            result[paper["paper_id"]] = split
            counts[split] += 1
    return result


def split_inventory(data, *, dev_size=20, holdout_size=40):
    result = json.loads(json.dumps(data))
    splits = assign_splits(result["papers"], seed=result["seed"],
                           dev_size=dev_size, holdout_size=holdout_size)
    if not splits:
        raise BenchmarkError("No source/identity/family-verified RCT reports; inventory is only a proposal")
    for paper in result["papers"]:
        paper["split"] = splits.get(paper["paper_id"], "unverified")
    result["status"] = "verified_split"
    result["split_counts"] = dict(Counter(splits.values()))
    return result


def annotation_template(paper):
    return {"schema_version": SCHEMA_VERSION, "paper_id": paper["paper_id"],
            "sources": paper["sources"], "status": "pending_independent_source_review",
            "source_coverage_complete": False, "reviewers": [],
            "adjudicated_by": "", "conflicts_resolved": False, "facts": [],
            "instructions": "Two independent PDF/supplement annotations and adjudication required. "
                            "Existing LLM output is not gold. Include missing outcomes as well as easy tables."}


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise BenchmarkError("Boolean is not a numerical value")
    raw = unicodedata.normalize("NFKC", str(value)).strip().replace("−", "-")
    match = re.fullmatch(r"([<>≤≥]=?)?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", raw)
    if not match:
        raise BenchmarkError(f"Use an explicit numerical value and separate units: {value!r}")
    try:
        number = Decimal(match[2])
    except InvalidOperation as exc:
        raise BenchmarkError("Invalid decimal") from exc
    if not number.is_finite():
        raise BenchmarkError("Nonfinite number")
    return (match[1] or "") + ("0" if number == 0 else format(number.normalize(), "f"))


def fact_key(fact):
    missing = set(FACT_FIELDS) - fact.keys()
    if missing:
        raise BenchmarkError(f"Outcome tuple lacks fields: {sorted(missing)}")
    values = []
    for field in FACT_FIELDS:
        value = fact[field]
        if field in NUMERIC_FIELDS:
            value = _number(value)
        elif value is not None:
            value = " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())
        if field in REQUIRED_TEXT and not value:
            raise BenchmarkError(f"Explicit {field} required (use 'none'/'not reported' where appropriate)")
        values.append(value)
    if fact["value"] is None:
        raise BenchmarkError("A fact must contain a reported value")
    return tuple(values)


def evidenced_key(fact):
    evidence = fact.get("evidence", {})
    if not evidence.get("source_sha256") or not str(evidence.get("locator", "")).strip():
        raise BenchmarkError("Every outcome needs a source hash and page/table/cell locator")
    return fact_key(fact) + (evidence["source_sha256"],)


def validate_gold(gold, paper):
    if gold.get("paper_id") != paper["paper_id"] or gold.get("status") != "adjudicated":
        raise BenchmarkError("Gold identity/status is unverified")
    if not gold.get("source_coverage_complete") or not gold.get("conflicts_resolved") or not gold.get("adjudicated_by"):
        raise BenchmarkError("Gold needs complete source review and resolved adjudication")
    expected = {(s["path"], s["sha256"]) for s in paper["sources"]}
    if {(s["path"], s["sha256"]) for s in gold.get("sources", [])} != expected:
        raise BenchmarkError("Gold sources differ from frozen report/supplements")
    reviewers = gold.get("reviewers", [])
    ids = {r.get("id") for r in reviewers if r.get("id")}
    if len(ids) < 2 or len(ids) != len(reviewers):
        raise BenchmarkError("Two distinct independent reviewers required")
    for reviewer in reviewers:
        if reviewer.get("independent") is not True or reviewer.get("source_reviewed") is not True:
            raise BenchmarkError("Reviewer must attest independent source review")
        annotation = reviewer.get("annotation")
        if not isinstance(annotation, dict):
            raise BenchmarkError("Each reviewer must retain a hashed independent annotation file")
        check_artifact(annotation)
        review = read_json(annotation["path"])
        if (review.get("paper_id") != paper["paper_id"] or review.get("reviewed_by") != reviewer["id"]
                or review.get("status") != "independent_review_complete"
                or review.get("source_coverage_complete") is not True
                or {(s["path"], s["sha256"]) for s in review.get("sources", [])} != expected
                or not review.get("facts")):
            raise BenchmarkError("Independent annotation must identify its reviewer, report, reviewed sources and facts")
        for fact in review["facts"]:
            evidenced_key(fact)
    if len({r["annotation"]["path"] for r in reviewers}) != len(reviewers):
        raise BenchmarkError("Independent annotations must be separate files")
    facts = gold.get("facts", [])
    if not facts or not any(f.get("primary") is True for f in facts):
        raise BenchmarkError("Gold needs explicitly identified primary numerical facts")
    keys = set()
    hashes = {s["sha256"] for s in paper["sources"]}
    for fact in facts:
        key = evidenced_key(fact)
        if key in keys:
            raise BenchmarkError("Duplicate gold outcome tuple")
        keys.add(key)
        evidence = fact.get("evidence", {})
        if evidence.get("source_sha256") not in hashes or not evidence.get("locator"):
            raise BenchmarkError("Every gold fact needs a source hash and page/table/cell locator")
        if not isinstance(fact.get("primary"), bool):
            raise BenchmarkError("Every gold fact needs explicit primary true/false")


def supervise(argv, *, cwd, timeout, log_path, env=None):
    """Supervise the Linux process tree, including detached/orphan GPU workers."""
    if timeout <= 0 or not math.isfinite(timeout):
        raise BenchmarkError("Timeout must be finite and positive")
    started = time.monotonic()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = argv
    if sys.platform == "linux":
        script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_conversion.py"
        command = [sys.executable, str(script), "_supervised-exec", json.dumps(argv)]
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                env=env, start_new_session=(os.name == "posix"))
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if sys.platform == "linux":
                terminate_linux_tree(proc.pid, include_root=True)
            elif os.name == "posix":  # pragma: no cover - Linux is the benchmark target
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover
                proc.kill()
            code = proc.wait()
    return {"returncode": code, "timed_out": timed_out,
            "wall_seconds": time.monotonic() - started, "log": str(log_path)}


def _linux_processes():
    processes = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            processes[int(path.name)] = (int(fields[1]), fields[19])  # PPID, process birth time
        except (OSError, IndexError, ValueError):
            continue
    return processes


def _signal_same_process(pid, birth, sig):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        if fields[19] == birth:  # Never signal a recycled PID.
            os.kill(pid, sig)
    except (ProcessLookupError, FileNotFoundError):
        pass


def terminate_linux_tree(root_pid, *, include_root):
    """Stop spawning first, then kill all descendants regardless of session ID.

    The supervised root is a child subreaper, so orphaned descendants are
    reparented to that root instead of escaping to init before this scan.
    """
    known = {}
    initial = _linux_processes()
    if root_pid not in initial:
        return
    root_birth = initial[root_pid][1]
    if include_root:
        _signal_same_process(root_pid, root_birth, signal.SIGSTOP)
    while True:
        current = _linux_processes()
        descendants = {root_pid}
        while True:
            expanded = descendants | {pid for pid, (parent, _) in current.items() if parent in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        fresh = {pid: current[pid][1] for pid in descendants - {root_pid}
                 if pid in current and known.get(pid) != current[pid][1]}
        for pid, birth in fresh.items():
            _signal_same_process(pid, birth, signal.SIGSTOP)
        known.update(fresh)
        if not fresh:
            break
    for pid, birth in known.items():
        _signal_same_process(pid, birth, signal.SIGKILL)
    if include_root:
        _signal_same_process(root_pid, root_birth, signal.SIGKILL)


def supervised_exec(argv):
    """Private Linux launcher: keep detached grandchildren in our process tree."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise BenchmarkError(f"Cannot establish child-subreaper supervision: errno {ctypes.get_errno()}")
    try:
        result = subprocess.Popen(argv).wait()
    finally:
        terminate_linux_tree(os.getpid(), include_root=False)
        # Reap adopted exited children; do not leave zombies in long commands.
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
    return result if result >= 0 else 128 - result


def runtime_probe(python=sys.executable, *, cuda=False, environment=None):
    code = """import importlib.metadata as m, json, sys
result = {'python': sys.version, 'executable': sys.executable, 'packages': {}}
for name in ['pdf4llm','docling','docling-core','torch','pymupdf4llm','PyMuPDF','marker-pdf','mineru']:
 try: result['packages'][name] = m.version(name)
 except m.PackageNotFoundError: pass
if sys.argv[1] == 'cuda':
 try:
  import torch
  result['cuda_available'] = torch.cuda.is_available()
  result['cuda_runtime'] = torch.version.cuda
  result['devices'] = [{'name':torch.cuda.get_device_name(i),'memory_bytes':torch.cuda.get_device_properties(i).total_memory} for i in range(torch.cuda.device_count())]
 except Exception as e: result['cuda_error'] = str(e)
print(json.dumps(result))
"""
    try:
        output = subprocess.run([python, "-c", code, "cuda" if cuda else "cpu"],
                                capture_output=True, text=True, timeout=60, check=True,
                                env={**os.environ, **(environment or {})})
        return json.loads(output.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise BenchmarkError(f"Runtime preflight failed for {python}: {exc}") from exc


def validate_adapter(adapter, *, probe=True):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", adapter.get("id", "")):
        raise BenchmarkError("Adapter ID must be a safe lowercase name")
    kind = adapter.get("kind")
    if kind not in {"snapshot", "pdf4llm", "pymupdf4llm", "external"}:
        raise BenchmarkError("Unknown adapter kind")
    if not adapter.get("configuration") or not adapter.get("packages"):
        raise BenchmarkError("Adapter must pin packages and an explicit configuration")
    environment = adapter.get("environment", {})
    if (not isinstance(environment, dict) or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str) or "\x00" in value for key, value in environment.items())):
        raise BenchmarkError("Adapter environment must be a string-to-string map of valid environment variables")
    if any(not isinstance(v, str) or not v.strip() or v in {"latest", "pending"}
           for v in adapter["packages"].values()):
        raise BenchmarkError("Exact installed package versions required")
    if kind in {"pdf4llm", "external"} and not adapter.get("model_artifacts"):
        raise BenchmarkError("Neural adapters need hashed model artifacts (no moving model identifiers)")
    if kind in {"pdf4llm", "external"} and not adapter.get("code_artifacts"):
        raise BenchmarkError("Pin editable converter/wrapper source files as code_artifacts")
    for item in adapter.get("model_artifacts", []) + adapter.get("code_artifacts", []):
        check_artifact(item)
    if kind == "external":
        argv = adapter.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
            raise BenchmarkError("External adapter requires an explicit argv array")
        if not any("{input}" in arg for arg in argv) or not any("{output}" in arg for arg in argv):
            raise BenchmarkError("External argv must use {input} and {output} placeholders")
    if not probe:
        return {}
    runtime = runtime_probe(adapter.get("python", sys.executable), cuda=bool(adapter.get("requires_cuda")), environment=environment)
    for name, version in adapter["packages"].items():
        if name == "benchmark_snapshot":
            continue
        if runtime["packages"].get(name) != version:
            # Probe arbitrary user-pinned distribution names in the selected interpreter.
            cmd = [adapter.get("python", sys.executable), "-c",
                   "import importlib.metadata,sys;print(importlib.metadata.version(sys.argv[1]))", name]
            got = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env={**os.environ, **environment})
            if got.returncode or got.stdout.strip() != version:
                raise BenchmarkError(f"Package pin mismatch: {name} expected {version}")
    if adapter.get("requires_cuda") and not runtime.get("cuda_available"):
        raise BenchmarkError("CUDA unavailable in this subprocess; GPU benchmark is blocked, not silently run on CPU")
    return runtime


def freeze(cohort, adapters, evaluation, *, split="dev"):
    if cohort.get("status") != "verified_split" or split not in {"dev", "holdout"}:
        raise BenchmarkError("Freeze requires a verified split")
    if not evaluation or any(key not in evaluation for key in
                             ("model", "prompt_sha256", "extraction_options", "source_policy", "input_budget", "packaging")):
        raise BenchmarkError("Freeze explicit extraction model/prompt/options/source policy/budget/packaging")
    if evaluation["source_policy"] not in {"pdf_only", "production"}:
        raise BenchmarkError("Choose explicit pdf_only or production source policy")
    experiment = "production_workflow" if evaluation["source_policy"] == "production" else "pdf_converter"
    if evaluation.get("experiment", experiment) != experiment:
        raise BenchmarkError("Experiment type contradicts its source policy")
    if (not re.fullmatch(r"[0-9a-f]{64}", str(evaluation["prompt_sha256"]))
            or not str(evaluation["model"]).strip() or str(evaluation["model"]).startswith("SET_")
            or not isinstance(evaluation["extraction_options"], dict)
            or not isinstance(evaluation["input_budget"], int) or evaluation["input_budget"] <= 0):
        raise BenchmarkError("Resolve evaluation template placeholders and freeze a positive input budget")
    selected = [p for p in cohort["papers"] if p.get("split") == split]
    if not selected:
        raise BenchmarkError(f"No verified {split} reports")
    if len({p["paper_id"] for p in cohort["papers"]}) != len(cohort["papers"]):
        raise BenchmarkError("Duplicate report IDs in frozen cohort")
    families = defaultdict(set)
    for p in cohort["papers"]:
        if p.get("split") in {"dev", "holdout"}:
            if not verified(p):
                raise BenchmarkError("Selected report has unverified identity/RCT/family")
            families[p["verification"]["trial_family"]].add(p["split"])
            if p.get("dev_sentinel") and p["split"] != "dev":
                raise BenchmarkError("Previously inspected pilot/sentinel cannot enter holdout")
    if any(len(splits) > 1 for splits in families.values()):
        raise BenchmarkError("Trial family leaks across development and holdout")
    gold = {}
    for paper in selected:
        for source in paper["sources"] + paper.get("baseline", []):
            check_artifact(source)
        if evaluation["source_policy"] == "production":
            production_sources(paper)
        if paper.get("gold_path"):
            path = Path(paper["gold_path"]).resolve()
            validate_gold(read_json(path), paper)
            gold[paper["paper_id"]] = artifact(path, "adjudicated_gold")
        elif split == "holdout":
            raise BenchmarkError("Held-out run requires two independent reviewers and adjudicated gold before freezing")
    if not adapters or len({a["id"] for a in adapters}) != len(adapters):
        raise BenchmarkError("Unique, nonempty adapter list required")
    runtimes = {a["id"]: validate_adapter(a) for a in adapters}
    payload = {"schema_version": SCHEMA_VERSION, "split": split, "seed": cohort["seed"],
               "experiment": experiment, "source_policy": evaluation["source_policy"],
               "paper_store_root": cohort.get("paper_store_root"),
               "papers": selected, "adapters": adapters, "evaluation": evaluation,
               "gold": gold, "runtimes": runtimes,
               "runner_code": artifact(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_conversion.py", "benchmark_runner"),
               "code": artifact(Path(__file__), "benchmark_code")}
    return {"payload": payload, "sha256": digest(payload)}


def validate_frozen(frozen):
    payload = frozen["payload"]
    if digest(payload) != frozen.get("sha256"):
        raise BenchmarkError("Frozen manifest changed")
    check_artifact(payload["code"])
    if payload.get("runner_code"):
        check_artifact(payload["runner_code"])
    for item in payload["evaluation"].get("code_artifacts", []):
        check_artifact(item)
    for paper in payload["papers"]:
        for source in paper["sources"] + paper.get("baseline", []):
            check_artifact(source)
    return payload


def resolve_prompt_job(job):
    """Resolve the real pass-2 prompt in a child with dotenv disabled."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("benchmark_pass2", root / "steps" / "extract_pass2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_prompt_template = lambda *args, **kwargs: job["raw_prompt"]
    flags = module.resolve_pass2_field_flags(job["config"])
    system, user = module._load_prompt_parts("rct", job["config"], field_flags=flags)
    return {"system": system, "user_template": user, "field_flags": flags,
            "format_note": module.format_note_for("md"), "resolved_model": module.resolve_model(job["model"])}


def _resolve_prompt(config, raw_prompt, model, provider, python):
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_conversion.py"
    env = {**os.environ, "BER_MANAGED_RUN": "1", "BER_LLM_PROVIDER": provider}
    result = subprocess.run([python, str(script), "_resolve-prompt"],
                            input=json.dumps({"config": config, "raw_prompt": raw_prompt, "model": model}),
                            capture_output=True, text=True, timeout=60, env=env, check=True)
    return json.loads(result.stdout)


def prepare_evaluation(config_path, *, model, provider="codex_cli", packaging="legacy",
                       input_budget=80000, max_batches=6, python=sys.executable, source_policy="pdf_only"):
    import yaml
    from ber_core.identity import slugify
    from ber_core.defaults import PASS2_FIELD_DEFAULTS
    root = Path(__file__).resolve().parents[1]
    if input_budget < 1 or not 1 <= max_batches <= 20 or packaging not in {"legacy", "table-aware"}:
        raise BenchmarkError("Use a positive input budget, 1–20 batches, and legacy/table-aware packaging")
    if source_policy not in {"pdf_only", "production"}:
        raise BenchmarkError("Choose pdf_only or production source policy")
    config_path = Path(config_path).resolve()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    cfg["dirs"] = {}
    cfg.pop("extra_steps", None)
    cfg["models"] = {**(cfg.get("models") or {}), "extraction_pass2": model}
    cfg["extraction_pass2_fields"] = {key: True for key in PASS2_FIELD_DEFAULTS}
    cfg["fulltext"] = {**(cfg.get("fulltext") or {}), "xml_first": source_policy == "production", "retry_on_empty": False,
                       "supplementary": source_policy == "production", "extraction_max_batches": max_batches}
    slug = slugify((cfg.get("topic") or {}).get("name", "default"))
    roots = []
    for candidate in (cfg.pop("prompts_dir", None), os.environ.get("BER_PROMPTS_DIR")):
        if candidate:
            path = Path(candidate).expanduser()
            roots.append(path if path.is_absolute() else config_path.parent / path)
    roots.append(root / "prompts")
    candidates = [path for base in roots for path in (base / slug / "extract_pass2_rct.txt", base / "extract_pass2_rct.txt")]
    selected = next((p for p in candidates if p.is_file()), None)
    if selected is None:
        raise BenchmarkError("No RCT extraction prompt")
    raw = selected.read_text(encoding="utf-8")
    resolved = _resolve_prompt(cfg, raw, model, provider, python)
    if resolved["resolved_model"] != model:
        raise BenchmarkError(f"Use exact resolved model {resolved['resolved_model']!r}, not an alias")
    code_paths = [root / p for p in ("steps/extract_pass2.py", "ber_core/claude_cli.py", "ber_core/utils.py",
                                    "ber_core/constants.py", "ber_core/defaults.py", "ber_core/codex_backend.py",
                                    "ber_core/extraction_inputs.py", "ber_core/scientific.py",
                                    "ber_core/meta_analysis.py")]
    return {"model": model, "provider": provider, "python": python,
            "prompt_sha256": digest(resolved), "resolved_prompt": resolved, "raw_prompt": raw,
            "extraction_options": cfg["extraction_pass2_fields"], "source_policy": source_policy,
            "experiment": "production_workflow" if source_policy == "production" else "pdf_converter",
            "input_budget": input_budget, "packaging": packaging, "max_batches": max_batches,
            "pass2_config": cfg, "base_config": artifact(config_path, "review_config"),
            "code_artifacts": [artifact(path, "extraction_code") for path in code_paths if path.is_file()]}


def production_sources(paper):
    """Validated, collision-free staging names for frozen native evidence."""
    from ber_core import paper_store
    root = Path(paper["paper_dir"]).resolve()
    outputs, seen, xml_count = [], set(), 0
    for item in paper["sources"]:
        role = item["role"]
        if role not in {"main_xml", "main_html", "supplement", "supplement_provenance"}:
            continue
        source = Path(item["path"]).resolve()
        if not source.is_relative_to(root):
            raise BenchmarkError("Production native inputs must be within the verified canonical paper directory")
        supplied = item.get("relative_path", source.relative_to(root).as_posix())
        relative = PurePosixPath(supplied)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in supplied or "\x00" in supplied
                or supplied != relative.as_posix() or not relative.parts):
            raise BenchmarkError("Unsafe native source relative path")
        if (root / Path(*relative.parts)).resolve() != source:
            raise BenchmarkError("Native source relative path does not match its frozen file")
        if role == "main_xml":
            if len(relative.parts) != 1:
                raise BenchmarkError("Main XML must be in the paper's root directory")
            xml_count += 1
            if xml_count > 1:
                raise BenchmarkError("Multiple main XML files require explicit source verification/selection")
            destination = PurePosixPath(paper_store.stem_for(paper["paper_id"]) + ".xml")
        elif role == "main_html":
            if len(relative.parts) != 1:
                raise BenchmarkError("Main HTML must be in the paper's root directory")
            destination = relative
        else:
            if relative.parts[0] != "supplementary" or len(relative.parts) < 2:
                raise BenchmarkError("Supplement must retain its canonical supplementary/ path")
            destination = relative
        if destination.as_posix() in seen or destination.as_posix() in {"body.md", "provenance.json"}:
            raise BenchmarkError("Native source staging filename collision")
        seen.add(destination.as_posix())
        outputs.append((destination, item))
    return outputs


def prepare_extraction(frozen, run_results, destination, *, execute=False, repetitions=3,
                       timeout_per_report=1800):
    """Stage paired pass-2 runs; only --execute starts the real LLM workflow."""
    import yaml
    from ber_core import paper_store
    payload = validate_frozen(frozen)
    evaluation = payload["evaluation"]
    production = evaluation["source_policy"] == "production"
    if run_results.get("frozen_sha256") != frozen["sha256"]:
        raise BenchmarkError("Conversion results belong to a different experiment")
    if not all(k in evaluation for k in ("pass2_config", "raw_prompt", "provider", "resolved_prompt")):
        raise BenchmarkError("Use the evaluation command to freeze the actual resolved pass-2 prompt")
    if repetitions < 1 or repetitions > 10:
        raise BenchmarkError("Use 1–10 repetitions; default is three")
    if evaluation["packaging"] not in {"legacy", "table-aware"}:
        raise BenchmarkError("Runnable extraction packaging must be legacy or table-aware")
    resolved = _resolve_prompt(evaluation["pass2_config"], evaluation["raw_prompt"], evaluation["model"],
                               evaluation["provider"], evaluation.get("python", sys.executable))
    if digest(resolved) != evaluation["prompt_sha256"]:
        raise BenchmarkError("Resolved extraction prompt/options/model changed")
    dest = Path(destination).resolve()
    if payload.get("paper_store_root"):
        store = Path(payload["paper_store_root"]).resolve()
        if dest == store or store in dest.parents:
            raise BenchmarkError("Extraction benchmark must be outside the paper store")
    if dest.exists():
        raise BenchmarkError("Extraction destination exists; use a new isolated run")
    selected = {p["paper_id"]: p for p in payload["papers"]}
    records = defaultdict(dict)
    allowed_adapters = {a["id"] for a in payload["adapters"]}
    for row in run_results["results"]:
        if row["adapter"] not in allowed_adapters or row["paper_id"] not in selected or row["paper_id"] in records[row["adapter"]]:
            raise BenchmarkError("Unknown or duplicate conversion result")
        for item in row["artifacts"]:
            check_artifact(item)
        records[row["adapter"]][row["paper_id"]] = row
    if not records or any(set(rows) != set(selected) for rows in records.values()):
        raise BenchmarkError("Conversion results must account for every frozen report")
    dest.mkdir(parents=True)
    script = Path(__file__).resolve().parents[1] / "steps" / "extract_pass2.py"
    plan = {"frozen_sha256": frozen["sha256"], "run_sha256": digest(run_results),
            "experiment": payload.get("experiment"), "source_policy": evaluation["source_policy"],
            "execute": execute, "repetitions": repetitions, "jobs": []}
    for adapter_id, rows in sorted(records.items()):
        for repetition in range(1, repetitions + 1):
            work = dest / adapter_id / f"rep-{repetition:02d}"
            inputs = work / "input"
            prompts = work / "prompts"
            inputs.mkdir(parents=True)
            prompts.mkdir()
            (prompts / "extract_pass2_rct.txt").write_text(evaluation["raw_prompt"], encoding="utf-8")
            cfg_path = work / "config.yaml"
            cfg_path.write_text(yaml.safe_dump(evaluation["pass2_config"], sort_keys=False), encoding="utf-8")
            staged, skipped = [], []
            for paper_id, row in rows.items():
                body = next((a for a in row["artifacts"] if Path(a["path"]).name == "body.md"), None)
                native = production_sources(selected[paper_id]) if production else []
                has_xml = any(item["role"] == "main_xml" for _, item in native)
                usable_body = row["usable_output"] and body is not None
                if not usable_body and not has_xml:
                    skipped.append(paper_id)
                    continue
                folder = inputs / paper_store.stem_for(paper_id)
                folder.mkdir()
                for item in row["artifacts"]:
                    if usable_body and Path(item["path"]).name in {"body.md", "provenance.json"}:
                        shutil.copyfile(item["path"], folder / Path(item["path"]).name)
                native_artifacts, unsupported = [], []
                for relative, item in native:
                    check_artifact(item)
                    target = folder / Path(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item["path"], target)
                    native_artifacts.append({**artifact(target, item["role"]), "original_path": item["path"]})
                    if item["role"] == "main_html":
                        unsupported.append({"source": item["path"], "reason": "native HTML pinned/staged but not selected by read_best_text; preconvert separately"})
                staged.append({"paper_id": paper_id, "body": artifact(folder / "body.md", "staged_input") if usable_body else None,
                               "characters": len((folder / "body.md").read_text(encoding="utf-8")) if usable_body else 0,
                               "native_sources": native_artifacts, "limitations": unsupported})
            raw_output = work / "raw-pass2.jsonl"
            argv = [evaluation.get("python", sys.executable), str(script), "--config", str(cfg_path),
                    "--design-type", "rct", "--input", str(inputs), "--output", str(raw_output),
                    "--model", evaluation["model"], "--workers", "1", "--xml-first" if production else "--no-xml-first", "--no-retry-on-empty",
                    "--input-packaging", evaluation["packaging"], "--input-budget-chars", str(evaluation["input_budget"]),
                    "--max-input-batches", str(evaluation.get("max_batches", 6))]
            env_overrides = {"BER_PROJECTS_DIR": str(work / "projects"), "BER_PROMPTS_DIR": str(prompts),
                             "BER_MANAGED_RUN": "1", "BER_LLM_PROVIDER": evaluation["provider"]}
            job = {"adapter": adapter_id, "repetition": repetition, "argv": argv,
                   "environment_overrides": env_overrides, "staged": staged, "skipped": skipped,
                   "input_budget_characters": evaluation["input_budget"], "raw_output": str(raw_output),
                   "status": "prepared_no_llm_calls"}
            if execute and staged:
                job.update(supervise(argv, cwd=work, timeout=timeout_per_report * len(staged),
                                     log_path=work / "extract.log", env={**os.environ, **env_overrides}))
                job["status"] = "completed" if job["returncode"] == 0 and not job["timed_out"] else "failed"
                if raw_output.is_file():
                    job["raw_artifact"] = artifact(raw_output, "raw_pass2")
                    raw_records = _read_raw_records(raw_output)
                    job["usage"] = {field: sum(r.get(field, 0) or 0 for r in raw_records.values())
                                    for field in ("_cost", "_input_tokens", "_output_tokens")}
                    scientific_path = work / "scientific-comparisons.json"
                    atomic_write_json(scientific_path, scientific_summary(raw_records))
                    job["scientific_comparisons"] = artifact(scientific_path, "derived_report_not_gold")
                    mapping = {"adapter": adapter_id, "repetition": repetition,
                               "raw_artifact": job["raw_artifact"], "papers": []}
                    for paper_id in selected:
                        raw_record = raw_records.get(paper_id)
                        mapping["papers"].append({"paper_id": paper_id, "raw_record_sha256": digest(raw_record) if raw_record else None,
                            "review_required": True, "flags": ["mapping_pending" if raw_record else "no_extraction_record"],
                            "mapping_verified_by": "", "facts": [],
                            "numerical_candidates": list(_numeric_pointers(raw_record.get("outcomes", []), "/outcomes")) if raw_record else []})
                    atomic_write_json(work / "mapping-template.json", mapping)
            plan["jobs"].append(job)
            atomic_write_json(dest / "extraction-plan.json", plan)
    return plan


def _read_raw_records(path):
    result = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = join_key(row.get("paper_id"))
        if not key or key in result:
            raise BenchmarkError("Missing/duplicate raw extraction paper ID")
        result[key] = row
    return result


def scientific_summary(records):
    """Report existing BER selection/variance calculations; never reference truth."""
    from ber_core.scientific import RATIOS, number, selected_comparisons, validate_record
    reports = []
    identity_fields = ("study_id", "outcome_concept", "unit", "population", "time_window",
                       "intervention", "comparator", "analysis_population", "basis", "effect_measure")
    for paper_id, record in records.items():
        if not isinstance(record.get("study_design"), dict) or not isinstance(record.get("outcomes"), list):
            reports.append({"paper_id": paper_id, "status": "schema_not_compatible", "comparisons": []})
            continue
        try:
            validation = validate_record(record)
            comparisons = selected_comparisons(record)
            rows = []
            for comparison in comparisons:
                identity = {key: comparison.get(key) for key in identity_fields}
                se = comparison.get("standard_error")
                rows.append({"identity": identity, "identity_sha256": digest(identity),
                             "estimate": comparison.get("effect"), "analysis_estimate": comparison.get("_y"),
                             "standard_error": se, "variance": se * se if number(se) and se > 0 else None,
                             "variance_scale": "log" if comparison.get("effect_measure") in RATIOS else "natural",
                             "poolable": comparison.get("poolable", False), "is_primary": comparison.get("is_primary"),
                             "withheld_reasons": comparison.get("issues", []),
                             "source_path": comparison.get("source_path"), "source_location": comparison.get("source_location"),
                             "source_file": comparison.get("source_file"), "rules_version": comparison.get("rules_version")})
            reports.append({"paper_id": paper_id, "status": "derived_not_source_verified",
                            "raw_status": record.get("_status"), "selected_text_source": record.get("_text_source"),
                            "source_file": record.get("_source_file"),
                            "input_requires_review": (record.get("_extraction_inputs") or {}).get("requires_review"),
                            "validation": validation, "comparisons": rows})
        except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
            reports.append({"paper_id": paper_id, "status": "derivation_failed", "reason": str(exc), "comparisons": []})
    return {"purpose": "Paired downstream comparison diagnostics; derived outputs are not gold or source verification.",
            "reports": reports}


def _numeric_pointers(value, pointer):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _numeric_pointers(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _numeric_pointers(child, pointer + "/" + str(index))
    elif value is not None and not isinstance(value, bool):
        try:
            _number(value)
        except BenchmarkError:
            return
        yield {"json_pointer": pointer, "raw_value": value}


def _at_pointer(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise BenchmarkError("Raw provenance requires an absolute JSON pointer")
    try:
        for component in pointer[1:].split("/"):
            key = component.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise BenchmarkError(f"Invalid raw-record JSON pointer: {pointer}") from exc


def map_predictions(mapping, frozen):
    """Explicit semantic mapping, with every numerical field traceable to raw output."""
    payload = validate_frozen(frozen)
    check_artifact(mapping["raw_artifact"])
    raw_records = _read_raw_records(mapping["raw_artifact"]["path"])
    expected = {p["paper_id"] for p in payload["papers"]}
    rows = mapping.get("papers", [])
    if len(rows) != len(expected) or {p["paper_id"] for p in rows} != expected:
        raise BenchmarkError("Mapping must account for every report")
    for row in rows:
        record = raw_records.get(row["paper_id"])
        if row.get("raw_record_sha256") != (digest(record) if record else None):
            raise BenchmarkError("Mapping raw record hash changed")
        if row.get("review_required") is False and (not row.get("mapping_verified_by") or not record or record.get("_status") != "ok"):
            raise BenchmarkError("Automatically accepted mappings need verified semantics and a successful raw record")
        for fact in row.get("facts", []):
            evidenced_key(fact)
            for field in NUMERIC_FIELDS:
                if fact[field] is not None:
                    pointer = fact.get("raw_fields", {}).get(field)
                    if _number(_at_pointer(record, pointer)) != _number(fact[field]):
                        raise BenchmarkError(f"Mapped {field} differs from its raw extraction value")
    return {"adapter": mapping["adapter"], "repetition": mapping.get("repetition"),
            "raw_artifact": mapping["raw_artifact"], "papers": rows}


def worker(job):
    """Entry point for the isolated built-in adapters; never sees gold."""
    output = Path(job["output"])
    output.mkdir(parents=True, exist_ok=True)
    adapter = job["adapter"]
    config = adapter["configuration"]
    if adapter["kind"] == "pdf4llm":
        from pdf4llm.config import Config
        from pdf4llm.pipeline import convert_single
        convert_single(Path(job["input"]), output, Config(**config), output_subdir_name="")
    elif adapter["kind"] == "pymupdf4llm":
        import pymupdf4llm
        text = pymupdf4llm.to_markdown(job["input"], **config.get("to_markdown", {}))
        (output / "body.md").write_text(text, encoding="utf-8")
        atomic_write_json(output / "provenance.json", {"conversion_status": "complete",
                          "extraction_mode": "pymupdf4llm", "configuration": config})
    else:
        raise BenchmarkError("Invalid built-in worker")


def run(frozen, destination, *, adapter_ids=None):
    payload = validate_frozen(frozen)
    destination = Path(destination).resolve()
    if payload.get("paper_store_root"):
        store = Path(payload["paper_store_root"]).resolve()
        if destination == store or store in destination.parents:
            raise BenchmarkError("Benchmark output must be outside the paper store")
    for paper in payload["papers"]:
        source_dir = Path(paper["paper_dir"]).resolve()
        if destination == source_dir or source_dir in destination.parents:
            raise BenchmarkError("Benchmark output must be outside the paper store")
    if destination.exists():
        raise BenchmarkError("Run destination already exists; create a new isolated run")
    adapters = [a for a in payload["adapters"] if adapter_ids is None or a["id"] in adapter_ids]
    if not adapters or (adapter_ids is not None and set(adapter_ids) - {a["id"] for a in adapters}):
        raise BenchmarkError("Unknown/empty adapter selection")
    runtime = {a["id"]: validate_adapter(a) for a in adapters}
    destination.mkdir(parents=True)
    atomic_write_json(destination / "frozen.json", frozen)
    results = []
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_conversion.py"
    for adapter in adapters:
        for paper in payload["papers"]:
            for source in paper["sources"] + paper.get("baseline", []):
                check_artifact(source)
            out = destination / adapter["id"] / identifier(paper["paper_id"])
            out.mkdir(parents=True)
            record = {"adapter": adapter["id"], "paper_id": paper["paper_id"],
                      "output": str(out), "measurement": "cold isolated document process"}
            if adapter["kind"] == "snapshot":
                started = time.monotonic()
                for item in paper.get("baseline", []):
                    shutil.copyfile(item["path"], out / Path(item["path"]).name)
                record.update(returncode=0, timed_out=False, wall_seconds=time.monotonic() - started,
                              measurement="artifact copy only; not conversion latency")
            else:
                pdf = next(s["path"] for s in paper["sources"] if s["role"] == "main_pdf")
                job = {"input": pdf, "output": str(out), "adapter": adapter}
                job_path = out / "job.json"
                atomic_write_json(job_path, job)
                if adapter["kind"] == "external":
                    argv = [arg.replace("{input}", pdf).replace("{output}", str(out))
                            for arg in adapter["argv"]]
                else:
                    argv = [adapter.get("python", sys.executable), str(script), "_worker", str(job_path)]
                record.update(supervise(argv, cwd=out, timeout=float(adapter.get("timeout_seconds", 600)),
                                        log_path=out / "worker.log", env={**os.environ, **adapter.get("environment", {})}))
            body = out / "body.md"
            provenance = out / "provenance.json"
            try:
                provenance_data = read_json(provenance) if provenance.is_file() else {}
                status = provenance_data.get("conversion_status", "historical_unknown" if adapter["kind"] == "snapshot" else "unknown")
            except (ValueError, OSError):
                status = "invalid_provenance"
                provenance_data = {}
            record["conversion_status"] = status
            record["usable_output"] = bool(not record["timed_out"] and record["returncode"] == 0
                                            and body.is_file() and body.stat().st_size > 0)
            # Historical snapshots reproduce prior acceptance; unknown status is
            # visible, not retroactively converted into a perfect abstaining baseline.
            explicit_warnings = {key: provenance_data[key] for key in
                                 ("requires_review", "review_required", "warnings", "errors", "quality_warnings", "table_warnings", "conversion_degraded")
                                 if provenance_data.get(key)}
            record["provenance_warnings"] = explicit_warnings
            record["requires_review"] = (not record["usable_output"] or status not in {"complete", "historical_unknown"}
                                         or bool(explicit_warnings))
            record["artifacts"] = [artifact(path, "conversion_output") for path in sorted(out.iterdir())
                                   if path.name in {"body.md", "provenance.json", "tables.json", "document.json"}]
            results.append(record)
            atomic_write_json(destination / "results.json", {"frozen_sha256": frozen["sha256"],
                               "runtime": runtime, "results": results})
    return {"frozen_sha256": frozen["sha256"], "runtime": runtime, "results": results}


def seal_predictions(predictions, frozen, run_results):
    payload = validate_frozen(frozen)
    if run_results.get("frozen_sha256") != frozen["sha256"]:
        raise BenchmarkError("Run belongs to another frozen experiment")
    if predictions.get("raw_artifact"):
        check_artifact(predictions["raw_artifact"])
    expected_papers = {p["paper_id"] for p in payload["papers"]}
    allowed_adapters = {a["id"] for a in payload["adapters"]}
    adapter_id = predictions.get("adapter")
    if adapter_id not in allowed_adapters:
        raise BenchmarkError("Predictions use an unfrozen adapter")
    papers = predictions.get("papers", [])
    if len(papers) != len(expected_papers) or {p["paper_id"] for p in papers} != expected_papers:
        raise BenchmarkError("Predictions must account for every frozen report, including failures")
    records = {r["paper_id"]: r for r in run_results["results"] if r["adapter"] == adapter_id}
    if set(records) != expected_papers:
        raise BenchmarkError("Converter run is incomplete")
    for paper in papers:
        if not isinstance(paper.get("review_required"), bool) or not isinstance(paper.get("flags"), list):
            raise BenchmarkError("Explicit pre-score flags and review_required are required")
        result = records[paper["paper_id"]]
        for item in result["artifacts"]:
            check_artifact(item)
        if result["requires_review"] and not paper["review_required"]:
            raise BenchmarkError("Conversion failure/partial/unknown status cannot be automatically accepted")
        for fact in paper.get("facts", []):
            evidenced_key(fact)
            source_hashes = {s["sha256"] for p in payload["papers"] if p["paper_id"] == paper["paper_id"] for s in p["sources"]}
            if fact["evidence"]["source_sha256"] not in source_hashes:
                raise BenchmarkError("Prediction cites a source outside this frozen report")
            if not isinstance(fact.get("review_required"), bool):
                raise BenchmarkError("Every prediction needs explicit pre-score review_required")
    sealed = {"frozen_sha256": frozen["sha256"], "run_sha256": digest(run_results),
              "predictions": predictions, "sealed_at_unix": time.time()}
    return {"payload": sealed, "sha256": digest(sealed)}


def score(sealed, frozen, *, gold_paths=None, baseline=None):
    payload = validate_frozen(frozen)
    if digest(sealed["payload"]) != sealed.get("sha256") or sealed["payload"]["frozen_sha256"] != frozen["sha256"]:
        raise BenchmarkError("Predictions changed after sealing or belong to another experiment")
    if sealed["payload"]["predictions"].get("raw_artifact"):
        check_artifact(sealed["payload"]["predictions"]["raw_artifact"])
    predictions = {p["paper_id"]: p for p in sealed["payload"]["predictions"]["papers"]}
    totals = Counter()
    details = []
    gold_digests = []
    for paper in payload["papers"]:
        paper_id = paper["paper_id"]
        frozen_gold = payload.get("gold", {}).get(paper_id)
        if frozen_gold:
            check_artifact(frozen_gold)
            gold = read_json(frozen_gold["path"])
        elif payload["split"] == "dev" and gold_paths and paper_id in gold_paths:
            gold = read_json(gold_paths[paper_id])
        else:
            raise BenchmarkError("No independently adjudicated gold for this report")
        validate_gold(gold, paper)
        gold_digests.append((paper_id, digest(gold)))
        expected = {evidenced_key(f): f for f in gold["facts"]}
        primary = {key for key, f in expected.items() if f["primary"]}
        prediction = predictions[paper_id]
        keys = [evidenced_key(f) for f in prediction.get("facts", [])]
        accepted = [evidenced_key(f) for f in prediction.get("facts", [])
                    if not prediction["review_required"] and not f["review_required"]]
        raw_hits = sum((Counter(keys) & Counter(expected.keys())).values())
        hits = sum((Counter(accepted) & Counter(expected.keys())).values())
        errors = Counter(accepted) - Counter(expected.keys())
        primary_hits = len(set(accepted) & primary)
        wrong = Counter()
        for key, multiplicity in errors.items():
            # Diagnose an attribution change only with matching population/outcome.
            choices = [candidate for candidate in expected if candidate[:2] == key[:2] and candidate[3] == key[3]]
            if choices:
                near = min(choices, key=lambda c: sum(a != b for a, b in zip(c, key)))
                for field in ("arm", "timepoint", "statistic", "uncertainty_type"):
                    i = FACT_FIELDS.index(field)
                    if near[i] != key[i]:
                        wrong["wrong_" + field] += multiplicity
        row = {"paper_id": paper_id, "gold_facts": len(expected), "primary_facts": len(primary),
               "predicted_facts": len(keys), "raw_correct": raw_hits,
               "auto_accepted_facts": len(accepted), "auto_correct": hits,
               "auto_primary_correct": primary_hits, "unflagged_critical_errors": sum(errors.values()),
               "flagged_report": int(prediction["review_required"] or any(f["review_required"] for f in prediction.get("facts", []))),
               "flagged_facts": len(keys) - len(accepted), **wrong}
        totals.update({k: v for k, v in row.items() if k != "paper_id"})
        details.append(row)
    def ratio(a, b):
        return totals[a] / totals[b] if totals[b] else None
    result = {"schema_version": SCHEMA_VERSION, "frozen_sha256": frozen["sha256"],
              "cohort_sha256": digest([(p["paper_id"], p["sources"]) for p in payload["papers"]]),
              "evaluation_sha256": digest(payload["evaluation"]), "split": payload["split"],
              "experiment": payload.get("experiment"), "source_policy": payload["evaluation"]["source_policy"],
              "gold_sha256": digest(gold_digests),
              "adapter": sealed["payload"]["predictions"]["adapter"], "reports": len(details),
              "trial_families": len({p["verification"]["trial_family"] for p in payload["papers"]}),
              "counts": dict(totals), "raw_precision": ratio("raw_correct", "predicted_facts"),
              "raw_recall": ratio("raw_correct", "gold_facts"),
              "auto_precision": ratio("auto_correct", "auto_accepted_facts"),
              "auto_recall": ratio("auto_correct", "gold_facts"),
              "auto_primary_recall": ratio("auto_primary_correct", "primary_facts"),
              "flagged_report_fraction": totals["flagged_report"] / len(details), "per_report": details,
              "uncertainty": "Observed sample only. Reports within trial families are dependent; zero observed errors is not zero production risk."}
    if baseline is not None:
        if any(baseline.get(key) != result[key] for key in ("cohort_sha256", "evaluation_sha256", "gold_sha256", "split")):
            raise BenchmarkError("Baseline must use the same sources, split, and extraction configuration")
        result["acceptance"] = {"no_unflagged_critical_errors": totals["unflagged_critical_errors"] == 0,
                                "nonzero_automatic_primary_coverage": totals["auto_primary_correct"] > 0,
                                "primary_recall_no_regression": (result["auto_primary_recall"] is not None
                                     and baseline.get("auto_primary_recall") is not None
                                     and result["auto_primary_recall"] >= baseline["auto_primary_recall"])}
        result["acceptance"]["passed"] = all(result["acceptance"].values())
    else:
        result["acceptance"] = {"passed": None, "reason": "Baseline comparison required"}
    return result
