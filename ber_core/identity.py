"""Stable names for things: workspaces, and papers (DOIs / PMIDs).

DOIs reach the pipeline in many shapes — ``https://doi.org/10.1/X``,
``doi:10.1/x``, URL-encoded ``10.1%2Fx``, a filename stem in either of two
encodings (this repo's ``10.1--x`` and fetchpdf's ``10.1--x~3a~y``), with a
trailing period from a sentence, or with an ``#arm`` suffix from the export.
:func:`canonical_doi` maps all of them to one lowercase DOI (or None when the
value is not a DOI at all), and :func:`join_key` is the key every file should
be joined on.
"""
from __future__ import annotations

import re
from urllib.parse import unquote


def slugify(name: str) -> str:
    value = re.sub(r"[^\w\s-]", "_", (name or "").lower())
    return re.sub(r"[-\s_]+", "_", value).strip("_") or "default"


# fetchpdf escapes a character as ``~XX~`` (hex) and ":" as a bare "~".
_STEM_ESCAPE_RE = re.compile(r"~([0-9a-fA-F]{2})~")
_DOI_URL_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.I)
_DOI_SCHEME_RE = re.compile(r"^doi:\s*", re.I)
_VALID_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_PMID_RE = re.compile(r"^pmid[_:\s]*(\d+)$", re.I)
_TRAILING_PUNCT = ".,;:'\""


def decode_stem_escapes(stem: str) -> str:
    """Undo fetchpdf's filename escapes (``~2d~`` -> "-", bare ``~`` -> ":").

    Case-preserving. ``~XX~`` must decode BEFORE the bare-``~`` pass, which
    would otherwise eat its delimiters. A stem this repo wrote has no ``~``.
    """
    decoded = _STEM_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), stem or "")
    return decoded.replace("~", ":")


def stem_to_doi(stem: str) -> str:
    """A paper-folder/file stem -> its DOI, CASE PRESERVED (``pmid_`` kept).

    Decodes both encoders: fetchpdf's escapes and this repo's ``--`` for "/".
    """
    return decode_stem_escapes(stem).replace("--", "/")


def canonical_doi(value) -> str | None:
    """The canonical (lowercase) DOI for *value*, or None if it is not one.

    Strips ``https://(dx.)doi.org/`` and ``doi:`` prefixes, URL-decodes, decodes
    a filename stem (either encoder — only when the value has no "/" yet, so a
    real DOI containing "--" is left alone), drops an ``#arm`` suffix and
    trailing sentence punctuation (and an unbalanced closing bracket), and
    lowercases. Returns None unless the result matches ``10.NNNN/<suffix>``.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = unquote(s).strip()
    s = _DOI_URL_RE.sub("", s)
    s = _DOI_SCHEME_RE.sub("", s)
    s = s.split("#")[0].strip()
    if "/" not in s:
        s = stem_to_doi(s)
    elif "~" in s:
        s = decode_stem_escapes(s)
    while s:
        if s[-1] in _TRAILING_PUNCT:
            s = s[:-1]
        elif s[-1] == ")" and s.count(")") > s.count("("):
            s = s[:-1]
        elif s[-1] == "]" and s.count("]") > s.count("["):
            s = s[:-1]
        else:
            break
    s = s.lower()
    return s if _VALID_DOI_RE.match(s) else None


def join_key(value, *, bare_digits_are_pmids: bool = False) -> str | None:
    """The key to join paper records on: a canonical DOI or ``pmid_<n>``.

    Accepts anything :func:`canonical_doi` does, plus ``pmid_123`` /
    ``PMID:123`` (and bare digits when *bare_digits_are_pmids*). An ``#arm``
    suffix is ignored. None when *value* identifies no paper.
    """
    if value is None:
        return None
    s = str(value).strip().split("#")[0].strip()
    if not s:
        return None
    m = _PMID_RE.match(s)
    if m:
        return f"pmid_{int(m.group(1))}"
    if bare_digits_are_pmids and s.isdigit():
        return f"pmid_{int(s)}"
    return canonical_doi(s)


def paper_join_key(value) -> str:
    """:func:`join_key`, falling back to ``ber_core.evidence.norm_paper_id``.

    For callers that must key EVERY record, including ids that are not a DOI
    or PMID (a trial-registry id, a stem :func:`canonical_doi` rejects): those
    get the legacy lowercase/``--``-decoded key instead of None, so they still
    join to themselves — after the same URL-decoding and ``doi.org`` /
    ``doi:`` prefix stripping, so a malformed DOI still matches its own URL
    form. Never None; ``""`` for a missing value.
    """
    if value is None:
        return ""
    from .evidence import norm_paper_id
    s = str(value)
    key = join_key(s)
    if key:
        return key
    s = _DOI_SCHEME_RE.sub("", _DOI_URL_RE.sub("", unquote(s).strip()))
    return norm_paper_id(s)
