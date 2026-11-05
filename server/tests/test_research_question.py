"""The optional plain-language research question (papertrace-style entry).

A review can start from a plain-language question the AI turns into search
terms. It is OPTIONAL and additive: an existing review with no question must
behave exactly as before, drafting from its description. The load-bearing
properties are (a) the question leads the search-drafting prompt when present,
(b) it falls back to the description when absent, and (c) it round-trips into
config without disturbing the other topic fields.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ber_question_"))
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --- the prompt ---------------------------------------------------------------

def test_the_question_leads_the_search_prompt() -> None:
    from ber_core.llm_search import _build_prompt

    prompt = _build_prompt(
        topic_name="tVNS", description="scope", synonyms=[], mesh_terms=[],
        not_about=[], question="Does tVNS improve sleep in insomnia?")
    assert "Research question: Does tVNS improve sleep in insomnia?" in prompt
    # It leads: appears before the Topic line.
    assert prompt.index("Research question:") < prompt.index("Topic:")


def test_no_question_produces_the_original_prompt() -> None:
    """Backward compat: a review with no question drafts exactly as before."""
    from ber_core.llm_search import _build_prompt

    prompt = _build_prompt(
        topic_name="tVNS", description="scope", synonyms=[], mesh_terms=[],
        not_about=[])
    assert "Research question:" not in prompt
    assert prompt.lstrip().startswith("You are designing")


# --- config seeding + fallback ------------------------------------------------

def test_the_question_is_seeded_into_config() -> None:
    from server.app.services.workspace import build_config_dict

    cfg = build_config_dict(
        topic_name="T", synonyms=["s"], description="d", not_about=["x"],
        include_observational=True, question="My question?")
    assert cfg["topic"]["question"] == "My question?"
    # Siblings intact.
    assert cfg["topic"]["name"] == "T"
    assert cfg["topic"]["synonyms"] == ["s"]
    assert cfg["topic"]["description"] == "d"


def test_a_review_without_a_question_seeds_an_empty_one() -> None:
    """The field always exists (empty), so older code paths never KeyError."""
    from server.app.services.workspace import build_config_dict

    cfg = build_config_dict(
        topic_name="T", synonyms=[], description="d", not_about=[],
        include_observational=True)
    assert cfg["topic"]["question"] == ""


def test_search_drafting_falls_back_to_description_when_no_question(monkeypatch) -> None:
    """The whole point of 'optional': an existing review (no question) drafts
    from its description, so nothing regresses."""
    import server.app.services.search as search_svc

    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return ({"pubmed": ["q"]}, {"cost_usd": 0.0})

    # _draft_blocking imports generate_search_queries lazily from ber_core.llm_search.
    import ber_core.llm_search as llm
    monkeypatch.setattr(llm, "generate_search_queries", fake_generate)

    search_svc._draft_blocking(
        topic_name="T", description="the description", synonyms=[],
        mesh_terms=[], not_about=[],
        question="the description",  # what draft_and_save passes on fallback
        model="sonnet")
    assert captured["question"] == "the description"
