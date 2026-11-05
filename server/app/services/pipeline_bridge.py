"""The ONLY module that imports the pipeline package (pipeline.py / ber_core).

Multi-tenant rule: the long-running server serves many reviews with different
workspaces, so it MUST NOT rely on the import-time ``BER_PROJECTS_DIR`` global.
Every Project is built with an EXPLICIT per-review ``project_dir`` pointing at
the review's own workspace.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import yaml

from ..config import settings
from ._pipeline_path import REPO_ROOT  # side effect: puts the pipeline on sys.path

from ber_core.projects import (  # noqa: E402
    Project,
    derive_pipeline_state,
    slugify,
)
from ber_core import paper_store  # noqa: E402  (repo root, via _pipeline_path)

logger = logging.getLogger(__name__)


def extra_steps_allowed() -> bool:
    """Whether a workspace config's ``extra_steps`` may run (operator opt-in,
    never under PUBLIC_MODE). See ``settings.ALLOW_CONFIG_EXTRA_STEPS``."""
    return bool(settings.ALLOW_CONFIG_EXTRA_STEPS) and not settings.PUBLIC_MODE


def _load_config(workspace: Path) -> dict:
    """The workspace's config.yaml, as the server lets the pipeline see it.

    ``extra_steps`` is STRIPPED unless the operator enabled it: the pipeline
    turns each entry into a script it runs as the server user, so honoring it
    from a workspace that predates the API's write-time refusal (or one edited
    on disk) would still be code execution. Every server path that builds the
    step list goes through here (``build_project`` -> ``canonical_steps_for``).
    """
    cfg_path = workspace / "config.yaml"
    if not cfg_path.exists():
        return {}
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if isinstance(cfg, dict) and cfg.get("extra_steps") and not extra_steps_allowed():
        logger.warning(
            "Ignoring extra_steps in %s: config-declared scripts run only when "
            "ALLOW_CONFIG_EXTRA_STEPS=true (and never under PUBLIC_MODE).", cfg_path)
        cfg = {k: v for k, v in cfg.items() if k != "extra_steps"}
    return cfg


def project_slug(workspace: Path) -> str:
    cfg = _load_config(workspace)
    return slugify((cfg.get("topic") or {}).get("name") or "default")


def build_project(workspace: Path) -> Project:
    cfg = _load_config(workspace)
    slug = slugify((cfg.get("topic") or {}).get("name") or "default")
    return Project(
        slug=slug,
        config_path=workspace / "config.yaml",
        config=cfg,
        # EXPLICIT per-review dir — NOT the global PROJECTS_DIR.
        project_dir=workspace / "projects" / slug,
    )


def papers_root(proj: Project) -> Path:
    """The review's paper store (``papers/<Type>/<stem>/``).

    Honors a user-configured ``base_pdf_dir`` — the studio lets a review point
    its PDF directory anywhere, and the type tree is built underneath it.
    """
    return paper_store.papers_root(proj.project_dir, proj.base_pdf_dir)


def paper_index(project_dir: Path, root: Path | None = None) -> dict[str, Path]:
    """Normalized DOI -> the paper's folder, in one scan.

    The server's single entry point for "where does this paper live". Falls back
    to the legacy ``converted/`` layout, so a review that has not been migrated
    still resolves.
    """
    return paper_store.build_index(project_dir, root=root)


def ensure_paper_dir(proj: Project, doi: str, article_type: str | None = None) -> Path:
    """The paper's folder, created under its type bucket if it has none."""
    return paper_store.ensure_paper_dir(
        proj.project_dir, doi, article_type, root=papers_root(proj))


def _fake_steps_allowed() -> bool:
    """The test hook is an arbitrary-script runner. Honor it only in pytest
    (or with an explicit BER_ALLOW_FAKE_STEPS=1), and never under PUBLIC_MODE
    even if that env var is set — a stray export must not become RCE on a
    hosted instance."""
    if settings.PUBLIC_MODE:
        return False
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("BER_ALLOW_FAKE_STEPS")
    )


def _synthetic_steps_from_env() -> list | None:
    """TEST HOOK. If ``BER_ENGINE_FAKE_STEPS`` points at a JSON list of
    ``{id,num,label,script,args,produces}`` objects, return synthetic ``Step``s
    from it (so a real arq worker can run deterministic no-network steps in
    tests). Production never sets this env var. Returns None when unset.

    Refused unless :func:`_fake_steps_allowed` — a file of ``script``/``args``
    is otherwise arbitrary command execution.
    """
    path = os.environ.get("BER_ENGINE_FAKE_STEPS")
    if not path:
        return None
    if not _fake_steps_allowed():
        logger.error(
            "Refusing BER_ENGINE_FAKE_STEPS=%s: the test step hook is only "
            "honored under pytest (or BER_ALLOW_FAKE_STEPS=1), and never "
            "under PUBLIC_MODE.",
            path,
        )
        return None
    from ber_core import pipeline  # lazy — keep module import light

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        pipeline.Step(
            id=str(entry["id"]),
            num=int(entry["num"]),
            label=str(entry.get("label", entry["id"])),
            script=str(entry["script"]),
            args=list(entry.get("args", [])),
            produces=list(entry.get("produces", [])),
        )
        for entry in raw
    ]


def canonical_steps_for(
    workspace: Path,
    *,
    since: str | None = None,
    fresh: bool = False,
    include_observational: bool | None = None,
    generate_meta_analysis: bool = False,
) -> list:
    """Return the ordered canonical steps for one review's workspace.

    Steps carry absolute workspace paths + ``--config <workspace>/config.yaml``
    (because ``build_project`` points project_dir/config_path at the workspace),
    so the subprocess env's BER_PROJECTS_DIR and the step args agree.
    """
    synthetic = _synthetic_steps_from_env()
    if synthetic is not None:
        return synthetic

    from ber_core import pipeline  # lazy

    proj = build_project(workspace)
    cfg = proj.config or {}
    if include_observational is None:
        include_observational = bool(cfg.get("include_observational", True))
        # An RCT-only meta-analysis config turns the observational pass off.
        designs = (cfg.get("meta_analysis") or {}).get("designs")
        if designs == "rct_only":
            include_observational = False

    # Pooling is a property of the REVIEW, not of one run, so it is read from
    # config exactly like include_observational above. Passing it per-run
    # instead would let a resume silently rebuild the bundle WITHOUT
    # meta_analysis.json (13_build_bundle re-runs on every resume), and would
    # leave single-step runs via execute_step permanently meta-less, since
    # find_step() resolves steps with no options. The kwarg stays so CLI
    # callers keep working; config is the fallback, never an override.
    if not generate_meta_analysis:
        generate_meta_analysis = bool(cfg.get("generate_meta_analysis", False))

    return pipeline.canonical_steps(
        proj,
        since=since,
        fresh=fresh,
        include_observational=include_observational,
        generate_meta_analysis=generate_meta_analysis,
    )


def find_step(workspace: Path, step_id: str, **opts):
    """Return the canonical ``Step`` with this unique id, or None."""
    for step in canonical_steps_for(workspace, **opts):
        if step.id == step_id:
            return step
    return None


def review_state(workspace: Path) -> list[dict]:
    """Return derive_pipeline_state as JSON-ready dicts (Path fields -> str).

    Each row also carries ``canonical_nums``: the canonical step numbers that
    run this row's script (one display row can cover several canonical steps,
    e.g. classify = relevance/type/screen). The UI's "run specific stages"
    checkboxes turn these into the run endpoint's ``only`` list. Rows whose
    script isn't in the standard run (reorganize, QC) get an empty list.
    """
    proj = build_project(workspace)
    nums_by_script: dict[str, list[int]] = {}
    for st in canonical_steps_for(workspace):
        nums = nums_by_script.setdefault(st.script, [])
        if st.num not in nums:
            nums.append(st.num)
    out: list[dict] = []
    for step in derive_pipeline_state(proj):
        d = asdict(step)
        d["output_paths"] = [str(p) for p in step.output_paths]
        d["canonical_nums"] = nums_by_script.get(step.script, [])
        out.append(d)
    return out


def classify_summary(workspace: Path) -> dict:
    """Categorical counts from the classify stage's CSVs, for the studio's
    bar chart. Missing files just yield empty dicts (the UI hides the chart).

    ``relevance.csv`` is the normal source for the relevance breakdown; the
    ``quick`` subcommand instead folds those columns into ``classifications.csv``,
    so fall back to it when relevance.csv is absent.
    """
    proj = build_project(workspace)
    data_dir = proj.data_dir

    def _read(name: str) -> list[dict]:
        path = data_dir / name
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _count(rows: list[dict], col: str) -> dict[str, int]:
        vals = [(r.get(col) or "").strip() for r in rows]
        return dict(Counter(v for v in vals if v))

    classifications = _read("classifications.csv")
    relevance_rows = _read("relevance.csv") or classifications

    return {
        "article_types": _count(classifications, "article_type"),
        "relevance": _count(relevance_rows, "is_relevant"),
        "studies_treatment": _count(relevance_rows, "studies_treatment"),
        "total": len(classifications),
    }


def pregate_preview(workspace: Path, config: dict | None = None) -> dict:
    """How the pre-gate would partition this review's abstracts, without any LLM call.

    ``config`` overrides the on-disk config so the studio can preview unsaved
    settings. Imports ``pregate`` (stdlib-only, shared with classify.py) rather
    than the classifier itself, so the lean server venv needs none of the
    pipeline's heavy dependencies and the preview can never drift from the run.
    """
    from ber_core.pregate import audit_pregate, build_pregate_pattern, pregate_settings

    proj = build_project(workspace)
    cfg = config if config is not None else (proj.config or {})

    abstracts = proj.abstracts_dir
    if not abstracts.is_dir():
        return {"total": 0, "would_send": 0, "term_match": 0, "nonlatin_bypass": 0,
                "would_skip": 0, "no_abstract": 0, "enabled": False,
                "detail": "No abstracts yet — run the abstract-fetch stage first."}

    # Same shape and same file selection as utils.gather_papers (non-recursive,
    # DOI/PMID-prefixed only) so the preview counts exactly what a run would.
    papers = [(p.stem, p, "") for p in sorted(abstracts.glob("*_abstract.md"))
              if p.stem.startswith(("10.", "pmid_"))]

    if not pregate_settings(cfg)["enabled"]:
        return {"total": len(papers), "would_send": len(papers), "term_match": 0,
                "nonlatin_bypass": 0, "would_skip": 0, "no_abstract": 0,
                "enabled": False,
                "detail": "Pre-gate disabled — every paper goes to the model."}

    pattern = build_pregate_pattern(cfg)
    out = audit_pregate(pattern, papers)
    out["enabled"] = pattern is not None
    if pattern is None:
        out["detail"] = ("No pre-gate terms configured, so nothing is skipped. "
                         "Add synonyms or extra terms, or re-enable base terms.")
    return out


def preflight(
    workspace: Path, config: dict | None = None, stage_slug: str | None = None
) -> list[dict]:
    """Config/data problems that would break or silently degrade a run.

    Returns ``{level, code, message}`` dicts — ``error`` should block a run,
    ``warning`` is advisory. These are the checks that turn the silent failure
    modes (empty vocabulary, a download filter matching nothing, a pre-gate that
    discards the corpus) into something visible BEFORE money is spent.

    ``stage_slug`` narrows the config-key checks to the keys THAT stage's scripts
    actually read, so a user is not blocked from running the search because a key
    only pass-2 extraction needs is unset. ``None`` checks everything.
    """
    proj = build_project(workspace)
    cfg = config if config is not None else (proj.config or {})

    # Declarative key checks (empty vocabularies, missing queries). These used to
    # be hand-rolled here for article_types + synonyms only; the schema covers the
    # same ground plus the keys that silently degrade to "other" in pass-2
    # extraction — the most expensive step to get wrong.
    from . import config_schema

    issues: list[dict] = config_schema.validate(cfg, stage_slug)

    # Does the classifier's output match what the full-text FETCH will accept?
    # ADVISORY now (not blocking): PDF download+convert is a BACKGROUND job, no
    # longer a canonical run step, so this can't make a run "fail". But if nothing
    # is downloadable, extraction gets no full text — worth surfacing.
    summary = classify_summary(workspace)
    observed = {t for t, n in (summary.get("article_types") or {}).items()
                if n and t not in ("no_abstract", "parse_error", "")}
    if observed:
        from ber_core.constants import PDF_ELIGIBLE_TYPES  # lazy: REPO_ROOT already on sys.path

        wanted = {str(t).strip() for t in (cfg.get("pdf_types") or PDF_ELIGIBLE_TYPES)}
        if not (observed & wanted):
            issues.append({
                "level": "warning", "code": "no_downloadable_types",
                "message": (
                    f"None of the {summary.get('total', 0)} classified papers have "
                    f"an article type the full-text fetch accepts "
                    f"(found: {', '.join(sorted(observed)[:5])}). No full text will "
                    f"be downloaded, so extraction will have nothing to read. Check "
                    f"`article_types` / `pdf_types`."
                ),
            })

    # Extraction reads ONLY the eligible_*/ buckets stage_eligible.py writes from
    # the pass-1 verdicts. If the full-text screen has not produced a verdict file
    # yet, there is nothing legitimate to extract — block, rather than let the
    # user spend a Sonnet call per paper on whatever happens to be on disk.
    #
    # Scoped to the extract stage ONLY (never the unscoped stage_slug=None call).
    # A brand-new review has no pass-1 file by definition, so raising this in the
    # whole-review check would report a blocking error — and `has_errors` gates
    # the run — before the user has even searched.
    if stage_slug == "extract":
        pass1 = proj.project_dir / "data" / "trial_extractions.jsonl"
        if not pass1.exists():
            issues.append({
                "level": "error", "code": "no_fulltext_verdicts",
                "message": (
                    "The full-text eligibility screen has not run yet, so no papers "
                    "are staged for extraction. It starts automatically as full text "
                    "arrives for screened-in papers — wait for it to finish (or run "
                    "it from the Review page) before extracting."
                ),
            })
        else:
            staged = sum(
                1 for name in ("rct", "observational")
                for _ in (proj.project_dir / f"eligible_{name}").glob("*")
            )
            if not staged:
                issues.append({
                    "level": "warning", "code": "nothing_staged",
                    "message": (
                        "The full-text screen ran but staged 0 papers for extraction, "
                        "so an extract run would do nothing. Papers reach extraction "
                        "by passing the full-text eligibility screen, or via a manual "
                        "Include on the Review page."
                    ),
                })
            # Partial coverage: papers still awaiting their full-text verdict
            # (screen_state "pending" = screened in at abstract level, no pass-1
            # ruling yet) with full text ALREADY on disk. Warning, not error —
            # extraction is resumable, so an eager partial run is legitimate;
            # the user just needs to know these papers won't be in it.
            try:
                from . import evidence

                rows = evidence.build_evidence(workspace).get("rows", [])
                awaiting = sum(
                    1 for r in rows
                    if r.get("screen_state") == "pending" and r.get("has_fulltext")
                )
            except Exception:
                awaiting = 0
            if awaiting:
                issues.append({
                    "level": "warning", "code": "pass1_incomplete",
                    "message": (
                        f"{awaiting} screened-in paper(s) with full text are still "
                        f"awaiting the full-text eligibility screen and will NOT be "
                        f"in this extraction run. Extraction is resumable — you can "
                        f"run it again after the screen finishes to pick them up."
                    ),
                })

    # Pre-gate: warn when it would discard most of the corpus.
    try:
        pv = pregate_preview(workspace, cfg)
    except Exception:
        pv = None
    if pv and pv.get("enabled") and pv.get("total"):
        skipped = pv.get("would_skip", 0)
        frac = skipped / pv["total"]
        if frac > 0.5:
            issues.append({
                "level": "warning", "code": "pregate_skips_most",
                "message": (
                    f"The keyword pre-gate would skip {skipped} of {pv['total']} "
                    f"papers ({frac:.0%}) without an LLM call. If that seems high, "
                    f"check that your topic's own name and common variants are in "
                    f"the synonyms (or add them as pre-gate extra terms), or turn "
                    f"the pre-gate off."
                ),
            })

    return issues


# The classification prompts the studio's pipeline actually runs, with the
# placeholders each MUST retain. A template missing one silently produces a
# useless prompt (e.g. no {abstract} => the model classifies nothing), so saves
# are rejected rather than allowed to fail at run time.
EDITABLE_PROMPTS: dict[str, dict] = {
    "classify_relevance.txt": {
        "label": "Relevance screening",
        "required": ["{topic_name}", "{topic_synonyms_str}",
                     "{not_about_bullets}", "{abstract}"],
    },
    "classify_type.txt": {
        "label": "Article type",
        "required": ["{article_types_list}", "{abstract}"],
    },
    "classify_quick.txt": {
        "label": "Combined relevance + type",
        "required": ["{topic_name}", "{topic_synonyms_str}", "{not_about_bullets}",
                     "{article_types_list}", "{abstract}"],
    },
    "screen_trials.txt": {
        "label": "Trial screening",
        "required": ["{topic_name}", "{topic_description}",
                     "{not_about_bullets}", "{abstract}"],
    },
}


def _override_path(workspace: Path, name: str) -> Path:
    """Where this review's edited copy of *name* lives.

    ``runner.build_step_env`` sets BER_PROMPTS_DIR to ``<workspace>/prompts`` when
    it exists, and ``utils.load_prompt_template`` looks under ``<dir>/<slug>/``
    first — so writing here overrides the shipped template for this review only.
    """
    return workspace / "prompts" / project_slug(workspace) / name


def list_prompts(workspace: Path) -> list[dict]:
    """Every editable prompt: the review's override if present, else the default."""
    out = []
    for name, meta in EDITABLE_PROMPTS.items():
        override = _override_path(workspace, name)
        if override.exists():
            text, is_override = override.read_text(encoding="utf-8"), True
        else:
            shipped = REPO_ROOT / "prompts" / name
            text = shipped.read_text(encoding="utf-8") if shipped.exists() else ""
            is_override = False
        out.append({
            "name": name,
            "label": meta["label"],
            "text": text,
            "is_override": is_override,
            "required_placeholders": list(meta["required"]),
        })
    return out


def missing_placeholders(name: str, text: str) -> list[str]:
    """Required placeholders absent from *text* (empty list = safe to save)."""
    required = EDITABLE_PROMPTS.get(name, {}).get("required", [])
    return [p for p in required if p not in (text or "")]


def save_prompt(workspace: Path, name: str, text: str | None) -> dict:
    """Write (or, for empty text, delete) a per-review prompt override."""
    path = _override_path(workspace, name)
    if text is None or not text.strip():
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return next(p for p in list_prompts(workspace) if p["name"] == name)


def resume_info(workspace: Path) -> dict:
    """Where a run should resume, computed in CANONICAL EXECUTION order.

    The 1–13 display steps (``derive_pipeline_state``) are in filename order,
    which is NOT the execution order (e.g. enrich runs after screening). So we
    walk the canonical steps in order and return the first one whose work isn't
    ``done``. ``resume_from_num`` is the canonical step number to pass as a run's
    ``from_num``; ``resume_display_number``/``resume_label`` describe it for the
    UI. Everything done -> ``all_done=True`` and ``resume_from_num=None``.
    """
    proj = build_project(workspace)
    display = derive_pipeline_state(proj)
    status_by_script: dict[str, str] = {}
    name_by_script: dict[str, str] = {}
    dispnum_by_script: dict[str, int] = {}
    for s in display:
        status_by_script.setdefault(s.script, s.status)
        name_by_script.setdefault(s.script, s.name)
        dispnum_by_script.setdefault(s.script, s.number)
    any_done = any(s.status == "done" for s in display)

    for st in canonical_steps_for(workspace):
        status = status_by_script.get(st.script)
        if status is None:
            continue  # internal step with no display row (stage_eligible, …)
        if status != "done":
            return {
                "all_done": False,
                "any_done": any_done,
                "resume_from_num": st.num,
                "resume_label": name_by_script.get(st.script) or st.label,
                "resume_display_number": dispnum_by_script.get(st.script),
            }
    return {
        "all_done": True,
        "any_done": any_done,
        "resume_from_num": None,
        "resume_label": None,
        "resume_display_number": None,
    }
