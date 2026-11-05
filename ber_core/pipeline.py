"""Canonical bird's-eye-review pipeline definition.

Single source of truth for the *ordered* sequence of steps that takes a topic
from "search" all the way to a dashboard bundle built in the project's own
workspace, followed by an optional INSPECT-SR manuscript. Publishing to the public
website is a separate, permission-gated action, never the tail of a run. Both the CLI
driver (``run_pipeline.py``) and the studio's job runner build their work from
:func:`canonical_steps`, so the two never drift.

This module uses UI-independent ``ber_core.projects`` for project paths.
``canonical_steps`` defines public step IDs; ``execution_steps`` expands the
shared prerequisites used by CLI and Studio.

Why the sequence is more than a list of scripts:
  * Pass 2 runs twice — once for RCTs, once for observational studies — writing
    two separate files.
  * PDF acquisition (5/6/7) and the full-text eligibility screen + staging
    (10/11) are prerequisites, not canonical steps: ``execution_steps`` inserts
    them before pass 2 (the studio runs them as background jobs).
  * The bundle build (step 17, ``export_dashboard.py``) unions the two
    extraction files into the single feed the dashboard reads.
  * QC (``qc_report.py``) is a standalone diagnostic, not part of the run.
  * Config-declared ``extra_steps`` run before the bundle build.
  * Each Step declares ``produces`` (its output artifact) so run_pipeline.py can
    verify a step actually wrote output before advancing to the next.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Where the pipeline step scripts live. ``Step.script`` holds a bare filename
#: resolved against this (extra steps carry an absolute path instead).
STEPS_DIR = REPO_ROOT / "steps"
from ber_core import projects as proj_lib  # noqa: E402


@dataclass
class Step:
    """One pipeline step: a script invocation with a stable id and label."""
    id: str          # stable identifier, e.g. "9_pass2_observational"
    num: int         # pipeline step number (for --from/--to/--only selection)
    label: str       # human-readable description
    script: str      # script filename in steps/, e.g. "extract_pass2.py" (or an absolute path)
    args: list[str]  # argv after the script name (incl. any subcommand)
    # Expected output artifact path(s) this step writes. Used by run_pipeline.py to
    # VERIFY the step actually produced something before advancing — the hook a
    # future AI orchestrator drives ("check each output before the next step").
    # Absolute paths, or empty when a step has no single checkable file artifact
    # (e.g. directory-staging steps). A "<dir>/" entry means "directory non-empty".
    produces: list[str] = field(default_factory=list)
    allow_empty_outputs: bool = False
    optional_outputs: bool = False

    @property
    def script_path(self) -> str:
        """Absolute path of the script to execute."""
        return script_path(self.script)

    @property
    def command(self) -> list[str]:
        return [self.script_path, *self.args]


def script_path(script: str | Path) -> str:
    """Resolve a step script: absolute paths pass through, bare names live in steps/."""
    p = Path(script)
    return str(p if p.is_absolute() else STEPS_DIR / p)


def load_project(config_path: str | Path) -> proj_lib.Project:
    """Build a Project from a config path."""
    import yaml
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = (REPO_ROOT / cfg_path).resolve()
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    topic = (cfg.get("topic") or {}).get("name") or cfg_path.stem
    slug = proj_lib.slugify(topic)
    return proj_lib.Project(
        slug=slug, config_path=cfg_path, config=cfg,
        project_dir=proj_lib.PROJECTS_DIR / slug,
    )


def _rel_config(project: proj_lib.Project) -> str:
    try:
        return str(project.config_path.relative_to(proj_lib.REPO_ROOT))
    except ValueError:
        return str(project.config_path)


def canonical_steps(
    project: proj_lib.Project,
    *,
    since: str | None = None,
    fresh: bool = False,
    include_observational: bool = True,
    generate_meta_analysis: bool = False,
) -> list[Step]:
    """Return the full ordered pipeline for one project.

    ``since`` / ``fresh`` are forwarded to the search step. The pipeline builds
    the dashboard bundle into the project workspace and never writes to the
    public site (publishing is a separate, permission-gated action).
    ``include_observational`` keeps the
    observational pass-2 + merge in the sequence (turn off for
    RCT-only topics). ``generate_meta_analysis`` (off by default) opts the
    export step into forest-plot pooling (meta_analysis.json).
    """
    cfg = _rel_config(project)
    data = project.data_dir
    abstracts = project.abstracts_dir
    classifications = data / "classifications.csv"
    classifications_enriched = data / "classifications_enriched.csv"
    screening = data / "trial_screening.csv"
    # trial_extractions.jsonl (pass-1) is produced by the background pass-1 job now,
    # not a canonical step, so no path binding is needed here.
    pass2 = data / "trial_extractions_pass2.jsonl"
    observational = data / "observational_extractions.jsonl"
    # The design buckets stage_eligible.py writes (it hardcodes these names).
    # Bound explicitly into the pass-2 steps below so the dependency lives in
    # the argv rather than in a `dirs.*` config key nothing ever sets — see the
    # NOTE above the pass-2 steps.
    eligible_rct = project.project_dir / "eligible_rct"
    eligible_observational = project.project_dir / "eligible_observational"
    # Output artifacts used for per-step verification (Step.produces).
    search_dois = data / "search_dois.csv"
    # The pipeline builds the dashboard bundle into the PROJECT'S OWN workspace.
    # It deliberately does not write to the shared public site: publishing is a
    # separate, permission-gated action, not the tail of a run.
    bundle_dir = project.project_dir / "dashboard"
    export_feed = bundle_dir / "trial_extractions.jsonl"

    # Step 1 — search (incremental-aware).
    search_args = ["--config", cfg, "--parallel"]
    if fresh:
        search_args.append("--fresh")
    if since:
        search_args += ["--since", since]

    # Step 9 enrich (screening): metadata is collected AFTER filtering — only for
    # screened papers, which is exactly what the dashboard's screening table
    # displays. Reuse the existing enriched cache so we only hit the API for
    # papers not already enriched. NOTE: we deliberately DROPPED the old
    # pre-filter step-4 enrich of all of classifications.csv — nothing on the
    # dashboards reads that file's metadata, and it was ~9h of fetches over the
    # (mostly irrelevant) full corpus. Trial-table biblio is backfilled at export.
    screening_enrich_args = ["--config", cfg, "--input", str(screening),
                             "--prefix", "paper_"]
    if classifications_enriched.exists():
        screening_enrich_args += ["--local-cache", str(classifications_enriched)]

    # NOTE on what is NOT here:
    #   * Number 4 (a pre-filter enrich of the whole corpus) is intentionally
    #     unused — metadata is collected after filtering (step 9) and backfilled
    #     at export.
    #   * Numbers 5-7 and 10-11 are the prerequisites execution_steps() inserts
    #     before pass 2; they are not canonical steps.
    #   * QC (scripts/qc_report.py) is a read-only standalone report.
    #   Step numbers keep their historical values; they are selectors, not run
    #   order. Ids are the stable identity.
    steps: list[Step] = [
        Step("1_search", 1,
             "Search papers (incremental)" if (since or fresh) else "Search papers",
             "search_papers.py", search_args, produces=[str(search_dois)]),
        Step("2_fetch_abstracts", 2, "Fetch abstracts",
             "fetch_abstracts.py", ["--config", cfg],
             produces=[str(abstracts) + "/"]),
        # ONE merged initial screen on abstracts: relevance + treatment + article
        # type + keep/drop (is_excluded) + an overall certainty, in a single haiku
        # call (~half the cost of the old relevance+type split). Resumable (skips
        # already-classified DOIs). Writes classifications.csv with the merged
        # columns that download/screen/dashboard already read (additive).
        Step("3_classify_quick", 3, "Screen abstracts (relevance + type + exclude)",
             "classify.py", ["quick", "--input", str(abstracts),
                               "--output", str(classifications), "--config", cfg],
             produces=[str(classifications)]),
        # NOTE: PDF download (5) + convert (6) are NO LONGER canonical steps. Full
        # text is fetched by a BACKGROUND job (server: kind="pdf" on the ber:pdf
        # queue) triggered for screened-in papers + the escalated (low-certainty /
        # thin-abstract) subset — extraction needs full text, but it must not gate
        # the wizard. The scripts (download_pdfs.py / convert_pdfs.py) are
        # unchanged and still run standalone.
        #
        # The full-text screen still produces trial_screening.csv (dashboard/PRISMA,
        # stage_eligible, enrich all read it). It reads whatever converted text the
        # background fetch has produced, falling back to the abstract per paper
        # (cmd_screen's built-in abstract_only path) — so it no longer waits on a
        # download stage.
        Step("3_classify_screen", 8, "Screen for treatment trials",
             "classify.py", ["screen", "--output", str(screening), "--config", cfg],
             produces=[str(screening)]),
        Step("4_enrich_screening", 9, "Enrich screening metadata (paper_* columns)",
             "enrich_metadata.py", screening_enrich_args, produces=[str(screening)]),
        # NOTE: pass-1 eligibility (extract_pass1) + stage_eligible are NO LONGER
        # canonical steps. Like the PDF fetch above, they run as a BACKGROUND job
        # (server: kind="fulltext_screen" on ber:default) auto-fired for screened-in papers as
        # their full text becomes ready — a cheap full-text pass that decides which
        # papers are real controlled trials worth the expensive pass-2, without
        # gating the wizard on a separate "Eligibility screen" stage the user steps
        # through. The scripts are unchanged (extract_pass1 processes whatever is
        # converted, resumable; stage_eligible routes eligible + force-included DOIs
        # into the eligible_rct/ + eligible_observational/ buckets pass-2 consumes,
        # honoring user_inclusions.json / user_exclusions.json). Pass-2 below depends
        # on those buckets, so it must not run until the pass-1 job has completed.
        #
        # That dependency is ENFORCED, not just documented: the pass-2 steps bind
        # `--input <bucket>` explicitly, and extract_pass2.py hard-errors when the
        # bucket is absent. It used to be expressed only through the optional
        # `dirs.eligible_trials` config key — which nothing ever sets — so pass 2
        # silently fell back to converted/ and extracted the ENTIRE corpus at ~25x
        # the cost, laundering pass-1-ineligible papers into the evidence table and
        # the meta-analysis pool. Do not reintroduce a converted/ fallback here.
        Step("9_pass2_rct", 12, "Extract pass 2 — RCTs",
             "extract_pass2.py",
             ["--config", cfg, "--design-type", "rct",
              "--input", str(eligible_rct)],
             produces=[str(pass2)]),
    ]

    if include_observational:
        steps.append(
            Step("9_pass2_observational", 13, "Extract pass 2 — observational",
                 "extract_pass2.py",
                 ["--config", cfg, "--design-type", "observational",
                  "--input", str(eligible_observational)],
                 produces=[str(observational)]))

    # Canonicalize free-text intervention names (raw -> canonical map) so the
    # dashboard can group naming variants. Resumable + a clean no-op for topics
    # without free-text intervention arms (e.g. antiviral/RLS, which use
    # agent_canonical), so it is safe in every project's run. No single verified
    # artifact (the map only appears where interventions exist), hence no produces.
    steps.append(
        Step("12_canonicalize_interventions", 16, "Canonicalize intervention names",
             "canonicalize_interventions.py", ["--config", cfg]))

    # Re-categorize interventions stuck in the catch-all "other" category (depends on
    # the canonical map above). Same safety profile: resumable, no-op without
    # interventions, no single verified artifact.
    steps.append(
        Step("categorize_interventions", 16, "Re-categorize 'other' interventions",
             "categorize_interventions.py", ["--config", cfg]))

    # Classify each study's indication (treatment / prevention / exclude) so the
    # export keeps the feed treatment-only and routes prevention out. Same safety
    # profile: resumable, no-op without extracted records, no verified artifact.
    steps.append(
        Step("classify_intervention_indication", 16, "Classify treatment vs prevention",
             "classify_intervention_indication.py", ["--config", cfg]))

    # Topic-specific derivations declared in the config (``extra_steps:``). They
    # run after the taxonomy tidy-up and BEFORE the export pools anything, since
    # they typically write trial-level ``derived`` fields that meta-analysis
    # ``subgroup_by`` / ``regress_on`` resolve against. Every other project's run
    # is unchanged. Step IDs carry the ``extra:`` prefix, which the studio's
    # stage registry assigns to the Extract stage.
    steps.extend(extra_steps(project, cfg))

    # --no-metadata-fetch: the studio dumps enriched biblio from the DB to the
    # local cache CSV before this step runs (runner.execute_pipeline), so the
    # network backfill is redundant. Skipping it turns a multi-minute serial
    # Crossref grind (one fetch per untitled record) into an instant local read.
    # Standalone CLI callers that WANT the network fetch drop this flag.
    export_args = ["--config", cfg, "--out-dir", str(bundle_dir), "--no-metadata-fetch"]
    if not include_observational:
        export_args.append("--no-observational")
    if generate_meta_analysis:
        export_args.append("--meta-analysis")
    # The information-loss guard exists to stop a PUBLISHED feed silently losing
    # studies (it once caught a 511-study drop). This step builds the owner's own
    # LOCAL preview, where a shrink is usually the intended result of a cleanup —
    # e.g. quarantining wrongly-extracted papers legitimately took one review from
    # 30 records to 13, and the guard then wedged the export stage with no way to
    # clear it from the UI. Publishing keeps the guard: `publish_deploy.deploy()`
    # re-checks the shrink and requires an explicit `allow_shrink` approval.
    export_args.append("--allow-shrink")
    steps.append(
        Step("13_build_bundle", 17, "Build dashboard bundle (local)",
             "export_dashboard.py", export_args, produces=[str(export_feed)], allow_empty_outputs=True))

    # Resolve which panels the data actually supports, and write the manifest
    # next to the bundle. Reads the bundle just built, so it never needs the
    # public site to exist.
    steps.append(
        Step("16_generate_dashboard", 18, "Resolve dashboard panels",
             "generate_dashboard.py",
             ["--config", cfg, "--data-dir", str(bundle_dir)],
             produces=[str(bundle_dir / "dashboard.manifest.json")]))

    # Explicit opt-in: the end-to-end integrity assessment spends model time.
    # It reads original screening/extractions and produces a local manuscript.
    if (project.config.get("systematic_review") or {}).get("enabled"):
        steps.append(Step("17_systematic_review", 19,
                          "Create systematic review with INSPECT-SR (Terra)",
                          "systematic_review.py", ["--config", cfg],
                          produces=[str(project.project_dir / "systematic_review" / "systematic_review.md")]))

    return steps


EXTRA_STEP_PREFIX = "extra:"


def extra_steps(project: proj_lib.Project, cfg: str | None = None) -> list[Step]:
    """Build the config-declared ``extra_steps`` (topic-specific scripts).

    Each entry is ``{id, script, label?, args?}``. ``script`` is resolved relative
    to the config file's directory (so a review kept outside this repo can ship
    its own derivation script beside its config); ``--config <cfg>`` is always
    passed first.
    """
    cfg = cfg or _rel_config(project)
    out: list[Step] = []
    for entry in project.config.get("extra_steps") or []:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("script"):
            raise ValueError(f"extra_steps entry needs 'id' and 'script': {entry!r}")
        script = Path(entry["script"]).expanduser()
        if not script.is_absolute():
            script = (project.config_path.parent / script).resolve()
        if not script.exists():
            raise FileNotFoundError(f"extra_steps script not found: {script}")
        out.append(Step(
            f"{EXTRA_STEP_PREFIX}{entry['id']}", 16,
            str(entry.get("label") or entry["id"]),
            str(script), ["--config", cfg, *[str(a) for a in entry.get("args") or []]]))
    return out


def select_steps(steps: list[Step], *, from_num: int | None = None,
                 to_num: int | None = None, only: list[int] | None = None) -> list[Step]:
    """Filter steps by --from / --to / --only (by step number).

    CAUTION: an EMPTY ``only`` list is falsy and therefore means "no filter", so
    it selects EVERY step. Callers computing ``only`` programmatically (e.g. from
    UI checkboxes) must guard against the empty case, or use
    :func:`select_step_ids`, which has no such trap.
    """
    if only:
        return [s for s in steps if s.num in set(only)]
    out = steps
    if from_num is not None:
        out = [s for s in out if s.num >= from_num]
    if to_num is not None:
        out = [s for s in out if s.num <= to_num]
    return out


def select_step_ids(steps: list[Step], ids: Iterable[str]) -> list[Step]:
    """Select steps by stable ``Step.id``, preserving canonical order.

    Prefer this over :func:`select_steps` for programmatic selection: ``Step.num``
    is a gappy legacy CLI selector that several steps SHARE (num 3 covers
    relevance+type; num 16 covers 3-4 steps), so it cannot identify one step.
    Unlike ``select_steps``, an empty ``ids`` selects NOTHING — which is what a
    caller that resolved zero steps actually means.
    """
    wanted = set(ids)
    return [s for s in steps if s.id in wanted]


def acquisition_steps(project, *, papers_dir=None, only_dois_path=None,
                      convert_options=()):
    """Prerequisite tasks, independent of wizard page numbering."""
    from ber_core import paper_store
    root = papers_dir or paper_store.papers_root(project.project_dir, config=project.config)
    cfg = _rel_config(project)
    download = ["--config", cfg, "--input", str(project.data_dir / "classifications.csv"),
                "--output-dir", str(root)]
    if only_dois_path:
        download += ["--only-dois", str(only_dois_path)]
    return [
        Step("5_download_pdfs", 5, "Download full-text PDFs", "download_pdfs.py",
             download, produces=[str(root) + "/"], allow_empty_outputs=True),
        Step("6_convert_pdfs", 6, "Convert PDFs to text", "convert_pdfs.py",
             ["--config", cfg, *convert_options, "--papers-dir", str(root)],
             produces=[str(root) + "/"], allow_empty_outputs=True),
        Step("7_install_structured", 7, "Install structured full text", "install_structured.py",
             ["--config", cfg, "--execute", "--papers-dir", str(root)]),
    ]


#: How a review's pass-2 input buckets (``eligible_rct/``, ``eligible_observational/``)
#: are built. ``pass1`` (default): the full-text eligibility screen
#: (extract_pass1.py) decides, and stage_eligible.py rebuilds the buckets from
#: its verdicts. ``manual``: the buckets are curated by hand or by a review's own
#: staging script, and the pipeline must never touch them — stage_eligible runs
#: with ``--clean`` and would wipe them.
STAGING_MODES = ("pass1", "manual")


def staging_mode(config: dict) -> str:
    """The config's ``staging`` key, validated (default ``pass1``)."""
    mode = str((config or {}).get("staging") or "pass1").strip().lower()
    if mode not in STAGING_MODES:
        raise ValueError(f"config key 'staging' must be one of {STAGING_MODES}, not {mode!r}")
    return mode


def eligibility_steps(project):
    """Pass 1 + staging — or nothing, for a review with ``staging: manual``."""
    if staging_mode(project.config) == "manual":
        return []
    cfg = _rel_config(project)
    return [
        Step("8_extract_pass1", 10, "Full-text eligibility screen", "extract_pass1.py",
             ["--config", cfg], produces=[str(project.data_dir / "trial_extractions.jsonl")],
             allow_empty_outputs=True, optional_outputs=True),
        Step("stage_eligible", 11, "Stage eligible papers", "stage_eligible.py",
             ["--config", cfg, "--execute", "--clean"]),
    ]


def execution_steps(project, selected, **acquisition_options):
    """Expand extraction prerequisites once, retaining stable public step IDs.

    Selection happens first: exporting alone does not unexpectedly acquire or
    extract papers. An extraction request always refreshes its prerequisite
    artifacts through the same resumable commands, in CLI and Studio.
    """
    prerequisites = acquisition_steps(project, **acquisition_options) + eligibility_steps(project)
    out, inserted = [], False
    selected_ids = {s.id for s in selected}
    for step in selected:
        if step.id in {"9_pass2_rct", "9_pass2_observational"} and not inserted:
            out.extend(s for s in prerequisites if s.id not in selected_ids)
            inserted = True
        out.append(step)
    return out


def _matches(step: Step, token: str) -> bool:
    return token == step.id or (token.isdigit() and int(token) == step.num)


def select_run(project, canonical: list[Step], *, start: str | None = None,
               stop: str | None = None, only: Iterable[str] | None = None) -> list[Step]:
    """The CLI's step selection, over the full run order (prerequisites included).

    Selectors are step ids or step numbers. ``only`` picks matching steps;
    ``start``/``stop`` cut the run order from the first step matching ``start``
    to the last step matching ``stop``. Prerequisites can be selected directly
    (``--only 6`` converts PDFs), and selecting a pass-2 step still brings in
    any prerequisite it needs, placed in run order.
    """
    full = execution_steps(project, canonical)
    if only is not None:
        tokens = [t for t in only if t]
        chosen = [s for s in full if any(_matches(s, t) for t in tokens)]
    else:
        lo, hi = 0, len(full)
        if start is not None:
            hits = [i for i, s in enumerate(full) if _matches(s, start)]
            lo = hits[0] if hits else len(full)
        if stop is not None:
            hits = [i for i, s in enumerate(full) if _matches(s, stop)]
            hi = hits[-1] + 1 if hits else 0
        chosen = full[lo:hi]
    wanted = {s.id for s in execution_steps(project, chosen)}
    return [s for s in full if s.id in wanted]
