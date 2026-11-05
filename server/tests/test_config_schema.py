"""Config rules that stop a stage from spending money on garbage.

The motivating incident: a review ran classification with no ``article_types``,
so the model invented 55 free-text labels, the PDF download step matched zero
papers, and the run died — after ~$6 was spent. ``classify.py`` was hardened
for that one key. These tests cover the generalized rule and, specifically, the
same hazard still live in the most expensive script (pass-2 extraction, which
degrades an empty vocabulary to the literal string "other").
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_cfgschema_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.app.services import config_schema as cs  # noqa: E402
from server.app.services import stages as st  # noqa: E402
from server.app.services import workspace as ws  # noqa: E402


def _seed(**over) -> dict:
    cfg = ws.build_config_dict(
        topic_name=over.pop("topic_name", "Test Topic"),
        synonyms=over.pop("synonyms", ["syn"]),
        description="d",
        not_about=[],
        include_observational=True,
    )
    cfg.update(over)
    return cfg


def _codes(issues, level=None) -> set[str]:
    return {i["code"] for i in issues if level is None or i["level"] == level}


def test_a_new_review_has_no_blocking_errors() -> None:
    """A brand-new review legitimately has no queries or extraction vocabularies.

    Those get filled in at the stage that needs them, so the whole-config view
    must not make an untouched review look broken.
    """
    assert _codes(cs.validate(_seed()), level="error") == set()


def test_extract_blocks_on_empty_vocabularies() -> None:
    """The nattokinase hazard, in the script that costs the most per paper.

    extract_pass2.py renders these into its prompt and falls back to the
    literal string "other" when empty, so every outcome lands in one bucket.
    """
    errors = _codes(cs.validate(_seed(), "extract"), level="error")
    assert "config_outcome_domains_empty" in errors
    assert "config_intervention_categories_empty" in errors

    ok = _seed(outcome_domains=["pain"], intervention_categories=["drug"])
    assert _codes(cs.validate(ok, "extract"), level="error") == set()


def test_classify_accepts_the_seeded_article_types() -> None:
    """article_types IS seeded at creation, so classify must not block."""
    assert _codes(cs.validate(_seed(), "classify"), level="error") == set()


def test_classify_blocks_when_article_types_is_emptied() -> None:
    """The original failure, reproduced through the schema."""
    errors = _codes(cs.validate(_seed(article_types=[]), "classify"), level="error")
    assert "config_article_types_empty" in errors


def test_a_list_of_blank_strings_counts_as_empty() -> None:
    """`["", "  "]` renders the same empty vocabulary block that `[]` does."""
    errors = _codes(cs.validate(_seed(article_types=["", "  "]), "classify"), "error")
    assert "config_article_types_empty" in errors


def test_stage_scoping_does_not_block_on_another_stage_s_key() -> None:
    """Running the search must not require pass-2's extraction vocabularies."""
    errors = _codes(cs.validate(_seed(search={"pubmed": ["q"]}), "search"), "error")
    assert errors == set(), f"search blocked on unrelated keys: {errors}"


def test_search_blocks_when_it_has_no_queries() -> None:
    assert "config_search_empty" in _codes(cs.validate(_seed(), "search"), "error")
    ok = _seed(search={"pubmed": ["nattokinase"]})
    assert _codes(cs.validate(ok, "search"), "error") == set()


def test_a_dict_of_only_empty_lists_is_empty() -> None:
    """`{"pubmed": []}` has a key but no queries — the search would find nothing."""
    assert "config_search_empty" in _codes(cs.validate(_seed(search={"pubmed": []}), "search"), "error")


def test_every_rule_names_a_stage_that_exists() -> None:
    """A rule scoped to a typo'd stage would silently never fire."""
    known = {s.slug for s in st.STAGES}
    for rule in cs.RULES:
        unknown = set(rule.stages) - known
        assert not unknown, f"rule {rule.key!r} references unknown stage(s) {unknown}"


def test_no_rule_covers_a_deliberately_unowned_key() -> None:
    """UNOWNED_CONFIG_KEYS are read only by subcommands the pipeline never runs.

    A rule on one would block a run over a key that cannot affect it.
    """
    for rule in cs.RULES:
        assert rule.key not in st.UNOWNED_CONFIG_KEYS, (
            f"{rule.key!r} is declared unowned but has a validation rule"
        )


def test_every_rule_explains_the_consequence() -> None:
    """`why` is shown to the user verbatim; a bare restatement of the rule is
    useless to someone deciding what to do about it."""
    for rule in cs.RULES:
        assert len(rule.why) > 40, f"rule {rule.key!r} needs a real explanation"
        assert rule.read_by, f"rule {rule.key!r} must cite a real reader"


def test_empty_not_about_warns_but_never_blocks() -> None:
    """An empty not_about renders a blank "NOT about" section into the screening
    prompt — the screen then over-includes adjacent lab/methods papers (the
    nattokinase review swept in ~300 fermentation papers). Advisory only: many
    narrow topics genuinely need no negative guidance."""
    cfg = _seed()  # _seed builds with not_about=[]
    issues = cs.validate(cfg, "classify")
    assert "config_topic_not_about_empty" in _codes(issues, "warning")
    assert "config_topic_not_about_empty" not in _codes(issues, "error")


def test_populated_not_about_is_clean() -> None:
    cfg = _seed()
    cfg["topic"]["not_about"] = ["enzyme production", "fermentation"]
    issues = cs.validate(cfg, "classify")
    assert "config_topic_not_about_empty" not in _codes(issues)
