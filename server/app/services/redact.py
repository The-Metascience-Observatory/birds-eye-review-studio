"""Scrub secrets out of text a user can read (job logs, SSE, job errors).

Pipeline scripts log request URLs, and several data-source APIs take their key
as a QUERY PARAMETER (``…?api_key=…``, ``&apikey=…``, ``&key=…``,
``&devkey=…``) or a header that a library echoes into an exception message.
Those lines land in ``_jobs/<id>.log``, which any owner of the review reads
through ``GET /jobs/{id}/log`` and the SSE stream — so without this, a review
owner on a shared instance could read the OPERATOR's API keys out of their own
job log.

Two layers, because neither alone is enough:

* **Shapes** — query parameters and headers whose NAME says "secret", plus the
  recognizable provider key formats (``sk-ant-…``, ``sk-or-…``).
* **Values** — the exact values of secret-looking variables in the step's
  environment, so a key printed in an unexpected format is still caught.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

REDACTED = "[REDACTED]"

# name=value in a URL or a log line. The leading boundary stops `sort_key=` or
# `monkey=` from matching `key=`; the value stops at the usual URL delimiters.
_PARAM_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"((?:api[_-]?key|apikey|key|devkey|dev_key|token|access_token|auth_token|"
    r"api_token|id_token|secret|client_secret|password|passwd)=)"
    r"([^&\s\"'<>#]+)"
)
# Header lines, and dict/repr echoes of them ('Authorization': 'Bearer …').
_HEADER_RE = re.compile(
    r"(?i)((?:authorization|proxy-authorization|x-api-key|x-els-apikey|api-key|"
    r"x-goog-api-key|openai-api-key)['\"]?\s*[:=]\s*['\"]?)"
    r"((?:bearer|basic|token)\s+)?([^\s'\",}]+)"
)
# Provider key formats worth catching even outside a name=value pair.
_KEY_SHAPES = re.compile(r"\b(sk-(?:ant|or|proj)?-?[A-Za-z0-9_\-]{16,})")

_SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)")
_MIN_SECRET_LEN = 8


def secret_values(env: Mapping[str, str] | None) -> list[str]:
    """Values of secret-looking variables in *env*, longest first (so a key
    that contains another key is replaced whole)."""
    if not env:
        return []
    vals = {
        v for k, v in env.items()
        if v and len(v) >= _MIN_SECRET_LEN and _SECRET_NAME.search(k)
    }
    return sorted(vals, key=len, reverse=True)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """*text* with every recognizable secret replaced by ``[REDACTED]``."""
    if not text:
        return text
    for value in secrets:
        if value and value in text:
            text = text.replace(value, REDACTED)
    text = _PARAM_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _HEADER_RE.sub(lambda m: m.group(1) + (m.group(2) or "") + REDACTED, text)
    text = _KEY_SHAPES.sub(REDACTED, text)
    return text


def scrub_paths(text: str, replacements: Mapping[str, str]) -> str:
    """Replace absolute server paths with placeholders (``{path: label}``),
    longest path first so a workspace inside DATA_ROOT keeps its own label."""
    if not text:
        return text
    for path in sorted((p for p in replacements if p), key=len, reverse=True):
        text = text.replace(path, replacements[path])
    return text
