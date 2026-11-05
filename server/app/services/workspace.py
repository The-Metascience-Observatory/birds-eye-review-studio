"""Materialize a per-review workspace on disk.

Layout produced for review ``{id}``::

    {DATA_ROOT}/reviews/{id}/
        config.yaml
        projects/{slug}/
            data/ abstracts/ converted/ pdfs/ progress/ claude_code_logs/

The ``config.yaml`` mirrors the SHAPE of the repo's ``config*.yaml`` (a top-level
``topic`` block) so pipeline_bridge can derive state from it. Shared constants
and ``slugify`` come from ``ber_core`` (the pipeline's dependency-free core), so
the server and the CLI cannot disagree about a slug or a default.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from ..config import settings

_PROJECT_SUBDIRS = (
    "data",
    "abstracts",
    # The per-paper store: papers/<Type>/<stem>/ holds a paper's PDF, XML,
    # supplements and converted text together (see paper_store.py). It replaced
    # the old pdfs/ + converted/ split, which a new review no longer creates.
    "papers",
    "progress",
    "claude_code_logs",
)


#: Identical to ``constants.ARTICLE_TYPES`` (kept in sync deliberately — this
#: module must not import the pipeline package). ``test_classification_config.py``
#: asserts the two lists match exactly.
#:
#: Seeding this into every new review is not cosmetic: ``classify.py`` renders
#: the list into the classifier prompt, so a review without it asks the model to
#: pick from an empty vocabulary, and the free-text labels it invents are
#: rejected by every downstream step.
from ber_core.defaults import ARTICLE_TYPES


#: Which pass-2 extraction blocks are on by default — mirrors
#: ``extract_pass2.PASS2_FIELD_DEFAULTS`` (this module must not import the
#: pipeline package). "Basic" extraction: the standard blocks on, the heavy
#: meta-analysis blocks off; the user enables the deep ones from the Extract page
#: when a meta-analysis looks possible. ``test_extraction_pass2_fields`` asserts
#: this mirror matches the pipeline's defaults so they can't drift.
from ber_core.defaults import PASS2_FIELD_DEFAULTS as EXTRACTION_PASS2_FIELD_DEFAULTS

#: Full-text acquisition + which source extraction reads. Lives at depth 2, the
#: exact limit of the config PATCH merge, so a partial patch of one key keeps
#: the rest. Mirrored by the CLI defaults in download_pdfs / extract_pass2 —
#: the scripts read this block themselves rather than taking it as arguments.
from ber_core.defaults import FULLTEXT_DEFAULTS

#: Default concurrent full-text extraction workers (Extract stage setting).
#: Mirrors extract_pass2.DEFAULT_EXTRACTION_WORKERS.
#:
#: This is the value SEEDED into a new review's config and the backfill for one
#: that predates the setting — deliberately a stable constant, not a
#: machine-derived number: ``normalize_config`` runs on every read, so deriving
#: it here would silently rewrite a user's saved choice every time free memory
#: moved. The machine-aware CAP is applied at run time instead
#: (``runner.extraction_workers_for``), which bounds the fan-out without
#: mutating what the user chose.
from ber_core.defaults import DEFAULT_EXTRACTION_WORKERS


from ber_core.identity import slugify


def workspace_dir(review_id: str) -> Path:
    return settings.DATA_ROOT / "reviews" / review_id


def create_review_workspace(review_id: str, config_dict: dict) -> Path:
    """Write config.yaml + the projects/{slug}/ tree; return the workspace path."""
    workspace = workspace_dir(review_id)
    workspace.mkdir(parents=True, exist_ok=True)

    from ber_core.storage import atomic_text
    atomic_text(workspace / "config.yaml",
        yaml.safe_dump(config_dict, sort_keys=False, allow_unicode=True),
    )

    slug = slugify((config_dict.get("topic") or {}).get("name") or "default")
    project_dir = workspace / "projects" / slug
    for sub in _PROJECT_SUBDIRS:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    return workspace


def build_config_dict(
    *,
    topic_name: str,
    synonyms: list[str],
    description: str,
    not_about: list[str],
    include_observational: bool,
    generate_meta_analysis: bool = False,
    question: str = "",
) -> dict:
    """Seed the minimal valid config for a new review.

    Minimal contract: at least ``topic.{name,synonyms,description,not_about}``.
    """
    return {
        "topic": {
            "name": topic_name,
            # The plain-language question, when the user gave one. Read back by
            # search drafting; falls back to `description` when absent.
            "question": question,
            "synonyms": list(synonyms),
            "description": description,
            "not_about": list(not_about),
        },
        "include_observational": include_observational,
        # Read back by pipeline_bridge.canonical_steps_for to decide whether
        # 13_build_bundle gets --meta-analysis.
        "generate_meta_analysis": generate_meta_analysis,
        # Human-only by default: the full-text eligibility screen marks non-human
        # (animal / in-vitro) papers ineligible unless the user opts in on the
        # Review page. Read by extract_pass1 + evidence._screen_state.
        "include_animal_studies": False,
        # Which pass-2 blocks the extractor pulls (Extract page toggles). Basic by
        # default; the deep meta-analysis blocks are enabled later. Read by
        # extract_pass2.resolve_pass2_field_flags.
        "extraction_pass2_fields": dict(EXTRACTION_PASS2_FIELD_DEFAULTS),
        # Concurrent full-text extraction workers (Extract page setting). Each is
        # one Sonnet call; read by extract_pass2 when --workers isn't passed.
        "extraction_workers": DEFAULT_EXTRACTION_WORKERS,
        # What to fetch besides the PDF, and which source extraction prefers.
        # Read by download_pdfs / extract_pass1 / extract_pass2.
        "fulltext": dict(FULLTEXT_DEFAULTS),
        # Required by classify.py's `type`/`quick` steps — see ARTICLE_TYPES.
        "article_types": list(ARTICLE_TYPES),
        # No `search` block: queries are drafted on the Search page
        # (services/search.py) or entered by hand.
    }


def normalize_config(config_json: dict) -> dict:
    """Fill in required keys a config may predate, without mutating the input.

    Reviews created before ``article_types`` was seeded would otherwise render an
    empty vocabulary into the classifier prompt. Applied on read so existing
    reviews are correct immediately, with no migration step.

    A DEEP copy: the input is usually ``ReviewConfig.config_json`` — the ORM's
    own JSON object — and the ``synthesis``/``fulltext`` back-fills below used
    to ``setdefault`` straight into its nested dicts. SQLAlchemy does not track
    in-place mutation of a JSON column, so the row silently diverged from the
    database until something else happened to write it.
    """
    cfg = deepcopy(dict(config_json or {}))
    types = [str(t).strip() for t in (cfg.get("article_types") or []) if str(t).strip()]
    if not types:
        cfg["article_types"] = list(ARTICLE_TYPES)
    # Reviews created before the animal policy existed default to human-only.
    if "include_animal_studies" not in cfg:
        cfg["include_animal_studies"] = False
    # Reviews predating the pass-2 field toggles get the basic-extraction defaults
    # (deep meta-analysis blocks off) so the Extract page renders them correctly.
    if not isinstance(cfg.get("extraction_pass2_fields"), dict):
        cfg["extraction_pass2_fields"] = dict(EXTRACTION_PASS2_FIELD_DEFAULTS)
    else:
        # Fill any individually-missing block with its default (additive, so a new
        # block added later shows up on old reviews without a migration).
        merged = dict(EXTRACTION_PASS2_FIELD_DEFAULTS)
        merged.update(cfg["extraction_pass2_fields"])
        cfg["extraction_pass2_fields"] = merged
    # Reviews predating the worker-count setting get the default (10).
    workers = cfg.get("extraction_workers")
    if not isinstance(workers, int) or workers < 1:
        cfg["extraction_workers"] = DEFAULT_EXTRACTION_WORKERS
    # Reviews predating the review-synthesis feature get its defaults so the
    # ReportStage options card renders correctly (additive, key by key).
    syn = cfg.get("synthesis") if isinstance(cfg.get("synthesis"), dict) else {}
    syn.setdefault("depth", "standard")
    syn.setdefault("audience", "clinician")
    cfg["synthesis"] = syn
    # Reviews predating structured full text get its defaults, key by key, so a
    # review that has only ever fetched PDFs starts fetching XML on its next
    # background pass (which backfills — the download step is resumable per
    # artifact class, not per paper).
    ft = cfg.get("fulltext") if isinstance(cfg.get("fulltext"), dict) else {}
    for key, value in FULLTEXT_DEFAULTS.items():
        ft.setdefault(key, value)
    cfg["fulltext"] = ft
    return cfg
