#!/usr/bin/env python3
"""Classify each extracted study as treatment / prevention / both / exclude.

The screening `studies_treatment` flag is too unreliable to route the final
feed, so this asks the model to judge each *extracted* record from its
participants (including the topic's condition definition), inclusion criteria,
arms and outcome framing. The export uses the result to keep the dashboard
feed on the review's focus (``topic.indication_focus``, default "treatment"):
studies of the other indication go to a side feed and ``exclude`` records are
dropped.

The prompt is topic-generic (``prompts/classify_study_indication.txt``); a
project may override it with ``prompts/<slug>/classify_study_indication.txt``
(long COVID does). The participants field holding the condition definition is
``topic.condition_field`` — the same key extract_pass2.py writes.

Reads:
    projects/{slug}/data/trial_extractions_pass2.jsonl + observational_extractions.jsonl
    projects/{slug}/data/trial_screening.csv (doi -> studies_treatment, passed as a hint)
Writes:
    projects/{slug}/data/intervention_indication_map.json  -- {paper_id: {indication, rationale}}
    projects/{slug}/data/intervention_indication_report.csv -- audit (incl. title + hint)
Model:
    config.models.canonicalize (default "sonnet"; overridable via --model).
Resumability:
    paper_ids already in the map are skipped unless --force; the map is written
    atomically after every batch.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.batch_classify import classify_batches, load_map
from ber_core.identity import join_key
from ber_core.claude_cli import set_log_dest_dir
from ber_core.constants import is_failed_status
from ber_core.utils import (RESET, YELLOW, load_config, load_prompt_template, log_status, project_paths,
                   read_jsonl)

GREEN = "\033[32m"
CLI_TIMEOUT = 300
MAX_BATCH_RETRIES = 2

SOURCE_FILES = ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl")
MAP_FILE = "intervention_indication_map.json"
REPORT_FILE = "intervention_indication_report.csv"
PROMPT_FILE = "classify_study_indication.txt"
VALID = {"treatment", "prevention", "both", "exclude"}

# Participants keys that have held the condition definition. The first is the
# generic name; the second is the historical default of extract_pass2.py.
CONDITION_FIELD_FALLBACKS = ("condition_definition", "long_covid_definition")

# Arm "intervention" names that are really placeholders for NO treatment given —
# observational cohorts / natural-history groups the extractor recorded as a
# pseudo-arm. Used to derive the `intervention_administered` hint.
_NO_TX_RE = re.compile(
    r"no (specific )?intervention|natural history|observational (group|cohort|study)"
    r"|no treatment|untreated|cohort followed", re.I)


def _load_treatment_hint(data_dir: Path) -> dict[str, str]:
    """doi -> studies_treatment (screening flag), used only as a weak prior."""
    fp = data_dir / "trial_screening.csv"
    out: dict[str, str] = {}
    if not fp.exists():
        return out
    with open(fp, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = join_key(row.get("doi") or row.get("paper_id") or
                           ("pmid_" + str(row["pmid"]) if row.get("pmid") else ""))
            if doi and doi not in out:
                out[doi] = (row.get("studies_treatment") or "").strip().lower()
    return out


def detect_condition_field(data_dir: Path, configured: str | None) -> str:
    """The participants key holding the condition definition in this corpus.

    ``topic.condition_field`` when configured; otherwise whichever historical
    key the extraction actually wrote (extract_pass2.py's default has been
    ``long_covid_definition``), so a project keeps the payload it was
    classified with; ``condition_definition`` when neither appears.
    """
    if configured:
        return configured
    seen = set()
    for fname in SOURCE_FILES:
        fp = data_dir / fname
        if not fp.exists():
            continue
        for rec in read_jsonl(fp, on_error="skip"):   # a sniff; _collect warns
            pt = rec.get("participants") or {}
            if isinstance(pt, dict):
                seen.update(k for k in CONDITION_FIELD_FALLBACKS if k in pt)
        if seen:
            break
    return next((k for k in CONDITION_FIELD_FALLBACKS if k in seen), CONDITION_FIELD_FALLBACKS[0])


def _collect(data_dir: Path, field: str) -> dict[str, dict]:
    """paper_id -> compact study summary for the prompt (one entry per record)."""
    out: dict[str, dict] = {}
    for fname in SOURCE_FILES:
        fp = data_dir / fname
        if not fp.exists():
            log_status(0, 0, "SKIP", f"missing {fname}")
            continue
        n = 0
        for rec in read_jsonl(fp, on_error="warn"):
            if is_failed_status(rec.get("_status")):
                continue
            pid = str(rec.get("paper_id", "")).strip()
            if not pid or pid in out:
                continue
            sd = rec.get("study_design") or {}
            pt = rec.get("participants") or {}
            arms = [a for a in (sd.get("arms") or []) if a.get("type") == "intervention"]
            ivs = []
            for a in arms:
                nm = (a.get("intervention_name") or "").strip()
                if nm and nm not in ivs:
                    ivs.append(nm)
            # Did the study actually GIVE a treatment? False when the only
            # "intervention" arms are observational placeholders (no-intervention /
            # natural-history) or there are no named intervention arms at all — a
            # strong signal the record is a non-interventional study -> exclude.
            intervention_administered = any(nm and not _NO_TX_RE.search(nm) for nm in ivs)
            prim = [(o.get("name") or o.get("symptom_domain") or "")
                    for o in (rec.get("outcomes") or []) if o.get("is_primary")]
            definition = pt.get(field) or ""
            if not isinstance(definition, (str, list)):
                definition = str(definition)
            out[pid] = {
                "paper_id": pid,
                "title": (rec.get("title") or "")[:200],
                "design_type": sd.get("design_type") or "",
                "interventions": ivs[:6],
                "intervention_administered": intervention_administered,
                field: definition[:240],
                "inclusion": (str(pt.get("key_inclusion_criteria") or ""))[:240],
                "primary_outcomes": [p for p in prim if p][:4],
            }
            # Post-infection topics (e.g. a per-review prompt that extracts it):
            # passed through only when the extraction recorded it.
            if pt.get("min_time_since_infection_weeks") is not None:
                out[pid]["min_weeks_since_infection"] = pt["min_time_since_infection_weeks"]
            n += 1
        log_status(0, 0, "OK", f"{fname}: scanned {n} records")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="classify at most N (smoke test)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = project_paths(cfg)
    data_dir = paths["data_dir"]
    slug = paths["slug"]
    topic_name = paths["topic_name"]
    topic = cfg.get("topic", {}) or {}
    topic_desc = topic.get("description", "") or ""
    condition_field = detect_condition_field(
        data_dir, (topic.get("condition_field") or "").strip() or None)
    model = args.model or (cfg.get("models", {}) or {}).get("canonicalize", "sonnet")
    set_log_dest_dir(paths["project_dir"] / "claude_code_logs")

    print(f"{GREEN}Classifying treatment/prevention indication for '{topic_name}'{RESET}",
          file=sys.stderr)

    studies = _collect(data_dir, condition_field)
    if not studies:
        print(f"{YELLOW}No extracted records found under {data_dir}; nothing to do.{RESET}",
              file=sys.stderr)
        return 0
    hint = _load_treatment_hint(data_dir)
    for pid, s in studies.items():
        s["studies_treatment_hint"] = hint.get(join_key(pid), "")
    print(f"  {len(studies)} extracted records", file=sys.stderr)

    if args.dry_run:
        for pid in list(studies)[:15]:
            s = studies[pid]
            print(f"    [{s['studies_treatment_hint'] or '?'}] {s['title'][:90]}", file=sys.stderr)
        return 0

    map_path = data_dir / MAP_FILE
    existing: dict[str, dict] = {} if args.force else load_map(map_path, label=MAP_FILE)
    if existing:
        print(f"  loaded {len(existing)} existing classifications (resume)", file=sys.stderr)

    todo = [pid for pid in studies if pid not in existing]
    if args.limit:
        todo = todo[:args.limit]
    print(f"  {len(todo)} to classify ({len(existing)} already done)", file=sys.stderr)

    prompt_tmpl = load_prompt_template(PROMPT_FILE, slug=slug)

    def make_prompt(batch):
        payload = [dict(studies[pid]) for pid in batch]
        return (prompt_tmpl
                .replace("{topic_name}", topic_name)
                .replace("{topic_description}", topic_desc)
                .replace("{condition_field}", condition_field)
                .replace("{studies_json}", json.dumps(payload, ensure_ascii=False)))

    def accept(batch, mapping):
        hits = {}
        for pid in batch:
            v = mapping.get(pid)
            ind = ((v.get("indication") if isinstance(v, dict) else None) or "").strip().lower()
            if ind in VALID:
                hits[pid] = {"indication": ind,
                             "rationale": (v.get("rationale") or "").strip()[:300]}
        return hits

    classify_batches(todo, batch_size=args.batch_size, make_prompt=make_prompt, accept=accept,
                     model=model, label="indication", store=existing, store_path=map_path,
                     timeout=CLI_TIMEOUT, retries=MAX_BATCH_RETRIES)

    rows = []
    for pid, s in studies.items():
        cl = existing.get(pid, {})
        rows.append({
            "paper_id": pid,
            "indication": cl.get("indication", ""),
            "studies_treatment_hint": s.get("studies_treatment_hint", ""),
            "title": s["title"],
            "rationale": cl.get("rationale", ""),
        })
    order = {"prevention": 0, "exclude": 1, "both": 2, "treatment": 3, "": 4}
    rows.sort(key=lambda r: (order.get(r["indication"], 5), r["title"].lower()))
    with open(data_dir / REPORT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["paper_id", "indication", "studies_treatment_hint", "title", "rationale"])
        w.writeheader()
        w.writerows(rows)

    dist = Counter(v.get("indication") for v in existing.values() if isinstance(v, dict))
    print(f"{GREEN}Done.{RESET} classified {len(existing)}/{len(studies)}:", file=sys.stderr)
    for k in ("treatment", "prevention", "both", "exclude"):
        print(f"    {dist.get(k, 0):>4}  {k}", file=sys.stderr)
    print(f"  map:    {map_path}", file=sys.stderr)
    print(f"  report: {data_dir / REPORT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
