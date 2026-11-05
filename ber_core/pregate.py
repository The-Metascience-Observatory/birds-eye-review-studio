"""Keyword pre-gate: skip obviously off-topic abstracts before any LLM call.

Split out of ``classify.py`` so the studio server can preview a pre-gate
setting without importing the classifier (which pulls in pymupdf and the rest of
the pipeline's dependencies). **Stdlib only** — keep it that way, or the server
can no longer import it.

The gate is deliberately broad: a false "pass" costs one ordinary LLM call, while
a false "skip" silently loses a paper from the review. That asymmetry is why the
audit helper exists — a misconfigured pattern can discard most of a corpus at zero
cost and with no error, so the counts must be inspectable before a run.
"""
from __future__ import annotations

import re
from pathlib import Path

# The pre-gate is fully general: it gates on the review's OWN terms — topic
# synonyms plus any `extra_terms` in config. It carries no built-in vocabulary,
# because any hardcoded term list encodes a topic assumption that is wrong for
# every other review. (An earlier version shipped intranasal ROUTE terms as an
# on-by-default base, which silently discarded 93% of an unrelated corpus.)
#
# The intranasal terms that were validated on the antiviral nasal sprays corpus
# (225/225 known-relevant papers match) now live in THAT review's config as
# `extra_terms`, where they belong — a topic-specific choice, opted into by the
# one review it fits, not inherited by all.

# The pattern is English/Latin, so it cannot read non-Latin abstracts
# (CyberLeninka is Russian/Cyrillic, ~6% of that corpus). A non-match there means
# "the gate can't read it", NOT "off-topic" — such abstracts are NEVER skipped.
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_NONLATIN_MIN_CHARS = 15

# Below this length an abstract is treated as absent rather than as a non-match.
_MIN_ABSTRACT_CHARS = 100


def is_nonlatin_heavy(text: str) -> bool:
    """True if the abstract carries enough Cyrillic to be unreadable by the pattern."""
    return len(_CYRILLIC_RE.findall(text)) >= _NONLATIN_MIN_CHARS


def pregate_settings(config: dict) -> dict:
    """Resolve ``classification.pregate`` with defaults."""
    raw = (config.get("classification") or {}).get("pregate") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "extra_terms": [str(t).strip() for t in (raw.get("extra_terms") or [])
                        if str(t).strip()],
    }


def build_pregate_pattern(config: dict) -> "re.Pattern | None":
    """Compile the pre-gate regex from config; None means "no usable pattern".

    Returns None when the review has no terms to gate on (no synonyms, no extra
    terms). That must NOT compile to an empty pattern: an
    empty alternation matches every string, so the gate would look like it ran
    while actually passing the whole corpus through.
    """
    settings = pregate_settings(config)
    synonyms = (config.get("topic") or {}).get("synonyms") or []
    terms = [str(s) for s in synonyms if str(s).strip()] + settings["extra_terms"]
    if not terms:
        return None
    return re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)


def audit_pregate(pregate_re: "re.Pattern | None", papers: list) -> dict:
    """Count how *pregate_re* would partition *papers*, calling no LLM.

    *papers* is the ``(doi, abstract_path, folder)`` shape ``gather_papers``
    returns. A None pattern means the gate is off, so nothing is skipped.

    Returns {total, would_send, term_match, nonlatin_bypass, would_skip,
    no_abstract}. Shared by the ``--pregate-audit`` flag and the studio's preview
    endpoint so the two can never disagree.
    """
    match = nomatch = notext = nonlatin = 0
    for _doi, abst, *_rest in papers:
        try:
            text = (Path(abst).read_text(encoding="utf-8", errors="replace").strip()
                    if abst else "")
        except OSError:
            text = ""
        if len(text) < _MIN_ABSTRACT_CHARS:
            notext += 1
        elif pregate_re is None or pregate_re.search(text):
            match += 1
        elif is_nonlatin_heavy(text):
            nonlatin += 1          # non-Latin: sent to the LLM, never skipped
        else:
            nomatch += 1
    return {
        "total": len(papers),
        "would_send": match + nonlatin,
        "term_match": match,
        "nonlatin_bypass": nonlatin,
        "would_skip": nomatch,
        "no_abstract": notext,
    }
