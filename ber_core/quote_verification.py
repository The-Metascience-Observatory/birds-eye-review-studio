"""Verify extracted quotes against the source text they claim to come from.

WHY
---
Every number in the meta-analysis is an LLM assertion. The pass-2 schema has
long carried ``*_source`` fields, but their values are only a provenance CLASS
("verbatim" / "calculated" / "inferred" / "not_reported") — a self-report by the
model, unverifiable and unfalsifiable.

The prompts now also ask for ``*_quote``: the actual span the number came from.
A quote the model invented would be worse than no quote, so it has to be
CHECKED, mechanically, against the converted paper text. That check is what
turns "an LLM said so" into "an LLM said so, and here is the sentence, confirmed
to exist".

WHAT COUNTS AS A MATCH
----------------------
Converted PDFs are lossy: GROBID merges table cells, drops rows, mangles
sub/superscripts, and normalizes unicode inconsistently. So an exact string
match is too strict to be the only test, and a legitimate quote can fail it.
Hence three outcomes — ``exact``, ``fuzzy``, ``absent`` — and the explicit rule
that **nothing gates on ``absent``**. It is reported, never enforced.
"""
from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

# Below this similarity a windowed match is not evidence of anything.
FUZZY_THRESHOLD = 0.85

# Quotes shorter than this are too generic to verify meaningfully ("n = 40").
MIN_QUOTE_CHARS = 12

# The quote fields the prompts ask for, paired with the value they justify.
QUOTE_FIELDS = (
    "result_quote",
    "effect_quote",
    "n_randomized_quote",
    "n_enrolled_quote",
)


def normalize(text: str) -> str:
    """Fold away differences that are formatting, not content.

    Deliberately aggressive: the goal is to detect whether the model COPIED a
    span, not to preserve the source's typography. Anything that survives PDF
    conversion inconsistently gets normalized away.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", html.unescape(text))
    # The pipeline's own PDF conversion substitutes these; see utils.preprocess_md.
    s = s.replace("§", "±")
    # Unicode dashes/minus/quotes -> ASCII.
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"[‘’‛′]", "'", s)
    s = re.sub(r"[“”‟″]", '"', s)
    s = s.replace(" ", " ")
    # Collapse all whitespace: line breaks inside a table row are an artifact.
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _best_window(needle: str, haystack: str) -> tuple[float, int]:
    """Best similarity for `needle` over same-length windows of `haystack`.

    Steps a quarter-window at a time rather than character-by-character: an
    O(len(text)) scan on a 60k-char paper for every quote is too slow to run
    over a whole corpus, and quarter-window granularity still finds any real
    match because the surrounding context shifts gradually.
    """
    n, h = len(needle), len(haystack)
    if n == 0 or h < n:
        return 0.0, -1
    step = max(1, n // 4)
    best, best_at = 0.0, -1
    matcher = SequenceMatcher(None, needle, "", autojunk=False)
    for start in range(0, h - n + 1, step):
        matcher.set_seq2(haystack[start:start + n])
        # real_quick_ratio/quick_ratio are cheap upper bounds; skip hopeless windows.
        if matcher.real_quick_ratio() < best or matcher.quick_ratio() < best:
            continue
        r = matcher.ratio()
        if r > best:
            best, best_at = r, start
            if best >= 0.995:
                break
    return best, best_at


def verify_quote(quote: str | None, full_text: str) -> dict:
    """Check one quote against the source. Never raises.

    Returns ``{status, ratio, offset}`` where status is:
      ``exact``   — found character-for-character (after normalization)
      ``fuzzy``   — found at >= FUZZY_THRESHOLD similarity; PDF conversion noise
      ``absent``  — not found; either a conversion loss or an invented quote
      ``skipped`` — no quote given, or too short to verify
    """
    if not quote or not str(quote).strip():
        return {"status": "skipped", "ratio": None, "offset": None,
                "reason": "no quote"}
    q = normalize(str(quote))
    if len(q) < MIN_QUOTE_CHARS:
        return {"status": "skipped", "ratio": None, "offset": None,
                "reason": "quote too short to verify"}
    if not full_text:
        return {"status": "skipped", "ratio": None, "offset": None,
                "reason": "no source text available"}

    h = normalize(full_text)
    at = h.find(q)
    if at >= 0:
        return {"status": "exact", "ratio": 1.0, "offset": at}

    ratio, at = _best_window(q, h)
    if ratio >= FUZZY_THRESHOLD:
        return {"status": "fuzzy", "ratio": round(ratio, 3), "offset": at}
    return {"status": "absent", "ratio": round(ratio, 3), "offset": None}


def _walk_quotes(node, path=""):
    """Yield ``(path, field, quote)`` for every quote field in a record."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in QUOTE_FIELDS and isinstance(v, (str, type(None))):
                yield path, k, v
            else:
                yield from _walk_quotes(v, f"{path}{k}.")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_quotes(v, f"{path}{i}.")


def verify_record(record: dict, full_text: str,
                  alt_text: str | None = None) -> dict:
    """Verify every quote in one extraction record.

    Returns a summary dict and, as a side effect, annotates each quote's parent
    object with ``<field>_verified`` so the UI can badge the exact number the
    quote justifies rather than only a per-paper total.

    ``alt_text`` is a second haystack, tried ONLY for quotes the first one
    scores ``absent``. It exists for raw XML: ``normalize`` unescapes entities
    and collapses whitespace but does not strip tags, so a quote that reads
    contiguously in a rendered table sits either side of ``</td><td>`` in the
    markup and a legitimate quote is scored a fabrication. Trying the stripped
    text second (never first) leaves every existing md/pdf result bit-identical,
    and costs a second scan only for quotes that already failed.
    """
    counts = {"exact": 0, "fuzzy": 0, "absent": 0, "skipped": 0}
    details = []
    for path, field, quote in _walk_quotes(record):
        res = verify_quote(quote, full_text)
        if res["status"] == "absent" and alt_text:
            alt = verify_quote(quote, alt_text)
            if alt["status"] != "absent":
                res = alt
        counts[res["status"]] += 1
        if res["status"] != "skipped":
            details.append({"path": f"{path}{field}", **res})

    verified = counts["exact"] + counts["fuzzy"]
    checked = verified + counts["absent"]
    return {
        "n_quoted": checked,
        "n_exact": counts["exact"],
        "n_fuzzy": counts["fuzzy"],
        "n_absent": counts["absent"],
        "n_skipped": counts["skipped"],
        # The headline number: of the quotes we could check, how many are real.
        "traceable_rate": round(verified / checked, 3) if checked else None,
        "details": details,
    }


def corpus_summary(summaries: list[dict]) -> dict:
    """Roll per-record summaries into the number worth publishing."""
    tot = {"n_quoted": 0, "n_exact": 0, "n_fuzzy": 0, "n_absent": 0,
           "n_skipped": 0, "n_records": len(summaries)}
    for s in summaries:
        for k in ("n_quoted", "n_exact", "n_fuzzy", "n_absent", "n_skipped"):
            tot[k] += s.get(k) or 0
    checked = tot["n_quoted"]
    tot["traceable_rate"] = (
        round((tot["n_exact"] + tot["n_fuzzy"]) / checked, 3) if checked else None
    )
    return tot
