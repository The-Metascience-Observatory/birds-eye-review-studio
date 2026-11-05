"""Meta-analysis feasibility for the wizard's meta stage — computed live.

The `meta` stage asks one question: *can these trials be pooled, and if not,
why?* `grading.assess_poolability` already answers it — verdict per outcome
domain, an attributed blocker for every unpooled outcome, and a plain-language
recommendation — and returns a dict the shipped `FeasibilityCard` renders
directly (same `overall`/`n_with_contrast`/`blockers`/`domains` shape the
published dashboard's `summary.feasibility` carries).

So this service is thin: read the pass-2 extraction records straight from the
workspace (the SAME reader the evidence table uses, so the two never disagree
about which records are `ok`) and hand them to `assess_poolability`. It is
ungated and never blocks — the card explains feasibility; the user keeps the
judgment about whether to pool. Meta-analysis stays optional.
"""
from __future__ import annotations

from pathlib import Path

from ._pipeline_path import ensure_pipeline_on_path
ensure_pipeline_on_path()
from .evidence import _data_dir, _read_jsonl

from ber_core import grading  # noqa: E402


# The two extraction feeds assess_poolability pools over — RCT and observational.
# assess_poolability itself filters to `_status in (None, "ok")`, but the reader
# does too, so a half-written record never reaches it.
_PASS2_FILES = ("trial_extractions_pass2.jsonl", "observational_extractions.jsonl")


def _empty(reason: str) -> dict:
    """The honest no-data verdict, in the shape assess_poolability returns.

    A review that has not reached extraction has nothing to pool. Say so in the
    same schema, so the card renders "no meta-analysis is possible" rather than
    the client having to special-case a missing block.
    """
    from ber_core.scientific import RULES_VERSION
    return {
        "rules_version": RULES_VERSION, "withheld": [], "n_with_comparison": 0,
        "overall": "not_possible",
        "n_papers": 0,
        "n_with_contrast": 0,
        "n_outcomes": 0,
        "n_pooled": 0,
        "domains": [],
        "blockers": [],
        "recommendation": reason,
    }


def build_feasibility(workspace: Path) -> dict:
    """Assess poolability from this review's pass-2 records. Never raises."""
    data = _data_dir(workspace)
    if data is None:
        return _empty(
            "This review is not set up yet, so there is nothing to pool.")

    records: list[dict] = []
    for name in _PASS2_FILES:
        records.extend(_read_jsonl(data / name, ok_only=True))

    if not records:
        return _empty(
            "No full extraction has run yet. Run the deep-extraction stage first, "
            "then feasibility can be assessed.")

    import yaml
    try:
        config = yaml.safe_load((workspace / "config.yaml").read_text()) or {}
    except (OSError, ValueError):
        config = {}
    from ber_core.utils import load_exclusion_dois, load_duplicate_dois, drop_duplicate_records, norm_paper_id
    excluded = load_exclusion_dois(data / "user_exclusions.json") | load_exclusion_dois(data / "delivery_exclusions.json")
    records = drop_duplicate_records([r for r in records if norm_paper_id(r.get("paper_id")) not in excluded], load_duplicate_dois(data / "duplicate_dois.json"))
    policy = config.get("meta_analysis") or {}
    return grading.assess_poolability(records, policy=policy, min_trials=policy.get("min_trials", 2))
