"""Golden tests for classify's response parsers.

Written against the pre-refactor parsers (one hand-written ``_parse_*`` per
subcommand) and kept as the contract for the shared ``parse_label`` machinery:
the rows each subcommand writes must not change.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("_classify_golden", ROOT / "steps/classify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def c3():
    return _load()


class FakeResp:
    def __init__(self, text, session_id=None, cost=0.0, inp=0, out=0):
        self.text = text
        self.session_id = session_id
        self.cost_usd = cost
        self.input_tokens = inp
        self.output_tokens = out


@pytest.fixture
def followups(monkeypatch, c3):
    """Record retry calls; answer them from a queue."""
    seen: list[dict] = []
    answers: list[str] = []

    def fake_call(prompt, model, doi, timeout=120, resume_session=None, **kw):
        seen.append({"prompt": prompt, "resume_session": resume_session})
        return FakeResp(answers.pop(0) if answers else "", session_id=resume_session)

    monkeypatch.setattr(c3, "call_claude", fake_call)
    return seen, answers


def j(obj) -> str:
    return json.dumps(obj)


# ── relevance / screen: plain field parsing ────────────────────────────────

def test_relevance_row(c3):
    row = c3._parse_relevance(j({"is_relevant": " Yes ", "confidence": "high",
                                 "studies_treatment": "NO"}), "10.1/a")
    assert row == {"doi": "10.1/a", "is_relevant": "yes", "confidence": "high",
                   "studies_treatment": "no"}


def test_relevance_missing_verdict_is_parse_error(c3):
    row = c3._parse_relevance(j({"confidence": "low"}), "10.1/a")
    assert row["is_relevant"] == "parse_error"
    assert row["studies_treatment"] == ""


def test_screen_row(c3):
    row = c3._parse_screen(j({
        "is_relevant": "yes", "studies_treatment": "Yes", "trial_type": "RCT",
        "phase": "Phase 2", "is_excluded": "No", "exclusion_reason": "null",
        "topics": "x"}), "10.1/a")
    assert row == {"doi": "10.1/a", "is_relevant": "yes", "studies_treatment": "yes",
                   "trial_type": "rct", "phase": "phase 2", "is_excluded": "no",
                   "exclusion_reason": "", "topics": "x"}


def test_screen_defaults(c3):
    row = c3._parse_screen(j({"is_relevant": "no"}), "10.1/a")
    assert row["trial_type"] == "unclear" and row["phase"] == "unclear"
    assert row["is_excluded"] == "no" and row["exclusion_reason"] == ""


def test_screen_keeps_real_exclusion_reason(c3):
    row = c3._parse_screen(j({"is_relevant": "yes", "exclusion_reason": "Animal Study"}),
                           "10.1/a")
    assert row["exclusion_reason"] == "animal study"


def test_unparseable_returns_none(c3):
    for fn in (c3._parse_relevance, c3._parse_screen, c3._parse_type, c3._parse_quick,
               c3._parse_spray_type, c3._parse_study_design, c3._parse_indication,
               c3._parse_drug_class):
        assert fn("no json here", "10.1/a") is None


# ── labelled tasks: valid answers ──────────────────────────────────────────

def test_type_valid(c3, followups):
    seen, _ = followups
    row = c3._parse_type(j({"article_type": "Review", "confidence": "high"}), "10.1/a",
                         session_id="s", valid_types={"Review", "Case Report"})
    assert row == {"doi": "10.1/a", "article_type": "Review", "confidence": "high"}
    assert not seen


def test_spray_type_valid(c3):
    row = c3._parse_spray_type(j({"spray_type": "antiviral", "agent": " Iota-Carrageenan ",
                                  "confidence": "medium"}), "10.1/a",
                               valid_categories={"antiviral"})
    assert row == {"doi": "10.1/a", "spray_type": "antiviral",
                   "agent": "iota-carrageenan", "confidence": "medium"}


def test_drug_class_valid(c3):
    row = c3._parse_drug_class(j({"drug_class": "dopamine agonist",
                                  "drug_name": "Pramipexole", "confidence": "high"}),
                               "10.1/a", valid_categories={"dopamine agonist"})
    assert row == {"doi": "10.1/a", "drug_class": "dopamine agonist",
                   "drug_name": "pramipexole", "confidence": "high"}


def test_study_design_valid(c3):
    row = c3._parse_study_design(j({"study_design": "RCT", "confidence": "low"}),
                                 "10.1/a", valid_categories={"RCT"})
    assert row == {"doi": "10.1/a", "study_design": "RCT", "confidence": "low"}


def test_indication_valid(c3):
    row = c3._parse_indication(j({"organism": "human", "indication": "treatment",
                                  "confidence": "high"}), "10.1/a",
                               valid_organisms={"human"}, valid_indications={"treatment"})
    assert row == {"doi": "10.1/a", "organism": "human", "indication": "treatment",
                   "confidence": "high"}


def test_empty_vocabulary_accepts_anything(c3, followups):
    seen, _ = followups
    row = c3._parse_spray_type(j({"spray_type": "whatever"}), "10.1/a",
                               session_id="s", valid_categories=set())
    assert row["spray_type"] == "whatever"
    assert not seen


QUICK_FULL = {
    "is_relevant": "YES", "relevance_confidence": "high", "studies_treatment": "Yes",
    "article_type": "Clinical Trial", "type_confidence": "medium",
    "is_excluded": "No", "exclusion_reason": "None", "certainty": "High",
    "organism": "Human",
}


def test_quick_valid(c3):
    row = c3._parse_quick(j(QUICK_FULL), "10.1/a", valid_types={"Clinical Trial"})
    assert row == {
        "doi": "10.1/a", "is_relevant": "yes", "relevance_confidence": "high",
        "studies_treatment": "yes", "article_type": "Clinical Trial",
        "type_confidence": "medium", "is_excluded": "no", "exclusion_reason": "",
        "certainty": "high", "organism": "human",
    }


def test_quick_defaults_and_organism_coercion(c3):
    row = c3._parse_quick(j({"is_relevant": "no", "article_type": "Review",
                             "organism": "zebrafish", "is_excluded": "maybe"}),
                          "10.1/a", valid_types={"Review"})
    assert row["organism"] == "other"
    assert row["is_excluded"] == "no"
    assert row["exclusion_reason"] == "" and row["certainty"] == ""


# ── labelled tasks: in-session retry on an off-vocabulary label ────────────

def test_type_retry_in_session(c3, followups):
    seen, answers = followups
    answers.append(j({"article_type": "Review", "confidence": "low"}))
    row = c3._parse_type(j({"article_type": "Narrative Review", "confidence": "high"}),
                         "10.1/a", session_id="sess", valid_types={"Review", "Erratum"})
    assert row == {"doi": "10.1/a", "article_type": "Review", "confidence": "low"}
    assert seen[0]["resume_session"] == "sess"
    assert "'Narrative Review' is not a valid article type" in seen[0]["prompt"]
    assert "Erratum, Review" in seen[0]["prompt"]


def test_type_retry_unparseable_keeps_original(c3, followups):
    _, answers = followups
    answers.append("garbage")
    row = c3._parse_type(j({"article_type": "Nope", "confidence": "high"}), "10.1/a",
                         session_id="sess", valid_types={"Review"})
    assert row == {"doi": "10.1/a", "article_type": "Nope", "confidence": "high"}


def test_indication_retry_in_session(c3, followups):
    seen, answers = followups
    answers.append(j({"organism": "human", "indication": "prevention",
                      "confidence": "medium"}))
    row = c3._parse_indication(j({"organism": "people", "indication": "prevention"}),
                               "10.1/a", session_id="s",
                               valid_organisms={"human"},
                               valid_indications={"prevention"})
    assert row["organism"] == "human" and row["confidence"] == "medium"
    assert "organism must be exactly one of: human" in seen[0]["prompt"]


def test_quick_retry_in_session(c3, followups):
    _, answers = followups
    answers.append(j({**QUICK_FULL, "is_relevant": "no", "article_type": "Review"}))
    row = c3._parse_quick(j({**QUICK_FULL, "article_type": "Reviewish"}), "10.1/a",
                          session_id="s", valid_types={"Review"})
    assert row["article_type"] == "Review"
    assert row["is_relevant"] == "no"
