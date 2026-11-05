"""Declarative config rules, checked BEFORE a stage spends money.

WHY THIS EXISTS
---------------
The nattokinase review burned ~$6 producing garbage because ``article_types`` was
absent: the classifier rendered an empty vocabulary block into its prompt, so the
model invented 55 free-text labels ("Not applicable - this is a basic
science/protein engineering study…") that no downstream step accepts. The PDF
download step then matched zero papers and the run died — after the money was
spent.

``classify.py:_require_article_types`` fixed that ONE key in ONE script. The
identical hazard is unguarded elsewhere, most expensively at
``extract_pass2.py:104-107``, which degrades an empty ``intervention_categories``
or ``outcome_domains`` to the literal string ``"other"`` and renders THAT into the
extraction prompt — in the script with the highest per-paper cost in the pipeline.

The rule both existing fixes encode, generalized here:

    A config key that feeds a prompt VOCABULARY or a filter PATTERN must never
    silently default to empty. An empty list is not a benign default; it is a
    prompt that asks the model to choose from nothing.

Rules are declared per stage so the wizard can check exactly the keys the stage
about to run actually reads — and so a key nothing reads cannot be declared.
Output matches ``pipeline_bridge.preflight``'s ``{level, code, message}`` shape,
so the studio renders these with the UI it already has.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigRule:
    """One checkable expectation about a config key."""

    key: str
    """Dotted path, e.g. ``topic.synonyms``."""

    stages: tuple[str, ...]
    """Stage slugs that read this key. The check runs when one is about to run."""

    kind: str = "list"
    """``list`` | ``str`` | ``bool`` | ``dict`` — what the readers expect."""

    required: bool = False
    """Missing or empty is an ERROR (blocks the run) rather than a warning."""

    why: str = ""
    """What goes wrong when this is empty. Shown to the user verbatim, so it
    must describe the CONSEQUENCE, not restate the rule."""

    read_by: str = ""
    """file:line of a real reader. Keeps the declaration honest — every rule here
    should be traceable to code that would actually misbehave."""


# Only keys with a REAL reader belong here. A rule for a key nothing reads would
# block a run for no reason; see stages.UNOWNED_CONFIG_KEYS for the keys that are
# deliberately unchecked because the default pipeline never runs their steps.
RULES: tuple[ConfigRule, ...] = (
    ConfigRule(
        key="article_types",
        stages=("classify",),
        required=True,
        why=(
            "The classifier renders this list into its prompt. Empty, the model "
            "is asked to choose from nothing and invents free-text labels that "
            "every downstream step rejects — and the PDF download step then "
            "matches zero papers."
        ),
        read_by="steps/classify.py:916 (_require_article_types)",
    ),
    ConfigRule(
        key="outcome_domains",
        stages=("extract",),
        required=True,
        why=(
            "Pass-2 extraction renders this into the prompt and falls back to the "
            "literal string \"other\" when empty, so every outcome is extracted "
            "into one meaningless bucket. This is the most expensive step in the "
            "pipeline to get wrong."
        ),
        read_by="steps/extract_pass2.py:106",
    ),
    ConfigRule(
        key="intervention_categories",
        stages=("extract",),
        required=True,
        why=(
            "Pass-2 extraction and intervention canonicalization both render this "
            "into their prompts, falling back to \"other\" when empty — so every "
            "intervention lands in one bucket and the meta-analysis cannot group "
            "trials."
        ),
        read_by="steps/extract_pass2.py:104, steps/classify.py:116",
    ),
    ConfigRule(
        key="topic.name",
        stages=("setup", "search", "classify", "extract1", "extract"),
        kind="str",
        required=True,
        why=(
            "The topic name goes into every prompt AND determines the workspace "
            "directory. Empty, prompts ask about nothing in particular."
        ),
        read_by="ber_core.utils.slugify via every script's project resolution",
    ),
    ConfigRule(
        key="topic.synonyms",
        stages=("search", "classify"),
        why=(
            "Synonyms are other words and phrases for your topic — alternative "
            "names, spellings, or abbreviations. They help the review recognize "
            "which papers are relevant. Without them, the automatic keyword filter "
            "can mistakenly set aside most of your papers before they are reviewed."
        ),
        read_by="ber_core.pregate.build_pregate_pattern, steps/classify.py",
    ),
    ConfigRule(
        key="topic.not_about",
        stages=("classify",),
        why=(
            "This lists what your topic is NOT about (e.g. for a nattokinase "
            "review: enzyme production, fermentation chemistry, lab methods). "
            "The screening prompt renders it as its only negative guidance — "
            "empty, the \"NOT about\" section is a blank header and the screen "
            "over-includes adjacent lab/veterinary/methods papers, each of "
            "which later costs full-text download and screening calls."
        ),
        read_by="steps/classify.py:104-114 (_build_prompt_quick)",
    ),
    ConfigRule(
        key="search",
        stages=("search",),
        kind="dict",
        required=True,
        why="Without per-source queries the search step has nothing to run.",
        read_by="steps/search_papers.py",
    ),
    ConfigRule(
        key="pdf_types",
        stages=("classify",),
        why=(
            "This sets which kinds of articles get their full text (PDF) "
            "downloaded. Leaving it blank uses a sensible default that fits most "
            "reviews — set it only if you want to include or leave out particular "
            "article types."
        ),
        read_by="steps/download_pdfs.py:75",
    ),
)


def _get_path(cfg: dict, dotted: str):
    """Read a dotted path, returning None for any missing link."""
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _is_empty(value, kind: str) -> bool:
    """Empty for the purpose this key serves.

    A list of blank strings is empty: it renders as an empty vocabulary block
    just like ``[]`` does, which is precisely the failure being guarded.
    """
    if value is None:
        return True
    if kind == "list":
        if not isinstance(value, (list, tuple)):
            return True
        return not [v for v in value if str(v).strip()]
    if kind == "dict":
        if not isinstance(value, dict):
            return True
        return not any(
            v for v in value.values() if v not in (None, "", [], {}, ())
        )
    if kind == "str":
        return not str(value).strip()
    if kind == "bool":
        return value is None
    return not value


def validate(cfg: dict, stage_slug: str | None = None) -> list[dict]:
    """Config problems as ``{level, code, message}`` dicts.

    Pass a ``stage_slug`` to check the keys THAT stage's scripts read; a missing
    key is then an ``error`` (it blocks) because the stage is about to run and
    would misbehave.

    ``stage_slug=None`` is the whole-config overview. There, an unset key that a
    LATER stage needs is reported as a ``warning``, never an error: a brand-new
    review legitimately has no search queries and no extraction vocabularies yet
    — they get filled in at the stage that needs them. Blocking on them up front
    would make every new review look broken before the user has done anything.
    """
    issues: list[dict] = []
    for rule in RULES:
        if stage_slug is not None and stage_slug not in rule.stages:
            continue
        if not _is_empty(_get_path(cfg, rule.key), rule.kind):
            continue
        blocking = rule.required and stage_slug is not None
        issues.append(
            {
                "level": "error" if blocking else "warning",
                "code": f"config_{rule.key.replace('.', '_')}_empty",
                # `read_by` stays on the rule for developer docs/tests, but the
                # file:line reference is noise in a message an average user reads.
                "message": f"`{rule.key}` is missing or empty. {rule.why}",
            }
        )
    return issues


def rules_for_stage(stage_slug: str) -> tuple[ConfigRule, ...]:
    """The rules a given stage is subject to (for docs/tests)."""
    return tuple(r for r in RULES if stage_slug in r.stages)
