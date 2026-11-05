"""Synthesize a narrative review document (standalone; NOT a canonical step).

Writes a markdown systematic-review narrative from the finished dashboard
bundle. Deliberately off-canonical (like ``stage_eligible.py``): it spends LLM
money, so it must never auto-run inside a full pipeline run — the studio fires
it as an explicit background job, and the CLI runs it by hand.

The LLM writes connective prose over a deterministically precomputed payload;
it is instructed never to compute or invent numbers. A verification pass then
regexes every number out of each generated section and checks it against the
numbers actually present in that section's payload — any section containing an
orphan number is dropped back to a deterministic template rendering (the same
renderer ``--no-llm`` uses for everything), so tables and figures are
trustworthy even when the prose is not.

Depth controls how much evidence the model reads, not just length:

    brief     one call, aggregates only (summary/meta/PRISMA)        ~1k words
    standard  one call, + per-trial rows and per-paper summaries     ~3k words
    deep      one call per section, + pass-2 effect/RoB/AE details   ~8k words

Outputs (into the project data dir):
    projects/<slug>/data/review_document.md
    projects/<slug>/data/review_document.json   (metadata sidecar)

Usage:
    python steps/synthesize_review.py --config configs/config_my_topic.yaml
    python steps/synthesize_review.py --config ... --depth deep --audience plain
    python steps/synthesize_review.py --config ... --no-llm     # deterministic only
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

from ber_core.utils import load_config, load_prompt_template, read_jsonl, slugify

try:
    from ber_core.claude_cli import call_claude, effective_provider, set_log_dest_dir
except Exception:  # CLI helper unavailable → deterministic-only mode
    call_claude = None
    effective_provider = None

    def set_log_dest_dir(_):  # type: ignore
        pass

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

DEPTHS = ("brief", "standard", "deep")
AUDIENCES = ("clinician", "plain")
MODELS = ("auto", "haiku", "sonnet", "opus")
DEPTH_MODELS = {"brief": "haiku", "standard": "sonnet", "deep": "opus"}
DEPTH_WORDS = {"brief": "800-1,200", "standard": "2,500-4,000", "deep": "6,000-10,000"}
DEPTH_RANK = {"brief": 0, "standard": 1, "deep": 2}

# (id, title, minimum depth at which the section is on by default). Order is
# document order. IDs are stable — the studio's checklist and Job.params use
# them, so renaming one silently disables it for existing configs.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("key_findings", "Key findings", "brief"),
    ("background", "Background", "standard"),
    ("methods_prisma", "Methods and study flow", "standard"),
    ("evidence_base", "The evidence base at a glance", "brief"),
    ("results_by_intervention", "Results by intervention", "standard"),
    ("pooled_results", "Pooled results", "brief"),
    ("intervention_deep_dives", "Intervention deep dives", "deep"),
    ("safety", "Safety and adverse events", "standard"),
    ("risk_of_bias", "Risk of bias", "deep"),
    ("certainty_grade", "Certainty of the evidence", "deep"),
    ("limitations", "Limitations", "brief"),
    ("research_gaps", "Research gaps", "deep"),
    ("conclusions", "Conclusions", "standard"),
)
SECTION_IDS = tuple(s[0] for s in SECTIONS)
SECTION_TITLES = {sid: title for sid, title, _ in SECTIONS}

AUDIENCE_INSTRUCTIONS = {
    "clinician": (
        "Write for clinicians and researchers: precise, structured, "
        "Cochrane-review register. Use standard terminology (SMD, CI, I², "
        "GRADE) without explaining it."
    ),
    "plain": (
        "Write for an educated lay reader: plain language, short sentences, "
        "explain any statistical term the first time it appears, and lead "
        "with what the evidence means in practice."
    ),
}


def default_sections(depth: str) -> list[str]:
    rank = DEPTH_RANK[depth]
    return [sid for sid, _, tier in SECTIONS if DEPTH_RANK[tier] <= rank]


# ── Loaders ───────────────────────────────────────────────────────────────────

def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError):
        print(f"  WARNING: unparseable JSON in {path.name}; ignoring it",
              file=sys.stderr)
        return None


def _read_jsonl(path: Path) -> list[dict]:
    return read_jsonl(path, on_error="warn")


def load_inputs(slug: str, config: dict | None = None) -> dict:
    """Read every bundle artifact the synthesis may draw on.

    A current summary and input fingerprint are required; everything else degrades to an absence the
    templates and prompts state honestly. The pooled groups come from the
    bundle's meta_analysis.json (export_dashboard.py --meta-analysis), which
    carries per-group GRADE ``certainty`` and ``rob_summary`` from the shared
    engine (pool_meta_analysis.py writes the identical shape to data/). A bundle
    built without --meta-analysis has no groups, and the certainty section says
    so.
    """
    dash = PROJECTS_DIR / slug / "dashboard"
    data = PROJECTS_DIR / slug / "data"
    paths = {
        "summary": dash / "summary.json",
        "rows": dash / "dashboard_rows.jsonl",
        "prisma": dash / "prisma.json",
        "meta": dash / "meta_analysis.json",
        "extractions": (dash / "trial_extractions.jsonl"
                        if (dash / "trial_extractions.jsonl").exists()
                        else data / "trial_extractions_pass2.jsonl"),
        "meta_report": data / "meta_analysis_report.md",
    }
    summary = _read_json(paths["summary"])
    if not summary:
        print(f"\nERROR: {paths['summary']} is missing or empty.\n"
              f"       Build the dashboard bundle (export_dashboard.py) "
              f"before synthesizing a review.\n", file=sys.stderr)
        sys.exit(1)
    from ber_core.scientific import RULES_VERSION
    if summary.get("scientific_rules_version") != RULES_VERSION:
        raise ValueError("Rebuild the dashboard with current scientific validation before narrative synthesis")
    from ber_core.bundles import INPUT_MANIFEST, source_fingerprint
    saved = _read_json(dash / INPUT_MANIFEST)
    current = source_fingerprint(data, config)
    if not saved or saved.get("files") != current["files"] or (config is not None and saved.get("config") != current["config"]):
        raise ValueError("Dashboard input artifacts changed or lack a fingerprint; rebuild before narrative synthesis")
    return {
        "summary": summary,
        "rows": _read_jsonl(paths["rows"]),
        "prisma": _read_json(paths["prisma"]) or {},
        "meta": _read_json(paths["meta"]) or {},
        "extractions": _read_jsonl(paths["extractions"]),
        "meta_report_md": (paths["meta_report"].read_text(encoding="utf-8")
                           if paths["meta_report"].exists() else ""),
        "fingerprint": {str(p): p.stat().st_mtime_ns
                        for p in paths.values() if p.exists()},
    }


# ── Payload assembly (all deterministic) ──────────────────────────────────────

def _compact_row(row: dict) -> dict:
    eff = row.get("eff") or {}
    out = {
        "id": row.get("id") or row.get("doi"),
        "title": row.get("title"),
        "year": row.get("year"),
        "design": row.get("design_type"),
        "n": row.get("n"),
        "rob": row.get("rob"),
        "verdict": row.get("verdict"),
        "blinding": row.get("blinding"),
        "countries": row.get("countries"),
    }
    if eff.get("value") is not None:
        out["effect"] = {k: eff.get(k)
                        for k in ("measure", "value", "ci_low", "ci_high", "p")}
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _compact_group(g: dict) -> dict:
    pooled = g.get("pooled") or {}
    out = {
        "name": g.get("ingredient") or g.get("spray_type") or g.get("name"),
        "outcome_domain": g.get("outcome_domain"),
        "n_trials": g.get("n_trials"),
        "effect_measure": g.get("effect_measure"),
        "total_n": g.get("total_n"),
        "comparator": g.get("comparator"), "outcome": g.get("outcome_concept"),
        "unit": g.get("unit"), "basis": g.get("basis"),
        "ci_method": pooled.get("ci_method"), "ci_sensitivity": g.get("ci_sensitivity"),
        "method_dependent": g.get("method_dependent"),
        "pooled": {k: pooled.get(k)
                   for k in ("effect", "ci_low", "ci_high", "p_value", "i2",
                             "prediction_low", "prediction_high", "model")
                   if pooled.get(k) is not None},
    }
    cert = g.get("certainty") or {}
    if cert.get("grade"):
        out["certainty"] = {
            "grade": cert.get("grade"),
            "start": cert.get("start"),
            # Domain names, once each (an I² >= 75% double downgrade is one domain).
            "downgrades": list(dict.fromkeys(
                d.get("domain") for d in cert.get("downgrades") or [] if d.get("domain"))),
        }
    if g.get("rob_summary"):
        out["rob_summary"] = g["rob_summary"]
    return {k: v for k, v in out.items() if v not in (None, "", {}, [])}


def build_payload(inputs: dict, depth: str, config: dict) -> dict:
    """The single deterministic payload the LLM writes prose over.

    Everything the prose may cite must be in here — the number-verification
    pass treats any number absent from the payload as invented.
    """
    from ber_core.scientific import prepare_synthesis, RULES_VERSION
    prepared = prepare_synthesis(inputs["extractions"], config.get("meta_analysis"))
    summary = inputs["summary"]
    charts = summary.get("charts") or {}
    payload: dict = {
        "topic": {
            "name": (config.get("topic") or {}).get("name", ""),
            "description": (config.get("topic") or {}).get("description", ""),
        },
        "totals": summary.get("totals") or {},
        "feasibility": summary.get("feasibility") or {},
        "prisma": inputs["prisma"],
        "by_intervention": charts.get("by_intervention") or [],
        "by_design": charts.get("by_design") or [],
        "by_rob": charts.get("by_rob") or [],
        "by_year": charts.get("by_year") or [],
        "pooled_groups": [_compact_group(g)
                          for g in (inputs["meta"].get("groups") or [])
                          if inputs["meta"].get("rules_version") == RULES_VERSION and g.get("pooled")],
        "scientific_rules_version": RULES_VERSION,
        "withheld_comparisons": prepared["withheld"],
        "definitions": summary.get("definitions") or {},
    }
    if depth in ("standard", "deep"):
        trials = []
        for row in inputs["rows"]:
            t = _compact_row(row)
            t.pop("effect", None)
            t["selected_comparisons"] = [s for s in prepared["selected"] if s["paper_id"] == row.get("id")]
            t["withheld_reasons"] = [{"outcome": s["outcome_name"], "issues": s["issues"]} for s in prepared["withheld"] if s["paper_id"] == row.get("id")]
            trials.append(t)
        payload["trials"] = trials
    if depth == "deep":
        details = []
        for e in inputs["extractions"]:
            effects = [s for s in prepared["selected"] if s["paper_id"] == e.get("paper_id")]
            rob = e.get("risk_of_bias") or {}
            details.append({
                "id": e.get("paper_id"),
                "effects": effects,
                "risk_of_bias": {k: rob.get(k)
                                 for k in ("tool", "overall_judgment",
                                           "key_concern")
                                 if rob.get(k)},
                "adverse_events": e.get("adverse_events"),
            })
        payload["trial_details"] = details
    return payload


# Deep mode sends each section only the payload keys it needs, so a section's
# verification set matches what its prompt could legitimately cite.
SECTION_PAYLOAD_KEYS = {
    "key_findings": ("topic", "totals", "feasibility", "by_intervention",
                     "pooled_groups"),
    "background": ("topic", "totals", "by_year"),
    "methods_prisma": ("topic", "prisma", "totals", "definitions"),
    "evidence_base": ("topic", "totals", "by_design", "by_year", "by_rob",
                      "trials"),
    "results_by_intervention": ("topic", "by_intervention", "trials",
                                "pooled_groups"),
    "pooled_results": ("topic", "pooled_groups", "feasibility"),
    "intervention_deep_dives": ("topic", "by_intervention", "trials",
                                "trial_details", "pooled_groups"),
    "safety": ("topic", "trials", "trial_details"),
    "risk_of_bias": ("topic", "by_rob", "trials", "trial_details"),
    "certainty_grade": ("topic", "pooled_groups", "feasibility"),
    "limitations": ("topic", "feasibility", "definitions", "totals"),
    "research_gaps": ("topic", "feasibility", "by_intervention",
                      "pooled_groups"),
    "conclusions": ("topic", "totals", "pooled_groups", "feasibility"),
}


def section_slice(payload: dict, section_id: str) -> dict:
    keys = SECTION_PAYLOAD_KEYS.get(section_id, tuple(payload))
    return {k: payload[k] for k in keys if k in payload}


# ── Number verification ───────────────────────────────────────────────────────

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Small counts read naturally in prose ("the 3 crossover trials") and appear as
# list lengths the payload doesn't spell out; whitelisting them keeps the
# fallback for real fabrications (effect sizes, percentages, Ns).
_ALWAYS_ALLOWED = {str(i) for i in range(13)} | {"95", "100"}


def _num_forms(x) -> set[str]:
    """Every string form of a payload number the prose may legitimately use."""
    out: set[str] = set()
    try:
        f = float(x)
    except (TypeError, ValueError):
        return out
    for v in {f, abs(f)}:
        out.add(f"{v:g}")
        out.add(f"{v:.1f}")
        out.add(f"{v:.2f}")
        if v == int(v):
            out.add(str(int(v)))
            out.add(f"{int(v):,}")
        if 0 < abs(v) <= 1:  # proportion quoted as a percentage
            out.add(f"{v * 100:g}")
            out.add(f"{v * 100:.1f}")
            out.add(f"{v * 100:.0f}")
    return out


def allowed_numbers(payload) -> set[str]:
    acc: set[str] = set(_ALWAYS_ALLOWED)

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple)):
            acc.update(_num_forms(len(obj)))
            for v in obj:
                walk(v)
        elif isinstance(obj, bool):
            pass
        elif isinstance(obj, (int, float)):
            acc.update(_num_forms(obj))
        elif isinstance(obj, str):
            for m in _NUM_RE.findall(obj):
                acc.update(_num_forms(m.replace(",", "")))

    walk(payload)
    return acc


def orphan_numbers(prose: str, allowed: set[str]) -> list[str]:
    """Numbers in *prose* that don't appear anywhere in the payload."""
    orphans = []
    for tok in _NUM_RE.findall(prose):
        raw = tok.replace(",", "")
        if raw in allowed:
            continue
        try:
            if f"{float(raw):g}" in allowed or f"{float(raw):.1f}" in allowed:
                continue
        except ValueError:
            pass
        orphans.append(tok)
    return orphans


# ── Deterministic template renderers (the --no-llm document + fallbacks) ──────

def _fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _t_key_findings(p):
    lines = []
    feas = p.get("feasibility") or {}
    if feas.get("recommendation"):
        lines.append(f"- {feas['recommendation']}")
    for g in (p.get("pooled_groups") or [])[:5]:
        pooled = g.get("pooled") or {}
        if pooled.get("effect") is None:
            continue
        cert = (g.get("certainty") or {}).get("grade")
        cert_txt = f" ({cert} certainty)" if cert else ""
        lines.append(
            f"- **{g.get('name')}** — {g.get('outcome_domain')}: "
            f"{g.get('effect_measure', 'SMD')} {_fmt(pooled.get('effect'))} "
            f"(95% CI {_fmt(pooled.get('ci_low'))} to "
            f"{_fmt(pooled.get('ci_high'))}; {g.get('n_trials')} trials)"
            f"{cert_txt}")
    if not lines:
        lines.append("- No pooled results are available; see the evidence "
                     "base and per-study findings below.")
    return "\n".join(lines)


def _t_background(p):
    topic = p.get("topic") or {}
    totals = p.get("totals") or {}
    return (f"{topic.get('description', '')}\n\n"
            f"This review draws on {totals.get('n_trials', 0)} studies "
            f"({totals.get('n_rct', 0)} randomized controlled trials, "
            f"{totals.get('n_observational', 0)} observational).")


def _t_methods_prisma(p):
    pr = p.get("prisma") or {}
    lines = ["Study flow (PRISMA):", ""]
    # Bundle shape: {"main_flow": [{label, n, stage}, ...]} (ber_core/prisma
    # to_visualize_json); tolerate a flat counts dict too.
    for box in pr.get("main_flow") or []:
        if box.get("label") and box.get("n") is not None:
            lines.append(f"- {box['label']}: {box['n']}")
    if len(lines) == 2:
        for k, v in pr.items():
            if isinstance(v, (int, float)):
                lines.append(f"- {str(k).replace('_', ' ').capitalize()}: {v}")
    if len(lines) == 2:
        lines.append("- PRISMA counts are unavailable for this review.")
    return "\n".join(lines)


def _t_evidence_base(p):
    totals = p.get("totals") or {}
    rows = [("Studies", totals.get("n_trials")),
            ("Randomized controlled trials", totals.get("n_rct")),
            ("Observational studies", totals.get("n_observational")),
            ("Total participants", totals.get("total_participants")),
            ("Countries", totals.get("n_countries")),
            ("Distinct interventions", totals.get("n_interventions"))]
    out = ["| | |", "|---|---|"]
    out += [f"| {label} | {_fmt(v, 0)} |" for label, v in rows if v is not None]
    return "\n".join(out)


def _t_results_by_intervention(p):
    items = p.get("by_intervention") or []
    if not items:
        return "No per-intervention results are available."
    out = ["| Intervention | Studies | Verdicts |", "|---|---|---|"]
    for it in items[:20]:
        verdicts = it.get("verdicts") or {}
        vtxt = ", ".join(f"{k}: {v}" for k, v in verdicts.items()) or "—"
        out.append(f"| {it.get('name', '—')} | {it.get('count', '—')} | {vtxt} |")
    return "\n".join(out)


def _t_pooled_results(p):
    groups = [g for g in p.get("pooled_groups") or [] if g.get("pooled")]
    if not groups:
        feas = p.get("feasibility") or {}
        why = feas.get("recommendation") or "No pooled analysis was generated."
        return why
    out = ["| Comparison | Outcome | k | Effect (95% CI) | I² | Certainty |",
           "|---|---|---|---|---|---|"]
    for g in groups:
        pooled = g["pooled"]
        eff = (f"{g.get('effect_measure', 'SMD')} {_fmt(pooled.get('effect'))} "
               f"({_fmt(pooled.get('ci_low'))} to {_fmt(pooled.get('ci_high'))})")
        i2 = _fmt(pooled.get("i2"), 0)
        cert = (g.get("certainty") or {}).get("grade", "—")
        out.append(f"| {g.get('name', '—')} | {g.get('outcome_domain', '—')} | "
                   f"{g.get('n_trials', '—')} | {eff} | {i2} | {cert} |")
    return "\n".join(out)


def _t_intervention_deep_dives(p):
    return _t_results_by_intervention(p)


def _t_safety(p):
    details = p.get("trial_details") or []
    n_ae = sum(1 for d in details if d.get("adverse_events"))
    if n_ae:
        return (f"Adverse-event data were extracted for {n_ae} studies; see "
                "the per-study records for details.")
    return ("Adverse-event reporting was not systematically extracted at this "
            "depth; consult the individual study records.")


def _t_risk_of_bias(p):
    items = p.get("by_rob") or []
    if not items:
        return "Risk-of-bias summaries are unavailable for this review."
    out = ["| Risk of bias | Studies |", "|---|---|"]
    out += [f"| {it.get('name', '—')} | {it.get('count', '—')} |"
            for it in items]
    return "\n".join(out)


def _t_certainty_grade(p):
    groups = [g for g in p.get("pooled_groups") or [] if g.get("certainty")]
    if not groups:
        return ("GRADE certainty ratings are unavailable: no comparison was pooled "
                "(build the dashboard with --meta-analysis to pool compatible "
                "comparisons; certainty is rated only for pooled groups).")
    out = ["| Comparison | Certainty | Downgraded for |", "|---|---|---|"]
    for g in groups:
        cert = g["certainty"]
        out.append(f"| {g.get('name', '—')} | {cert.get('grade', '—')} | "
                   f"{', '.join(cert.get('downgrades') or []) or '—'} |")
    return "\n".join(out)


def _t_limitations(p):
    feas = p.get("feasibility") or {}
    lines = [f"- {b}" for b in feas.get("blockers") or []]
    for d in feas.get("domains") or []:
        if d.get("verdict") in ("thin", "fragmented", "not_possible") and d.get("reason"):
            lines.append(f"- {d['reason']}")
    if not lines:
        lines.append("- See the per-study risk-of-bias judgments for "
                     "study-level limitations.")
    return "\n".join(lines)


def _t_research_gaps(p):
    feas = p.get("feasibility") or {}
    lines = []
    for d in feas.get("domains") or []:
        if d.get("verdict") in ("thin", "fragmented") and d.get("reason"):
            lines.append(f"- {d.get('domain', 'Evidence')}: {d['reason']}")
    if not lines:
        lines.append("- No specific evidence gaps were flagged by the "
                     "feasibility assessment.")
    return "\n".join(lines)


def _t_conclusions(p):
    totals = p.get("totals") or {}
    feas = p.get("feasibility") or {}
    parts = [f"Across {totals.get('n_trials', 0)} studies, the evidence is "
             "summarized in the sections above."]
    if feas.get("recommendation"):
        parts.append(feas["recommendation"])
    return " ".join(parts)


TEMPLATES = {
    "key_findings": _t_key_findings,
    "background": _t_background,
    "methods_prisma": _t_methods_prisma,
    "evidence_base": _t_evidence_base,
    "results_by_intervention": _t_results_by_intervention,
    "pooled_results": _t_pooled_results,
    "intervention_deep_dives": _t_intervention_deep_dives,
    "safety": _t_safety,
    "risk_of_bias": _t_risk_of_bias,
    "certainty_grade": _t_certainty_grade,
    "limitations": _t_limitations,
    "research_gaps": _t_research_gaps,
    "conclusions": _t_conclusions,
}


def render_template_section(section_id: str, payload: dict) -> str:
    return TEMPLATES[section_id](payload)


# ── LLM generation ────────────────────────────────────────────────────────────

def _depth_instructions(depth: str) -> str:
    return (f"Target length for the whole document: {DEPTH_WORDS[depth]} "
            f"words. Depth level: {depth}.")


def _sections_spec(section_ids: list[str]) -> str:
    return "\n".join(f"## {SECTION_TITLES[sid]}" for sid in section_ids)


def _split_by_heading(doc: str) -> dict[str, str]:
    """Map normalized '## ' heading text -> body text."""
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in doc.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower().rstrip(".:")
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def _match_section_body(chunks: dict[str, str], title: str) -> str | None:
    key = title.strip().lower()
    if key in chunks:
        return chunks[key]
    for heading, body in chunks.items():
        if key in heading or heading in key:
            return body
    return None


def generate_singleshot(payload, section_ids, depth, audience, model,
                        topic, slug) -> dict[str, dict]:
    """One LLM call for the whole document; verify per section."""
    # .replace, not .format: the template and the payload both contain braces
    # (the convention every .replace-filled prompt template follows).
    template = load_prompt_template("synthesize_review.txt", slug=slug)
    prompt = (template
              .replace("{topic_name}", topic.get("name", ""))
              .replace("{topic_description}", topic.get("description", ""))
              .replace("{depth_instructions}", _depth_instructions(depth))
              .replace("{audience_instructions}", AUDIENCE_INSTRUCTIONS[audience])
              .replace("{sections_spec}", _sections_spec(section_ids))
              .replace("{payload}",
                       json.dumps(payload, ensure_ascii=False, indent=1)))
    print("Progress: 0/1", flush=True)
    resp = call_claude(prompt, model, "synthesize_review",
                       timeout=3600, use_stdin=True)
    print("Progress: 1/1", flush=True)
    chunks = _split_by_heading(resp.text or "")
    allowed = allowed_numbers(payload)
    sections: dict[str, dict] = {}
    for sid in section_ids:
        body = _match_section_body(chunks, SECTION_TITLES[sid])
        if body:
            orphans = orphan_numbers(body, allowed)
            if orphans:
                print(f"  section '{sid}': invented number(s) "
                      f"{orphans[:5]} — falling back to template",
                      file=sys.stderr)
                body = None
        if body:
            sections[sid] = {"body": body, "source": "llm"}
        else:
            sections[sid] = {"body": render_template_section(sid, payload),
                             "source": "template_fallback"}
    return sections


def generate_per_section(payload, section_ids, depth, audience, model,
                         topic, slug) -> dict[str, dict]:
    """Deep mode: one LLM call per section over a section-scoped slice."""
    template = load_prompt_template("synthesize_review_section.txt", slug=slug)
    sections: dict[str, dict] = {}
    outline = _sections_spec(section_ids)
    total = len(section_ids)
    for i, sid in enumerate(section_ids):
        print(f"Progress: {i}/{total}", flush=True)
        sl = section_slice(payload, sid)
        prompt = (template
                  .replace("{topic_name}", topic.get("name", ""))
                  .replace("{topic_description}", topic.get("description", ""))
                  .replace("{audience_instructions}",
                           AUDIENCE_INSTRUCTIONS[audience])
                  .replace("{section_title}", SECTION_TITLES[sid])
                  .replace("{section_instructions}", _depth_instructions(depth))
                  .replace("{document_so_far_outline}", outline)
                  .replace("{payload_slice}",
                           json.dumps(sl, ensure_ascii=False, indent=1)))
        resp = call_claude(prompt, model, f"synthesize_{sid}",
                           timeout=1200, use_stdin=True)
        body = (resp.text or "").strip()
        # Strip a repeated heading the model may emit despite instructions.
        body = re.sub(rf"^#+\s*{re.escape(SECTION_TITLES[sid])}\s*\n",
                      "", body, flags=re.IGNORECASE)
        if body:
            orphans = orphan_numbers(body, allowed_numbers(sl))
            if orphans:
                print(f"  section '{sid}': invented number(s) "
                      f"{orphans[:5]} — falling back to template",
                      file=sys.stderr)
                body = ""
        if body:
            sections[sid] = {"body": body, "source": "llm"}
        else:
            sections[sid] = {"body": render_template_section(sid, payload),
                             "source": "template_fallback"}
    print(f"Progress: {total}/{total}", flush=True)
    return sections


# ── Document assembly ─────────────────────────────────────────────────────────

def assemble_document(sections: dict[str, dict], section_ids: list[str],
                      topic: dict, meta_report_md: str,
                      generated_at: str) -> str:
    title = topic.get("name") or "Systematic review"
    parts = [f"# {title}: an AI-synthesized evidence review", "",
             f"*Generated {generated_at}. AI-assisted synthesis over "
             f"extracted study data — verify before clinical use.*", ""]
    for sid in section_ids:
        parts.append(f"## {SECTION_TITLES[sid]}")
        parts.append("")
        parts.append(sections[sid]["body"])
        parts.append("")
    if meta_report_md:
        parts.append("## Appendix: meta-analysis methods and certainty")
        parts.append("")
        parts.append(meta_report_md.strip())
        parts.append("")
    return "\n".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Synthesize a narrative review document from the bundle")
    ap.add_argument("--config", required=True)
    ap.add_argument("--depth", choices=DEPTHS, default="standard")
    ap.add_argument("--audience", choices=AUDIENCES, default="clinician")
    ap.add_argument("--sections",
                    help="comma-separated section ids (default: depth's set)")
    ap.add_argument("--model", choices=MODELS, default="auto",
                    help="override the depth-implied model")
    ap.add_argument("--no-llm", action="store_true",
                    help="deterministic templates only (no LLM calls)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing review_document.md")
    args = ap.parse_args()

    config = load_config(args.config)
    slug = slugify((config.get("topic") or {}).get("name", "default"))
    data_dir = PROJECTS_DIR / slug / "data"
    out_md = data_dir / "review_document.md"
    out_meta = data_dir / "review_document.json"

    if args.sections:
        section_ids = [s.strip() for s in args.sections.split(",") if s.strip()]
        unknown = [s for s in section_ids if s not in SECTION_IDS]
        if unknown:
            print(f"ERROR: unknown section id(s): {', '.join(unknown)}\n"
                  f"       valid: {', '.join(SECTION_IDS)}", file=sys.stderr)
            sys.exit(2)
        # Keep document order regardless of the order given.
        section_ids = [sid for sid in SECTION_IDS if sid in section_ids]
    else:
        section_ids = default_sections(args.depth)
    if not section_ids:
        print("ERROR: no sections selected", file=sys.stderr)
        sys.exit(2)

    # After option validation, so a bad flag errors even when a doc exists.
    if out_md.exists() and not args.force:
        print(f"{out_md} already exists — pass --force to regenerate.")
        return
    set_log_dest_dir(PROJECTS_DIR / slug / "claude_code_logs")

    model = args.model
    if model == "auto":
        model = (config.get("models") or {}).get("synthesis") or "auto"
    if model == "auto":
        model = DEPTH_MODELS[args.depth]

    inputs = load_inputs(slug, config)
    payload = build_payload(inputs, args.depth, config)
    topic = payload["topic"]

    use_llm = not args.no_llm and call_claude is not None
    if use_llm:
        gen = generate_per_section if args.depth == "deep" else generate_singleshot
        sections = gen(payload, section_ids, args.depth, args.audience,
                       model, topic, slug)
    else:
        sections = {sid: {"body": render_template_section(sid, payload),
                          "source": "template"}
                    for sid in section_ids}

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    doc = assemble_document(sections, section_ids, topic,
                            inputs["meta_report_md"], generated_at)

    data_dir.mkdir(parents=True, exist_ok=True)
    out_md.write_text(doc, encoding="utf-8")
    llm_calls = (0 if not use_llm
                 else len(section_ids) if args.depth == "deep" else 1)
    out_meta.write_text(json.dumps({
        "depth": args.depth,
        "audience": args.audience,
        "model": model if use_llm else None,
        # The provider the calls actually went through (scoped value, else
        # BER_LLM_PROVIDER, else claude_cli's own default) — not a guessed default.
        "provider": (effective_provider() if use_llm and effective_provider else None),
        "generated_at": generated_at,
        "word_count": len(doc.split()),
        "llm_calls": llm_calls,
        "sections": [{"id": sid, "title": SECTION_TITLES[sid],
                      "source": sections[sid]["source"],
                      "model": model if sections[sid]["source"] == "llm" else None}
                     for sid in section_ids],
        "source_fingerprint": inputs["fingerprint"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    n_fallback = sum(1 for s in sections.values()
                     if s["source"] == "template_fallback")
    print(f"Wrote {out_md} ({len(doc.split())} words, "
          f"{len(section_ids)} sections"
          + (f", {n_fallback} fell back to template" if n_fallback else "")
          + ")")


if __name__ == "__main__":
    main()
