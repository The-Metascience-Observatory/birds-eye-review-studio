"""The wizard stage registry — the single source of truth for stage structure.

WHY A FOURTH IDENTIFIER EXISTS
------------------------------
Three identifier spaces already exist in this codebase and none can name a
wizard stage:

1. ``Step.num`` — a gappy legacy CLI selector for ``--from/--to/--only``. Numbers
   4 and 7 are gaps; num 3 covers TWO steps (relevance + type); num 16 covers
   three-to-four. It cannot identify a single step, let alone a stage.
2. The 13 display rows from ``ber_core.projects.derive_pipeline_state`` — in FILENAME
   order, which is NOT execution order (enrich runs after screening). Good for a
   status table, wrong for a sequential wizard.
3. Script basenames — the join key between the two, but one script serves several
   canonical steps (``classify.py`` runs at 3, 3 and 8).

So stages get a stable ``slug`` (the URL segment) and map to a tuple of
``Step.id`` values, which ARE unique and stable. ``run_step`` already resolves by
``Step.id``, and :func:`pipeline.select_step_ids` selects by it without the
empty-list footgun that ``select_steps(only=[])`` has.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not compute status and it does not store anything. Stage status is
DERIVED at request time from artifacts on disk, job state, and (later) the
approval row. Keeping the registry a pure declaration is what lets the resolved
stage list be recomputed cheaply and stay honest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import pipeline_bridge


# The six papertrace-style phases, in order. A PHASE groups several stages for
# display only — it has NO gate, NO status of its own, and nothing in
# `stage_gates.py` or `resolve_stages` ever reads it. This keeps the phase field
# a pure presentation concern with zero risk to the approval machinery: the gate
# tests never inspect it. The one invariant (enforced by test) is that every
# stage's `phase` is one of these and that they partition the stage list.
PHASES: tuple[tuple[str, str], ...] = (
    ("define", "Define"),
    ("search", "Search"),
    ("screen", "Screen"),
    ("review", "Review"),
    ("extract", "Extract"),
    # Slug stays "synthesize" (permanent — it is in URLs and gate records); the
    # displayed label is "Meta-analysis", which is what the phase actually does.
    ("synthesize", "Meta-analysis"),
    # Slug stays "publish" (permanent), but the flow-chart label reads
    # "View dashboard" — the stage is first about seeing the Bird's Eye Review
    # dashboard; publishing to the public site is an opt-in step within it.
    ("publish", "View dashboard"),
)
PHASE_SLUGS: frozenset[str] = frozenset(p for p, _ in PHASES)


@dataclass(frozen=True)
class StageDef:
    """One wizard stage. Pure declaration — no status, no I/O."""

    slug: str
    """URL segment. Stable forever: it ends up in user bookmarks."""

    title: str

    phase: str = "define"
    """Which of the six PHASES this stage belongs to — display grouping only.

    Presentation-only: gating stays stage-level and nothing in the resolve/gate
    path reads this. Regrouping stages across phases never touches a slug (URLs
    and body-dispatch keys are the slug), so it can't break bookmarks or bodies.
    """

    step_ids: tuple[str, ...] = ()
    # Also claim any canonical step whose id starts with one of these (used for
    # config-declared ``extra:<id>`` steps, whose ids aren't known statically).
    step_id_prefixes: tuple[str, ...] = ()
    """Canonical ``Step.id`` values this stage runs, in execution order.

    A SUPERSET: ids absent from a given review's ``canonical_steps()`` (e.g.
    ``9_pass2_observational`` for an RCT-only review) are filtered out when the
    stage is resolved. Empty means the stage runs no pipeline steps at all —
    it is a settings or curation page.
    """

    optional_steps: tuple[str, ...] = ()
    """Canonical step ids this stage OWNS when they exist, but whose absence
    does not make the stage ``skipped``.

    For opt-in steps (``17_systematic_review`` exists only when
    ``systematic_review.enabled``): listing such a step in ``step_ids`` made a
    stage whose only declared step was opt-in read as skipped — and so hidden
    and unreachable — on every review that had not opted in, even though the
    stage has a page of its own (the report stage's narrative synthesis).
    """

    kind: str = "run"
    """setup | run | curate | review | output — drives which body the page renders."""

    description: str = ""
    """One line explaining what the stage is for, shown in the page header."""

    config_keys: tuple[str, ...] = ()
    # Safe to start WITHOUT the user clicking Run: a pure fetch with no LLM
    # spend and no destructive effect, whose only sane next action is "do it".
    # Deliberately opt-in per stage rather than inferred: a stage that spends
    # money must never auto-start because a heuristic thought it was cheap.
    autorun: bool = False
    """Config paths this stage reads.

    Used later for SCOPED staleness: editing ``meta_analysis.ci_method`` must not
    invalidate an approved search stage. Dotted paths address nested keys.
    """

    hidden: bool = False
    """Not shown in the wizard walkthrough (no page, no step count, no nav).

    For pure background stages that own a canonical step + drive autorun but have
    nothing for the user to look at — the step still runs and its progress shows
    on whatever page the user IS on, but it never appears as a numbered stage.
    Presentation-only: the stage still exists server-side for step ownership and
    gating; only the client hides it. The wizard never lands on a hidden stage.
    """

    requires_approval: bool = True


# Ordered. This IS the wizard's page order, and it follows CANONICAL EXECUTION
# order — deliberately not the filename order of the 13-row status table.
STAGES: tuple[StageDef, ...] = (
    StageDef(
        slug="setup",
        title="Define review",
        phase="define",
        kind="setup",
        description="What the review is about, and what it is not about.",
        # `topic` covers name/synonyms/description/not_about. It is the SOLE
        # owner of topic.* — the search stage reads topic.synonyms too, but two
        # owners would make "which stage does this edit invalidate?" ambiguous,
        # which is the one question config_keys exists to answer.
        config_keys=("topic", "include_observational"),
    ),
    StageDef(
        slug="search",
        title="Find papers",
        phase="search",
        step_ids=("1_search",),
        description="Search the literature databases for candidate papers.",
        config_keys=("search",),
    ),
    StageDef(
        slug="abstracts",
        title="Fetch abstracts",
        phase="search",
        step_ids=("2_fetch_abstracts",),
        description="Retrieve an abstract for every DOI found by the search.",
        # Pure network fetch, no LLM cost, and nothing to configure — so the
        # wizard starts it automatically and does not stop on it. Its progress
        # is shown on whatever page the user is actually working on.
        autorun=True,
        # HIDDEN from the walkthrough: there is nothing for the user to look at
        # here (no settings, no decision, no output to review beyond "did it
        # download"). It still owns step 2_fetch_abstracts and auto-runs; it just is
        # not a numbered stage the user steps through.
        hidden=True,
        # No sign-off to give. Requiring approval would make a background stage
        # block the whole pipeline while the user is on another page.
        requires_approval=False,
    ),
    # ONE screening stage. The old "Screen abstracts" + "Get full texts" + "Screen
    # full texts" trio collapsed into a single initial abstract screen: the merged
    # `quick` step decides relevance / treatment / article-type / keep-drop /
    # certainty from the abstract; the trial-screen + enrich still produce
    # trial_screening.csv (dashboard/PRISMA/eligibility read it), reading whatever
    # full text the BACKGROUND fetch has produced. PDF download+convert is no
    # longer a stage — it's a background job (server kind="pdf"). `pdf_types` moves
    # here (it was owned by the deleted pdfs stage) to keep scoped staleness.
    StageDef(
        slug="classify",
        title="Screen",
        phase="screen",
        step_ids=("3_classify_quick", "3_classify_screen", "4_enrich_screening"),
        # No blurb or step-count details on the screen page — the header stays a
        # bare title. The screening controls below speak for themselves, and the
        # raw "relevance: N | types: N | screened: N" counts were noise.
        description="",
        config_keys=(
            "article_types", "classification", "pdf_types",
            "models.classification", "models.screening",
        ),
    ),
    # The "Review" phase: one human checkpoint over the screened set before the
    # expensive extraction. Slug stays "triage" (stable URL/bookmarks); only the
    # title + phase change. (The old separate "Paper table" stage rendered the
    # SAME CurateStage body — it was removed as a duplicate of this one.)
    StageDef(
        slug="triage",
        title="Review",
        phase="review",
        kind="curate",
    ),
    # HIDDEN, step-less stage: the full-text eligibility screen (pass 1) + staging
    # left the canonical range — it runs as a BACKGROUND job (server
    # kind="fulltext_screen") auto-fired for screened-in papers as their full text
    # arrives, deciding which are real controlled trials worth the expensive
    # pass-2 (reading the methods the abstract screen couldn't). It is NOT a
    # walkthrough step: the user goes straight from Screen to Review while it firms
    # up decisions (papers show as Pending until it rules). The stage stays in the
    # registry only to OWN the pass-1 config keys (so scoped staleness has a home);
    # its `step_ids` are empty because 8_extract_pass1/stage_eligible are no longer
    # canonical. Hidden + no approval gate (a hidden required-approval stage would
    # block the extract stage downstream with no UI to clear it).
    StageDef(
        slug="extract1",
        title="Full-text eligibility screen",
        phase="extract",
        step_ids=(),
        description=(
            "Background full-text pass that decides which screened-in papers are "
            "clinical trials worth the expensive extraction. Runs automatically; "
            "the Review page shows papers as Pending until it rules."
        ),
        config_keys=("extraction_pass1_fields", "models.extraction_pass1"),
        hidden=True,
        requires_approval=False,
    ),
    # The single user-facing extraction stage (slug "extract" → /stages/extract).
    # The cheap eligibility pass (extract1) is hidden + background, so this is just
    # "Extract" to the user. It also owns the taxonomy tidy-up steps
    # (canonicalize/categorize/indication + topic-specific derivations) that run
    # right after pass-2 — the old separate "taxonomy" stage page was redundant
    # with this one.
    StageDef(
        slug="extract",
        title="Extract",
        phase="extract",
        step_ids=(
            "9_pass2_rct",
            "9_pass2_observational",
            "12_canonicalize_interventions",
            "categorize_interventions",
            "classify_intervention_indication",
        ),
        # Config-declared topic-specific derivations (pipeline.extra_steps).
        step_id_prefixes=("extra:",),
        # Bare header — the ExtractStage body's own banner + block toggles explain
        # the stage; a one-line blurb on top was redundant.
        description="",
        config_keys=(
            "extraction_pass2_fields", "extraction_workers", "outcome_domains",
            "models.extraction_pass2", "intervention_categories",
            "models.canonicalize", "custom_extraction_questions",
        ),
    ),
    StageDef(
        slug="meta",
        title="Meta-analysis",
        phase="synthesize",
        kind="review",
        description="Pool comparable trials into effect estimates with GRADE certainty.",
        # `generate_meta_analysis` is the on/off switch the stage's own "Pool
        # these trials" button flips; owning it here means toggling pooling marks
        # THIS stage stale rather than something unrelated. The pipeline reads it
        # when it builds the step list, so the next run picks it up.
        config_keys=("meta_analysis", "generate_meta_analysis"),
        # Optional by design (only runs when generate_meta_analysis is on) — it
        # never blocks Export or anything downstream.
        requires_approval=False,
    ),
    StageDef(
        # Renamed from "export": the URL now matches what the page IS. Slugs are
        # normally permanent (they are stored in StageState.stage and appear in
        # bookmarks), so this rename ships with a one-time migration —
        # `_migrate_renamed_stage_slugs` in db/session.py — that rewrites the
        # stored rows. Without it the stage's approval would silently orphan and
        # read as never-approved.
        slug="view-dashboard",
        title="View dashboard",
        phase="publish",
        step_ids=("13_build_bundle", "16_generate_dashboard"),
        kind="output",
        # No blurb — the ExportStage body's own controls ("View dashboard" button
        # + the opt-in publish toggle) explain the stage.
        description="",
    ),
    StageDef(
        slug="report",
        title="Synthesize review",
        phase="publish",
        # The page's own work — the narrative synthesis — is user-initiated
        # (POST /synthesize-review), not a canonical step. The full INSPECT-SR
        # step exists only when systematic_review.enabled is true, so it is
        # OPTIONAL: owned when present, and its absence does not skip the stage.
        optional_steps=("17_systematic_review",),
        kind="output",
        description="Write a narrative systematic-review document from the "
                    "finished dashboard.",
        config_keys=("synthesis", "models.synthesis", "systematic_review"),
        # Optional output; nothing is downstream, so it never gates.
        requires_approval=False,
    ),
)

_BY_SLUG = {s.slug: s for s in STAGES}

# Config keys with UI but NO owning stage, on purpose. They are read only by
# `classify.py` subcommands (drug_class, study_design, indication) that
# `canonical_steps()` never schedules, so no stage's staleness should depend on
# them. Listed explicitly so "belongs to no stage" reads as a decision rather
# than an oversight — and so a test can assert the set stays closed.
UNOWNED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "drug_class_categories",
        "study_design_categories",
        "organism_categories",
        "indication_categories",
    }
)


def get_stage(slug: str) -> StageDef | None:
    return _BY_SLUG.get(slug)


def phase_list() -> list[dict]:
    """The six phases as ``{slug, label}`` dicts, in order — for the stepper."""
    return [{"slug": slug, "label": label} for slug, label in PHASES]


# The CLI's status details name absolute paths and raw artifact files, which is
# useful in a terminal but leaks the server's filesystem layout into the studio
# UI (e.g. "no PDFs in /home/.../projects/<slug>/pdfs yet"). Reword them for the
# browser. The CLI's own output (ber_core.projects.derive_pipeline_state) is untouched.
# A rewrite to "" suppresses the detail entirely — `_humanize_detail`'s callers
# drop empty strings. Metadata enrichment now runs automatically in the
# background, so "not enriched yet" is a transient internal state the user has no
# reason to see or act on.
_DETAIL_REWRITES = {
    "no *_enriched.csv yet": "",
    "search_dois.csv not yet generated": "search results not generated yet",
    "no classification CSVs found": "no screening results yet",
    "no abstracts/ directory": "no abstracts fetched yet",
}
_ABS_PATH = re.compile(r"\s*/(?:[^\s,|]+)(?:/[^\s,|]+)*")


def _humanize_detail(text: str) -> str:
    """Strip server paths / raw filenames from a status detail before the UI shows it."""
    if not text:
        return ""
    if text in _DETAIL_REWRITES:
        return _DETAIL_REWRITES[text]
    # Phrasings that embed an absolute path — reword the whole clause.
    text = re.sub(r"no PDFs in\s+\S+\s+yet", "no PDFs downloaded yet", text)
    text = re.sub(r"base_pdf_dir missing:.*", "PDF directory not configured", text)
    # "1,234 PDF file(s) under /path" -> "1,234 PDF file(s)".
    text = re.sub(r"\s+under\s+\S+.*$", "", text)
    # Belt-and-suspenders: remove any remaining absolute-path token.
    text = _ABS_PATH.sub("", text)
    return text.strip()


# Stages whose header shows no step-count details — the raw counts read as noise
# rather than signal (e.g. "relevance: 368 | types: 368 | screened: 87").
# Stages whose header stays bare — the per-step "not yet generated (foo.jsonl)"
# detail lines are noise (the user doesn't think in artifact filenames). `classify`
# suppressed its relevance/type counts; `extract` and `export` suppress the
# "not yet generated (trial_extractions.jsonl / dashboard.manifest.json)" lines.
_NO_DETAIL_STAGES: frozenset[str] = frozenset(
    {"classify", "extract", "view-dashboard"})


def _details_for(
    slug: str,
    resolved_ids: list[str],
    steps_by_id: dict,
    detail_by_script: dict[str, str],
) -> list[str]:
    """Human-readable detail for a stage, without leaking a sibling's text.

    The 13-row display table has ONE row per script, so the ``classify.py`` row
    reads "relevance: 556 | types: 556" — text that belongs to the "classify"
    stage. Reusing it for "screen" (which runs the same script as a different
    subcommand) claimed screening had processed 556 papers when it had not run.

    So a script's detail is only borrowed when this stage owns the step that
    actually produced it; otherwise fall back to naming what the stage produces.
    """
    if slug in _NO_DETAIL_STAGES:
        return []
    out: list[str] = []
    for sid in resolved_ids:
        step = steps_by_id[sid]
        status = _step_status(step)
        if status == "done" and step.script in detail_by_script:
            text = _humanize_detail(detail_by_script[step.script])
            if text and text not in out:
                out.append(text)
        elif status in ("pending", "partial"):
            missing = [Path(p.rstrip("/")).name for p in step.produces]
            if missing:
                out.append(f"{step.label}: {'not yet generated' if status == 'pending' else 'partial'} ({', '.join(missing)})")
            else:
                out.append(f"{step.label}: {status}")
    return out


def _step_status(step) -> str:
    """Has THIS canonical step produced its declared outputs?

    Status must be per-STEP, not per-script. One script can back several steps in
    different stages: ``classify.py`` runs as relevance and type (stage
    "classify") and again as screen (stage "screen"), and they all collapse into
    a single ``classify.py`` display row. Keying status by script therefore
    reported "screening is partially done" purely because relevance had run —
    exactly the kind of false signal a human would rubber-stamp.

    ``Step.produces`` is the same contract ``verify_produces`` enforces after a
    run, so this agrees with what the runner considers success. Steps that
    declare no outputs (stage_eligible, the taxonomy steps) are genuinely
    unknowable from the filesystem and report ``n/a``.

    A step whose bound ``--input`` is a STAGED ``eligible_*`` bucket that
    EXISTS AND IS EMPTY is also ``n/a``: staging ran and routed nothing to
    this design (e.g. the observational pass-2 on a corpus with no
    observational papers), so there is no work and no artifact to expect.
    Without this, `include_observational: true` + an all-RCT corpus left the
    extract stage "partial" forever, pinning the wizard's current stage (and the
    timeline's you-are-here marker) on Extract for a review the user had long
    finished. Scoped to ``eligible_*`` dirs on purpose: other steps bind
    ``--input`` too (classify reads abstracts/), and THEIR empty dir means
    "upstream hasn't run", which must stay ``pending``. A MISSING bucket also
    stays ``pending``. The runner applies the same tolerance
    (``_input_bucket_empty``) so status and run outcome agree.
    """
    if not step.produces:
        return "n/a"
    args = list(getattr(step, "args", []) or [])
    if "--input" in args:
        try:
            bucket = Path(args[args.index("--input") + 1])
        except IndexError:
            bucket = None
        if (bucket is not None and bucket.name.startswith("eligible_")
                and bucket.is_dir() and not any(bucket.iterdir())):
            return "n/a"
    seen_any = False
    for raw in step.produces:
        want_dir = raw.endswith("/")
        p = Path(raw.rstrip("/"))
        if not p.exists():
            return "partial" if seen_any else "pending"
        if want_dir:
            if not any(p.iterdir()):
                return "partial" if seen_any else "pending"
        elif p.stat().st_size == 0:
            return "partial" if seen_any else "pending"
        seen_any = True
    return "done"


def resolve_stages(workspace: Path) -> list[dict]:
    """Resolve the registry against ONE review's actual canonical steps.

    Returns JSON-ready dicts: the registry declaration plus the steps that really
    exist for this review and the derived status of their display rows. The
    client renders whatever this returns and never hardcodes the stage list —
    which keeps the most complex contract in the app from drifting the way the
    hand-mirrored ``lib/types.ts`` types already have.
    """
    canonical = pipeline_bridge.canonical_steps_for(workspace)
    steps_by_id = {s.id: s for s in canonical}

    # Per-SCRIPT display detail (row counts etc.) is still useful context to show,
    # even though status is computed per step — see _step_status.
    detail_by_script: dict[str, str] = {}
    for row in pipeline_bridge.review_state(workspace):
        detail_by_script.setdefault(row["script"], row.get("detail", ""))

    claimed: set[str] = set()
    out: list[dict] = []
    for order, stage in enumerate(STAGES):
        resolved_ids = [i for i in (*stage.step_ids, *stage.optional_steps)
                        if i in steps_by_id]
        required_present = any(i in steps_by_id for i in stage.step_ids)
        if stage.step_id_prefixes:
            resolved_ids += [s.id for s in canonical
                             if s.id.startswith(stage.step_id_prefixes)
                             and s.id not in resolved_ids]
        claimed.update(resolved_ids)
        statuses = [_step_status(steps_by_id[i]) for i in resolved_ids]

        out.append(
            {
                "slug": stage.slug,
                "title": stage.title,
                "phase": stage.phase,
                "kind": stage.kind,
                "description": stage.description,
                "order": order,
                "step_ids": resolved_ids,
                "requires_approval": stage.requires_approval,
                "config_keys": list(stage.config_keys),
                "autorun": stage.autorun,
                # Not shown in the walkthrough (still runs + gates server-side).
                "hidden": stage.hidden,
                # A stage whose declared steps all vanished for this review (e.g.
                # the observational pass on an RCT-only review) is skipped, not
                # blocked: there is genuinely no work to do. Optional steps
                # never count toward this (see StageDef.optional_steps).
                "skipped": bool(stage.step_ids) and not required_present,
                "status": _roll_up(statuses),
                "details": _details_for(stage.slug, resolved_ids, steps_by_id, detail_by_script),
            }
        )

    # Nothing may be silently dropped: a canonical step claimed by no stage would
    # run in a full pipeline run but be invisible in the wizard. Surface it.
    orphans = [s.id for s in canonical if s.id not in claimed]
    if orphans:
        out.append(
            {
                "slug": "_other",
                "title": "Other steps",
                # A drift banner, not a phase member — parked in the last phase so
                # it renders after everything real. Excluded from the phase
                # partition test, which covers only registry stages.
                "phase": "publish",
                "kind": "run",
                "description": "Steps not claimed by any wizard stage.",
                "order": len(STAGES),
                "step_ids": orphans,
                "requires_approval": False,
                "config_keys": [],
                "autorun": False,
                "hidden": False,
                "skipped": False,
                "status": "pending",
                "details": [],
            }
        )
    return out


def _roll_up(statuses: list[str]) -> str:
    """Collapse a stage's per-step statuses into one word.

    Deliberately conservative: a stage is only ``done`` when every step that CAN
    be checked is done, because approving a half-finished stage is the failure
    mode the wizard exists to prevent.

    ``n/a`` steps (those declaring no ``produces``) are ignored rather than
    treated as done or pending — they carry no filesystem evidence either way.
    A stage of only such steps reports ``n/a``: honest about not knowing, which
    is why the approve button must show counts rather than trust this alone.

    Note there is no ``blocked`` case: ``_step_status`` reads the filesystem and
    can only report done/partial/pending/n-a. Blocking is an APPROVAL concept
    (an unapproved upstream stage), computed in :func:`resolve_stages` against
    StageState — not something an artifact check can observe.
    """
    known = [s for s in statuses if s != "n/a"]
    if not known:
        return "n/a"
    if all(s == "done" for s in known):
        return "done"
    if any(s in ("done", "partial") for s in known):
        return "partial"
    return "pending"
