#!/usr/bin/env python3
"""Final, opt-in INSPECT-SR systematic-review stage, using Codex CLI.

Example:
  python3 steps/systematic_review.py --config configs/config_my_topic.yaml \
      --dashboard <website checkout>/data/birds_eye_reviews/<slug>

All existing screening, paper and extraction artifacts are read-only. Outputs and
source snapshots are resumable under <project>/systematic_review/. The established
data/review_document.md artifact is refreshed only after a successful manuscript.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.pipeline import load_project
from ber_core.inspect_corpus import collect_corpus
from ber_core.inspect_sources import digest, download_retractions, now, write_json
from ber_core.inspect_sr import MODEL, assess, adjudicate, enrich_corpus
from ber_core.inspect_report import write_manuscript
from ber_core.inspect_visuals import prepare_visuals
from ber_core.inspect_scope import audit_scope
from ber_core.inspect_dashboard import dashboard_assessments
from ber_core.codex_backend import LUNA_MODEL, TERRA_MODEL, resolve_model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--dashboard", type=Path, help="Published bundle directory; every displayed report is audited")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--retraction-watch", type=Path)
    parser.add_argument("--model", default=MODEL, help="Explicit Codex model: luna or terra (default: terra)")
    parser.add_argument("--reviewers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1, help="Concurrent Codex assessment calls; default 1, matching one serial worker")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--visual-pages", type=int, default=12, help="Maximum main-PDF pages per RCT for visual screening; 0 disables it")
    parser.add_argument("--prepare-only", action="store_true", help="Snapshot and retrieve sources without model calls")
    parser.add_argument("--resume-snapshot", action="store_true", help="Continue the existing frozen corpus instead of reading live pipeline changes")
    parser.add_argument("--offline", action="store_true", help="Use cached external sources; missing sources remain unavailable")
    parser.add_argument("--refresh-sources", action="store_true", help="Recheck notices and refresh external source caches")
    parser.add_argument("--limit", type=int, help="Smoke-test subset; writes to a separate smoke output directory")
    parser.add_argument("--adjudications", type=Path, help="Named human adjudications with input fingerprints")
    parser.add_argument("--supplemental-evidence", type=Path, help="Named, dated reviewer evidence keyed by paper ID and source ID")
    args = parser.parse_args(argv)
    if args.resume_snapshot and (args.refresh_sources or args.limit or args.supplemental_evidence):
        parser.error("Snapshot resume cannot be combined with refreshed sources, a new limit or supplemental evidence")
    if args.batch_size < 1 or args.timeout < 1 or not 1 <= args.workers <= 10 or (args.limit is not None and args.limit < 1):
        parser.error("Batch size, timeout and limit must be positive; workers must be 1-10")
    if args.model not in {"luna", "terra", LUNA_MODEL, TERRA_MODEL}:
        parser.error("Choose luna or terra explicitly; no silent model substitution")
    model = resolve_model(args.model)
    if not args.prepare_only and os.environ.get("BER_LLM_PROVIDER") not in {None, "", "codex_cli"}:
        parser.error("INSPECT-SR requires the codex_cli provider. Select Codex in the Studio before running this stage.")
    project = load_project(args.config)
    out = args.out_dir or project.project_dir / "systematic_review"
    if args.limit:
        out = out / ("smoke-" + str(args.limit))
    out.mkdir(parents=True, exist_ok=True)
    # Prevent overlapping runs from racing response and report files.
    import fcntl
    with (out / ".run.lock").open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another systematic-review process is using this output directory")
        try:
            if args.resume_snapshot:
                manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
                audit = json.loads((out / "selection_audit.json").read_text(encoding="utf-8"))
                records = [json.loads((out / "corpus" / (digest(r["paper_id"])[:20] + ".json")).read_text(encoding="utf-8")) for r in audit]
                if len(records) != manifest["counts"]["union_reports"]:
                    raise ValueError("Frozen corpus count does not match the manifest")
                project.config = manifest["config"]
            else:
                records, manifest = collect_corpus(project, out, args.dashboard)
            if args.limit:
                records = [r for r in records if r["selection_status"] == "candidate"][:args.limit]
                manifest["smoke_test"] = True
                manifest["counts"]["union_reports"] = len(records)
                manifest["counts"]["with_text"] = sum(bool(r["texts"]) for r in records)
            if not records:
                raise ValueError("No screened-in clinical reports or dashboard reports found")
            print(json.dumps(manifest["counts"]), flush=True)
            write_json(out / "run_status.json", {"state": "preparing", "updated_at": now(), "model": model})
            rw = args.retraction_watch or project.project_dir.parent / "_resources/retraction_watch.csv"
            if not args.resume_snapshot and not args.offline and (args.refresh_sources or not rw.exists()):
                download_retractions(rw)
            if not args.resume_snapshot:
                records = enrich_corpus(records, out, rw, offline=args.offline, refresh=args.refresh_sources)
                records = prepare_visuals(records, out, args.visual_pages)
            if args.supplemental_evidence:
                extra = json.loads(args.supplemental_evidence.read_text(encoding="utf-8"))
                known = {r["paper_id"] for r in records}
                if not isinstance(extra, dict) or set(extra) - known:
                    raise ValueError("Supplemental evidence must be keyed by current paper IDs")
                for record in records:
                    record["supplemental_evidence"] = extra.get(record["paper_id"], {})
                    for source_id, evidence in record["supplemental_evidence"].items():
                        if not isinstance(evidence, dict) or not all(evidence.get(k) for k in ("kind", "text", "reviewer", "reviewed_at")):
                            raise ValueError("Supplemental evidence needs kind, text, reviewer and reviewed_at")
                        record["sources"]["supplemental_" + source_id] = evidence
            for record in records:
                write_json(out / "corpus" / (digest(record["paper_id"])[:20] + ".json"), record)
            if not args.resume_snapshot:
                manifest["resources"] = {"retraction_watch": str(rw.resolve()), "sha256": digest(rw.read_bytes()) if rw.exists() else None}
            manifest["model"] = model
            manifest["machine_reviewers_requested"] = args.reviewers
            manifest["codex_workers"] = args.workers
            write_json(out / "manifest.json", manifest)
            if args.prepare_only:
                write_json(out / "run_status.json", {"state": "prepared", "updated_at": now(), "reports": len(records)})
                return 0
            write_json(out / "run_status.json", {"state": "assessing", "updated_at": now(), "reports": len(records)})
            results = assess(records, out, project.config, model=model, reviewers=args.reviewers,
                             batch_size=args.batch_size, timeout=args.timeout, workers=args.workers)
            write_json(out / "run_status.json", {"state": "auditing_scope", "updated_at": now(), "reports": len(records)})
            scope_audits = audit_scope(records, results, out, project.config, model=model, workers=args.workers)
            decisions = adjudicate(records, results, args.adjudications, scope_audits=scope_audits)
            write_json(out / "decisions.json", decisions)
            if args.limit:
                # Smoke tests never overwrite the real Studio manuscript.
                write_json(out / "run_status.json", {"state": "smoke_complete", "updated_at": now(), "reports": len(records)})
                return 0
            write_json(out / "run_status.json", {"state": "writing", "updated_at": now(), "reports": len(records)})
            summary = write_manuscript(project, records, manifest, decisions, out, model=model, workers=args.workers)
            completed_at = now()
            write_json(out / "inspect_sr.json", dashboard_assessments(
                decisions, records, assessed_at=completed_at, model=model))
            write_json(out / "run_status.json", {"state": "automated_draft_complete", "updated_at": completed_at,
                                                "human_review_required": True, "summary": summary})
            print(f"Review written: {out / 'systematic_review.md'}", flush=True)
        except BaseException as exc:
            write_json(out / "run_status.json", {"state": "failed", "updated_at": now(),
                                                "error_type": type(exc).__name__, "message": str(exc)[:500]})
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
