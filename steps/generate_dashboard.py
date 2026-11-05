#!/usr/bin/env python3
"""Generate a Bird's Eye Review dashboard for a topic.

A Bird's Eye Review is a DASHBOARD covering all papers on a topic, where the
end-user decides how to filter. A pooled meta-analysis is an optional extra that
many topics genuinely cannot support: a large, heterogeneous evidence base can
make a rich dashboard precisely because it can't be pooled.

Dashboards for different topics are near-identical copies of one template, so
this decides which panels the data supports instead of hand-building each page.
The per-topic knobs are small and enumerated in ``TopicSpec``.

What this writes: ``dashboard.manifest.json`` next to the bundle in ``--data-dir``
(the pipeline's mode), or — with ``--website-dir`` instead — into
``{website}/app/birds-eye-reviews/{route}/``. The manifest plans:
    page.tsx                 loaders + <h1> + gated buttons + client wrapper
    ResultsClientWrapper.tsx filters -> intervention grid -> year chart -> table
    ResultsTable.tsx         the trial table
    utils.ts, facets.ts      formatting + the chart/table count invariant
    screening/               PRISMA + screening table (when prisma.json exists)

Usage:
    python steps/generate_dashboard.py --config configs/config_x.yaml --data-dir projects/x/dashboard
    python steps/generate_dashboard.py --config configs/config_x.yaml --website-dir ../site --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core import grading as M
from ber_core.dashboard_summary import derive_outcome_types
from ber_core.utils import dashboard_layout_digest, load_config, read_jsonl, slugify

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))


# ── Optional panels ──────────────────────────────────────────────────────────
# The base template is always emitted. Everything below is opt-in via the
# review's config, so a user can add a panel later WITHOUT regenerating the
# whole dashboard, and so a panel whose data is absent is skipped with a stated
# reason rather than rendering empty.

@dataclass(frozen=True)
class Panel:
    key: str
    label: str
    requires: tuple[str, ...]   # data files that must exist for it to render
    why: str                    # what the panel answers, shown in the wizard


PANELS: tuple[Panel, ...] = (
    Panel("country_map", "Trials by country", ("trial_extractions.jsonl",),
          "Where the research is happening — a headline Bird's Eye question."),
    Panel("year_chart", "Trials by year", ("trial_extractions.jsonl",),
          "Whether the field is growing, flat, or abandoned."),
    Panel("intervention_grid", "Trials by intervention", ("trial_extractions.jsonl",),
          "Every treatment being studied, coloured by result."),
    Panel("design_breakdown", "Study types", ("trial_extractions.jsonl",),
          "RCT vs observational vs preclinical — exposes gaps like 'no RCTs'."),
    Panel("heatmap", "Intervention x outcome landscape", ("trial_extractions.jsonl",),
          "Which treatment/outcome pairs are studied, and which are empty."),
    Panel("blinding_significance", "Blinding vs significance",
          ("trial_extractions.jsonl",),
          "Whether open-label trials report more positive results."),
    # Only prisma.json: the flow diagram renders from it alone, and the export
    # now writes it into every bundle. (trial_screening.csv powers the separate
    # hand-built screening TABLES, not the funnel — requiring it here kept the
    # panel permanently skipped for reviews without that CSV.)
    Panel("prisma", "Screening flow", ("prisma.json",),
          "How the corpus was narrowed, with exclusions and reasons."),
    Panel("forest_plots", "Meta-analysis", ("meta_analysis.json",),
          "Pooled effects — only when the topic supports pooling."),
    # The sections DashboardShell used to render unconditionally, now individually
    # removable from the studio's dashboard editor. Same registry so the editor,
    # the manifest, and the skip-reason machinery treat every section alike.
    Panel("feasibility", "Meta-analysis feasibility", ("trial_extractions.jsonl",),
          "Whether pooling is honest for this topic, and what blocks it."),
    Panel("filter_design", "Study design filter", ("trial_extractions.jsonl",),
          "Narrow every chart and the table to one study design."),
    Panel("filter_rob", "Risk of bias filter", ("trial_extractions.jsonl",),
          "Narrow to low/high risk-of-bias trials."),
    Panel("filter_country", "Country filter", ("trial_extractions.jsonl",),
          "Narrow to trials run in one country."),
    Panel("filter_outcome_type", "Outcome type filter", ("trial_extractions.jsonl",),
          "Narrow to one outcome type."),
    Panel("rob_breakdown", "Risk of bias breakdown", ("trial_extractions.jsonl",),
          "How much of the evidence is trustworthy at a glance."),
    Panel("outcome_domains", "Outcome domains", ("trial_extractions.jsonl",),
          "Which outcomes the field actually measures."),
    Panel("results_table", "Trials table", ("trial_extractions.jsonl",),
          "Every included trial, sortable and searchable."),
)

PANELS_BY_KEY = {p.key: p for p in PANELS}

# Emitted for every dashboard unless the config says otherwise. Reproduces the
# pre-editor DashboardShell exactly (every section it rendered unconditionally,
# plus the three historically manifest-gated charts). `prisma` has no shell
# renderer yet but stays for manifest/publish-preflight parity.
DEFAULT_PANELS = (
    "feasibility",
    "filter_design", "filter_rob", "filter_country", "filter_outcome_type",
    "intervention_grid", "year_chart",
    "design_breakdown", "rob_breakdown", "country_map", "outcome_domains",
    "results_table",
    "prisma",
)

# Results-table columns when the config doesn't choose. Keys must match the
# RESULT_COLUMNS registry (and DEFAULT_COLUMN_KEYS) in
# packages/dashboard-ui/src/ResultsTable.tsx.
DEFAULT_COLUMNS = ("year", "study", "design", "organism", "intervention",
                   "comparator", "primary_outcome", "n", "follow_up", "rob",
                   "p", "result")


@dataclass
class TopicSpec:
    """Everything that differs between two generated dashboards."""

    slug: str                       # data dir name, e.g. "nattokinase_for_..."
    route: str                      # URL segment, e.g. "nattokinase"
    title: str                      # <h1>
    description: str                # <meta name=description>
    intervention_noun: str = "Intervention"   # or "Ingredient"
    wide_min_trials: int = 10
    outcome_types: list[dict] = field(default_factory=list)
    panels: list[str] = field(default_factory=lambda: list(DEFAULT_PANELS))
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    has_meta_analysis: bool = False


def spec_from_config(cfg: dict, data_dir: Path) -> TopicSpec:
    """Derive the spec from a review config, defaulting from the data itself."""
    topic = (cfg.get("topic") or {}).get("name") or "Review"
    slug = slugify(topic)
    dash = cfg.get("dashboard") or {}

    # `is not None`, not truthiness: an explicit empty list is a user who turned
    # everything off, which must not snap back to the defaults.
    raw_panels = dash.get("panels")
    panels = list(raw_panels) if raw_panels is not None else list(DEFAULT_PANELS)
    raw_columns = dash.get("columns")
    columns = (list(raw_columns) if raw_columns is not None
               else list(DEFAULT_COLUMNS))

    return TopicSpec(
        slug=slug,
        route=dash.get("route") or slug.replace("_", "-"),
        title=dash.get("title") or f"{topic} — Human Clinical Trials",
        description=dash.get("description")
        or f"Extracted outcomes from human clinical trials of {topic}.",
        intervention_noun=dash.get("intervention_noun", "Intervention"),
        wide_min_trials=int(dash.get("wide_min_trials", 10)),
        outcome_types=dash.get("outcome_types") or [],
        panels=panels,
        columns=columns,
        has_meta_analysis=(data_dir / "meta_analysis.json").exists(),
    )


def maybe_auto_append_forest(spec: TopicSpec, cfg: dict, records: list[dict],
                             data_dir: Path) -> None:
    """Offer pooling when the data supports it AND the file exists — but ONLY
    while the user has not taken ownership of the panel list. Once
    ``dashboard.panels`` exists in config, a missing ``forest_plots`` is a
    deliberate removal and must stay removed.

    Feasibility decides; file existence is only a precondition. (Long Covid has
    the file and correctly gets no forest plot; restless legs has both and does.)
    """
    if (cfg.get("dashboard") or {}).get("panels") is not None:
        return
    if "forest_plots" in spec.panels:
        return
    probe = M.assess_poolability(records, policy=cfg.get("meta_analysis"), min_trials=(cfg.get("meta_analysis") or {}).get("min_trials", 2))
    if probe["overall"] == "supported" and (data_dir / "meta_analysis.json").exists():
        spec.panels.append("forest_plots")


def resolve_panels(spec: TopicSpec, data_dir: Path,
                   feasibility: dict) -> tuple[list[dict], list[dict]]:
    """(rendered, skipped) — a skipped panel always carries a reason.

    A forest plot is gated on the measured feasibility VERDICT, not on whether
    ``meta_analysis.json`` happens to sit on disk. Long Covid is the case that
    matters: it has a 946 KB meta_analysis.json and deliberately no
    meta-analysis route, because 402 trials spanning 766 instruments in one
    domain would pool into an estimate in no meaningful unit. Offering the
    panel because the file exists would resurrect exactly that mistake.
    """
    rendered, skipped = [], []
    for key in spec.panels:
        p = PANELS_BY_KEY.get(key)
        if p is None:
            skipped.append({"key": key, "reason": "unknown panel"})
            continue
        missing = [f for f in p.requires if not (data_dir / f).exists()]
        if missing:
            skipped.append({"key": key, "label": p.label,
                            "reason": f"needs {', '.join(missing)}"})
            continue
        if key == "forest_plots" and feasibility["overall"] != "supported":
            skipped.append({
                "key": key, "label": p.label,
                "reason": f"pooling is {feasibility['overall']} for this topic — "
                          f"{feasibility['recommendation']}"})
            continue
        if key == "forest_plots":
            from ber_core.scientific import RULES_VERSION
            try:
                meta_version = json.loads((data_dir / "meta_analysis.json").read_text(encoding="utf-8")).get("rules_version")
            except (ValueError, OSError):
                meta_version = None
            if meta_version != RULES_VERSION:
                skipped.append({"key": key, "label": p.label, "reason": "Historical meta-analysis requires scientific validation and rebuild"})
                continue
        rendered.append({"key": key, "label": p.label})
    return rendered, skipped


def build_manifest(spec: TopicSpec, data_dir: Path, records: list[dict],
                   dashboard_cfg: dict | None = None, analysis_cfg: dict | None = None) -> dict:
    """The generation plan — written alongside the dashboard so a later run (or
    the wizard) can see exactly what was emitted and what was skipped, and why."""
    feasibility = M.assess_poolability(records, policy=analysis_cfg, min_trials=(analysis_cfg or {}).get("min_trials", 2))
    rendered, skipped = resolve_panels(spec, data_dir, feasibility)
    return {
        "slug": spec.slug,
        "route": spec.route,
        "title": spec.title,
        "intervention_noun": spec.intervention_noun,
        "wide_min_trials": spec.wide_min_trials,
        "outcome_types": spec.outcome_types,
        # The layout contract for the studio's dashboard editor. `layout_version`
        # gates the renderer's strict panel gating (absent = legacy manifest);
        # `layout_digest` lets bundle_state detect a layout edited after this
        # write; `panels_requested` is the PRE-resolve list — the editor seeds
        # its config from this, never from panels_rendered, so a panel skipped
        # for missing data isn't silently dropped on first edit.
        "layout_version": 2,
        "layout_digest": dashboard_layout_digest(dashboard_cfg or {}),
        "panels_requested": list(spec.panels),
        "results_columns": list(spec.columns),
        "panels_rendered": rendered,
        "panels_skipped": skipped,
        "available_panels": [
            {"key": p.key, "label": p.label, "why": p.why,
             "requires": list(p.requires)}
            for p in PANELS if p.key not in spec.panels
        ],
        # The honesty numbers. The dashboard renders THESE — the same numbers a
        # stage checkpoint would gate on — so the two can never disagree.
        "feasibility": {
            "overall": feasibility["overall"],
            "n_papers": feasibility["n_papers"],
            "n_with_contrast": feasibility["n_with_contrast"],
            "n_outcomes": feasibility["n_outcomes"],
            "n_pooled": feasibility["n_pooled"],
            "recommendation": feasibility["recommendation"],
            "domains": feasibility["domains"],
            "blockers": feasibility["blockers"],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--data-dir", default=None,
                    help="Read the exported bundle from here (a review's own "
                         "workspace) instead of from under --website-dir.")
    ap.add_argument("--website-dir", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the manifest without writing any files")
    args = ap.parse_args()

    cfg = load_config(args.config)
    topic = (cfg.get("topic") or {}).get("name") or "default"
    slug = slugify(topic)
    website_dir = args.website_dir or cfg.get("website_dir")
    if not args.data_dir and not website_dir:
        ap.error("pass --data-dir (a locally built bundle) or --website-dir")
    website = Path(website_dir) if website_dir else None
    # `--data-dir` points at a locally-built bundle (what the pipeline passes).
    # Without it we read the conventional path under a website checkout.
    data_dir = (Path(args.data_dir) if args.data_dir
                else website / "data" / "birds_eye_reviews" / slug)

    # An absent feed is a normal state, not an error: a review that has not yet
    # reached the build step simply has no dashboard to resolve panels for. It
    # used to hard-error telling the user to publish first, which made previewing
    # impossible without publishing.
    feed = data_dir / "trial_extractions.jsonl"
    if not feed.exists():
        print(f"No dashboard bundle at {feed} — nothing to resolve yet.\n"
              f"Run the bundle build step first.", file=sys.stderr)
        return 0

    # The published feed, written atomically by the export: a malformed line is
    # corruption, and resolving panels over a silently smaller set would be
    # wrong — so it raises, as the export does on the same file.
    records = read_jsonl(feed, on_error="raise")
    spec = spec_from_config(cfg, data_dir)
    if not spec.outcome_types:
        spec.outcome_types = derive_outcome_types(records)

    maybe_auto_append_forest(spec, cfg, records, data_dir)

    manifest = build_manifest(spec, data_dir, records,
                              dashboard_cfg=cfg.get("dashboard") or {}, analysis_cfg=cfg.get("meta_analysis"))
    fz = manifest["feasibility"]

    print(f"Dashboard plan for {spec.title!r}", file=sys.stderr)
    print(f"  route:    /birds-eye-reviews/{spec.route}", file=sys.stderr)
    print(f"  data:     {data_dir}", file=sys.stderr)
    print(f"  trials:   {len(records)}", file=sys.stderr)
    print(f"  panels:   {', '.join(p['key'] for p in manifest['panels_rendered'])}",
          file=sys.stderr)
    for s in manifest["panels_skipped"]:
        print(f"    skipped {s['key']}: {s['reason']}", file=sys.stderr)
    print(f"  pooling:  {fz['overall']} "
          f"({fz['n_with_contrast']}/{fz['n_papers']} papers with a contrast)",
          file=sys.stderr)
    print(f"            {fz['recommendation']}", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        print("\nDry run; no files written.", file=sys.stderr)
        return 0

    # The manifest belongs NEXT TO THE DATA it describes, so a bundle carries its
    # own generation plan and can be previewed (or later published) as one unit.
    out_dir = data_dir if args.data_dir else website / "app" / "birds-eye-reviews" / spec.route
    out_dir.mkdir(parents=True, exist_ok=True)
    from ber_core.storage import atomic_text
    atomic_text(out_dir / "dashboard.manifest.json", json.dumps(manifest, indent=2))
    print(f"\nWrote {out_dir / 'dashboard.manifest.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
