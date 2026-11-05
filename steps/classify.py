#!/usr/bin/env python3
"""Unified classification and screening pipeline.

Subcommands
-----------
  relevance   Is this paper about {topic}? (single question)
  type        What article type is this? (single question)
  quick       Both of the above in one Claude call (~half the cost of relevance+type)
  screen      Is this a treatment/intervention trial?

By default, --input is read from and --output is written under
projects/{slug}/ where {slug} is derived from topic.name in the review config
(every subcommand requires --config). Pass --input / --output to override.

Examples
--------
  python steps/classify.py quick --config configs/config_x.yaml
  python steps/classify.py quick --config ... --limit 10              # smoke test
  python steps/classify.py quick --config ... --output /tmp/test.csv  # explicit output
  python steps/classify.py screen --config ... --dirs /path/to/Original_Research
"""

import argparse
import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.claude_cli import (
    call_claude, parse_json_response, raise_if_auth_failed, run_halted, set_log_dest_dir,
    resolve_model,
)
from ber_core import migrate_to_paper_store
from ber_core import paper_store
from ber_core.pregate import (
    audit_pregate,
    build_pregate_pattern,
    is_nonlatin_heavy,
    pregate_settings,
)
from ber_core.utils import (
    load_config, load_prompt_template, doi_to_filename, slugify,
    load_done_dois_csv, log_progress, emit_run_summary,
    build_pdf_index, gather_papers, dedup_csv, write_csv_atomic, configured_dir,
    RED, YELLOW, RESET,
)

CLI_TIMEOUT = 120
MAX_RETRIES = 3

# The closed organism vocabulary the merged screen (and the full-text pass-1
# screen) share: human vs. the two commonest preclinical animals vs. everything
# else (in-vitro / cell-culture / in-silico / unclear). Kept here so the abstract
# screen and pass-1 can't drift apart.
_ORGANISM_VALUES = ("human", "mouse", "other_animal", "other")

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
# Load this repo's .env.local, as the other numbered scripts do. Without it the
# OpenRouter provider is unusable standalone: claude_cli reads OPENROUTER_API_KEY
# from the process environment, and nothing else here puts it there. The bug was
# invisible while the default provider was the Claude CLI (which uses the host's
# login, not an env key) and in the studio, where dev.sh exports .env.local into
# the workers. `override=False`, so an explicitly-exported key still wins.
try:
    from ber_core.environment import load_local_env as _load_dotenv
    _load_dotenv(BASE_DIR / ".env.local", override=False)
except ImportError:      # dotenv is optional; the CLI provider needs no key
    pass
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))


def _project_paths(config: dict) -> dict:
    """Return a dict of per-project paths derived from config['topic']['name']."""
    topic_name = config.get("topic", {}).get("name", "default")
    slug = slugify(topic_name)
    project_dir = PROJECTS_DIR / slug
    return {
        "topic_name": topic_name,
        "slug": slug,
        "project_dir": project_dir,
        "abstracts_dir": project_dir / "abstracts",
        "data_dir": project_dir / "data",
    }


# ── Prompt builders ──────────────────────────────────────────────────────────
#
# A builder fills the config-derived placeholders NOW and leaves ``{abstract}``
# for ``_classify_one``, which fills it with ``str.format``. ``.format`` then
# also un-doubles the templates' ``{{...}}`` JSON examples — so every value put
# in here must have its own braces doubled, or a topic description containing
# "{" raised KeyError/ValueError for every paper in the run.


def _brace_escape(value) -> str:
    return str(value).replace("{", "{{").replace("}", "}}")


def _fill(template: str, **values) -> str:
    """Substitute ``{name}`` placeholders with brace-escaped values."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", _brace_escape(value))
    return template


def _bullets(items) -> str:
    return "\n".join(f"- {item}" for item in (items or []))


def _build_prompt_relevance(config: dict, template: str) -> str:
    topic = config.get("topic", {})
    return _fill(template,
                 topic_name=topic.get("name", "the condition"),
                 topic_synonyms_str=", ".join((topic.get("synonyms") or [])[:5]),
                 not_about_bullets=_bullets(topic.get("not_about")))


def _build_prompt_type(config: dict, template: str) -> str:
    return _fill(template, article_types_list=_bullets(config.get("article_types")))


def _build_prompt_quick(config: dict, template: str) -> str:
    topic = config.get("topic", {})
    return _fill(template,
                 topic_name=topic.get("name", "the topic"),
                 topic_synonyms_str=", ".join((topic.get("synonyms") or [])[:5]),
                 not_about_bullets=_bullets(topic.get("not_about")),
                 article_types_list=_bullets(config.get("article_types")))


def _build_prompt_spray_type(config: dict, template: str) -> str:
    return _fill(template,
                 topic_name=config.get("topic", {}).get("name", "the topic"),
                 intervention_categories_list=_bullets(config.get("intervention_categories")))


def _build_prompt_study_design(config: dict, template: str) -> str:
    return _fill(template,
                 topic_name=config.get("topic", {}).get("name", "the topic"),
                 study_design_categories_list=_bullets(config.get("study_design_categories")))


def _build_prompt_indication(config: dict, template: str) -> str:
    return _fill(template,
                 topic_name=config.get("topic", {}).get("name", "the topic"),
                 organism_categories_list=_bullets(config.get("organism_categories")),
                 indication_categories_list=_bullets(config.get("indication_categories")))


def _drug_class_vocab(config: dict) -> list:
    return config.get("drug_class_categories") or config.get("intervention_categories") or []


def _build_prompt_drug_class(config: dict, template: str) -> str:
    return _fill(template,
                 topic_name=config.get("topic", {}).get("name", "the topic"),
                 drug_class_categories_list=_bullets(_drug_class_vocab(config)))


def _build_prompt_screen(config: dict, template: str) -> str:
    topic = config.get("topic", {})
    not_about = topic.get("not_about", [])
    return _fill(template,
                 topic_name=topic.get("name", "the condition"),
                 topic_description=str(topic.get("description")
                                       or "the condition under study").strip(),
                 not_about_bullets=_bullets(not_about) if not_about else "- (none specified)")


# ── Response parsers ─────────────────────────────────────────────────────────
#
# Every subcommand's answer is a flat JSON object with two kinds of field:
#
# * free fields, normalized by ``_s`` / ``_lower``. JSON null, true/false and
#   numbers must never reach a bare ``.lower()``: one AttributeError used to
#   escape the worker, and the executor then ran every queued paper through the
#   model and threw the results away;
# * LABELS from a closed vocabulary (article type, spray type, ...), checked by
#   ``parse_label`` and repaired once by ``_retry_labels`` — in the same session
#   when the provider keeps one, otherwise by resending the prompt with the
#   validation error appended (the codex and OpenRouter providers are
#   stateless, so the old session-only retry never fired for them).


def _s(value) -> str:
    """A JSON scalar as a stripped string. null -> "", true/false -> yes/no."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip()


def _lower(value, default: str = "") -> str:
    return _s(value).lower() or default


def parse_label(parsed: dict, field: str, vocab) -> tuple[str, bool]:
    """(label, is_valid) for one closed-vocabulary field.

    A missing or blank label is ``parse_error``. An empty vocabulary accepts
    anything (the subcommand was configured without one).
    """
    value = _s(parsed.get(field)) or "parse_error"
    return value, (not vocab) or value in vocab


#: The reply shape each labelled task asks for when it re-asks.
_REPLY_TYPE = 'a JSON object: {"article_type": "<type>", "confidence": "<high|medium|low>"}'
_REPLY_SPRAY = ('a JSON object: {"spray_type": "<category>", "agent": "<specific compound, lowercased>", '
                '"confidence": "<high|medium|low>"}')
_REPLY_DESIGN = 'a JSON object: {"study_design": "<category>", "confidence": "<high|medium|low>"}'
_REPLY_DRUG = ('a JSON object: {"drug_class": "<category>", "drug_name": "<specific drug, lowercased>", '
               '"confidence": "<high|medium|low>"}')
_REPLY_INDICATION = ('a JSON object: {"organism": "<organism>", "indication": "<indication>", '
                     '"confidence": "<high|medium|low>"}')
_REPLY_QUICK = ("a JSON object with the same fields as before (is_relevant, "
                "relevance_confidence, studies_treatment, article_type, type_confidence, "
                "is_excluded, exclusion_reason, certainty, organism).")


def _make_retry(retry, session_id, model: str, doi: str):
    """A ``retry(followup) -> response`` callable, or None when none is possible.

    The worker passes a full ``retry`` (resume the session if there is one,
    else resend the prompt; either way the cost is accounted). Direct callers
    may pass only ``session_id``.
    """
    if retry is not None:
        return retry
    if session_id:
        return lambda followup: call_claude(followup, model, doi, resume_session=session_id)
    return None


def _retry_labels(parsed: dict, doi: str, labels, retry, reply: str,
                  *, combined_message: bool = False) -> dict:
    """Check each ``(field, vocab, noun)`` label; re-ask once if any is invalid.

    Returns the dict to build the row from: the retry's answer merged over the
    original, so a field the retry omits keeps its first value.
    """
    bad = []
    for field, vocab, noun in labels:
        value, ok = parse_label(parsed, field, vocab)
        if not ok:
            bad.append((field, vocab, noun, value))
    if not bad:
        return parsed
    shown = ", ".join(f"{field}='{value}'" for field, _, _, value in bad)
    if retry is None:
        print(f"  {YELLOW}WARN: Unexpected label(s) for {doi} ({shown}){RESET}",
              file=sys.stderr, flush=True)
        return parsed
    print(f"  {YELLOW}WARN: Unexpected label(s) for {doi} ({shown}), "
          f"asking again{RESET}", file=sys.stderr, flush=True)
    if combined_message:
        lists = " ".join(
            f"{field} must be exactly one of: {', '.join(sorted(vocab or []))}."
            for field, vocab, _ in labels)
        followup = f"Invalid label(s). {lists} Respond with ONLY {reply}"
    else:
        field, vocab, noun, value = bad[0]
        followup = (f"'{value}' is not a valid {noun}. "
                    f"You must choose exactly one from this list: "
                    f"{', '.join(sorted(vocab))}. "
                    f"Respond with ONLY {reply}")
    resp2 = retry(followup)
    parsed2 = parse_json_response(getattr(resp2, "text", "") or "", doi)
    if parsed2 is None:
        return parsed
    return {**parsed, **parsed2}


def _parse_relevance(response: str, doi: str, **_kw) -> dict | None:
    parsed = parse_json_response(response, doi)
    if parsed is None:
        return None
    is_relevant = _lower(parsed.get("is_relevant"))
    if is_relevant not in ("yes", "no"):
        print(f"  {YELLOW}WARN: Unexpected value '{is_relevant}' for {doi}{RESET}",
              file=sys.stderr, flush=True)
    return {
        "doi": doi,
        "is_relevant": is_relevant or "parse_error",
        "confidence": _s(parsed.get("confidence")),
        "studies_treatment": _lower(parsed.get("studies_treatment")),
    }


def _classify_model(config: dict) -> str:
    """The screening model from config, defaulting to haiku.

    ``models.classification`` is the key the stage registry declares (and the
    studio's Config page edits); ``models.classify`` is the constants.py
    spelling — accept both. Before this helper every call site hardcoded
    "haiku", so the declared config key silently did nothing.
    """
    models = config.get("models") or {}
    return resolve_model(models.get("classification") or models.get("classify") or "haiku")


def _require_article_types(config: dict, config_path: str) -> set[str]:
    """Return the config's article_types, or exit with a clear error.

    An empty list is NOT a benign default: it renders an empty "ARTICLE TYPES:"
    block into the prompt, so the model is asked to choose from nothing and
    invents free-text labels that no downstream step accepts. It also used to
    disable the invalid-type retry (every guard read `if valid_types and ...`),
    making a total classifier failure completely silent. Fail loudly instead.
    """
    types = [str(t).strip() for t in (config.get("article_types") or []) if str(t).strip()]
    if not types:
        print(f"\n{RED}ERROR: `article_types` is missing or empty in "
              f"{config_path}{RESET}", file=sys.stderr)
        print("       The classifier needs a vocabulary to choose from; without "
              "one it\n       invents labels and every downstream step rejects "
              "them.\n       Add an `article_types:` list to the config "
              "(see configs/example.yaml for the\n       canonical taxonomy).",
              file=sys.stderr, flush=True)
        sys.exit(1)
    return set(types)


def _labelled(response: str, doi: str, labels, reply: str, *, retry, session_id,
              model: str, combined_message: bool = False) -> dict | None:
    """Parse + validate + (maybe) repair one labelled answer."""
    parsed = parse_json_response(response, doi)
    if parsed is None:
        return None
    return _retry_labels(parsed, doi, labels, _make_retry(retry, session_id, model, doi),
                         reply, combined_message=combined_message)


def _parse_type(response: str, doi: str, *,
                session_id: str | None = None,
                valid_types: set[str] | None = None,
                model: str = "haiku", retry=None,
                **_kw) -> dict | None:
    parsed = _labelled(response, doi, [("article_type", valid_types, "article type")],
                       _REPLY_TYPE, retry=retry, session_id=session_id, model=model)
    if parsed is None:
        return None
    return {
        "doi": doi,
        "article_type": parse_label(parsed, "article_type", valid_types)[0],
        "confidence": _s(parsed.get("confidence")),
    }


def _parse_spray_type(response: str, doi: str, *,
                      session_id: str | None = None,
                      valid_categories: set[str] | None = None,
                      model: str = "haiku", retry=None,
                      **_kw) -> dict | None:
    parsed = _labelled(response, doi,
                       [("spray_type", valid_categories, "spray-type category")],
                       _REPLY_SPRAY, retry=retry, session_id=session_id, model=model)
    if parsed is None:
        return None
    return {
        "doi": doi,
        "spray_type": parse_label(parsed, "spray_type", valid_categories)[0],
        "agent": _lower(parsed.get("agent")),
        "confidence": _s(parsed.get("confidence")),
    }


def _parse_study_design(response: str, doi: str, *,
                        session_id: str | None = None,
                        valid_categories: set[str] | None = None,
                        model: str = "haiku", retry=None,
                        **_kw) -> dict | None:
    parsed = _labelled(response, doi,
                       [("study_design", valid_categories, "study-design category")],
                       _REPLY_DESIGN, retry=retry, session_id=session_id, model=model)
    if parsed is None:
        return None
    return {
        "doi": doi,
        "study_design": parse_label(parsed, "study_design", valid_categories)[0],
        "confidence": _s(parsed.get("confidence")),
    }


def _parse_drug_class(response: str, doi: str, *,
                      session_id: str | None = None,
                      valid_categories: set[str] | None = None,
                      model: str = "haiku", retry=None,
                      **_kw) -> dict | None:
    parsed = _labelled(response, doi,
                       [("drug_class", valid_categories, "drug-class category")],
                       _REPLY_DRUG, retry=retry, session_id=session_id, model=model)
    if parsed is None:
        return None
    return {
        "doi": doi,
        "drug_class": parse_label(parsed, "drug_class", valid_categories)[0],
        "drug_name": _lower(parsed.get("drug_name")),
        "confidence": _s(parsed.get("confidence")),
    }


def _parse_indication(response: str, doi: str, *,
                      session_id: str | None = None,
                      valid_organisms: set[str] | None = None,
                      valid_indications: set[str] | None = None,
                      model: str = "haiku", retry=None,
                      **_kw) -> dict | None:
    parsed = _labelled(response, doi,
                       [("organism", valid_organisms, "organism"),
                        ("indication", valid_indications, "indication")],
                       _REPLY_INDICATION, retry=retry, session_id=session_id,
                       model=model, combined_message=True)
    if parsed is None:
        return None
    return {
        "doi": doi,
        "organism": parse_label(parsed, "organism", valid_organisms)[0],
        "indication": parse_label(parsed, "indication", valid_indications)[0],
        "confidence": _s(parsed.get("confidence")),
    }


def _parse_quick(response: str, doi: str, *,
                 session_id: str | None = None,
                 valid_types: set[str] | None = None,
                 model: str = "haiku", retry=None,
                 **_kw) -> dict | None:
    parsed = _labelled(response, doi, [("article_type", valid_types, "article type")],
                       _REPLY_QUICK, retry=retry, session_id=session_id, model=model)
    if parsed is None:
        return None

    is_relevant = _lower(parsed.get("is_relevant"))
    if is_relevant not in ("yes", "no"):
        print(f"  {YELLOW}WARN: Unexpected is_relevant='{is_relevant}' for {doi}{RESET}",
              file=sys.stderr, flush=True)

    # The keep/drop + certainty fields the merged initial screen adds. Defensive
    # defaults so a model that omits them (or an older prompt) still yields a
    # complete row: not excluded, unknown certainty.
    excl = _lower(parsed.get("is_excluded"), "no")
    reason = _lower(parsed.get("exclusion_reason"))
    reason = "" if reason in ("null", "none", "") else reason
    # Organism: an abstract-level guess (the full-text screen refines it). Coerce
    # to the closed vocabulary; anything off-list (or missing) becomes "other".
    organism = _lower(parsed.get("organism"))
    if organism not in _ORGANISM_VALUES:
        organism = "other"

    return {
        "doi": doi,
        "is_relevant": is_relevant or "parse_error",
        "relevance_confidence": _s(parsed.get("relevance_confidence")),
        "studies_treatment": _lower(parsed.get("studies_treatment")),
        "article_type": parse_label(parsed, "article_type", valid_types)[0],
        "type_confidence": _s(parsed.get("type_confidence")),
        "is_excluded": excl if excl in ("yes", "no") else "no",
        "exclusion_reason": reason,
        "certainty": _lower(parsed.get("certainty")),
        "organism": organism,
    }


def _parse_screen(response: str, doi: str, **_kw) -> dict | None:
    parsed = parse_json_response(response, doi)
    if parsed is None:
        return None
    is_rel = _lower(parsed.get("is_relevant"))
    if is_rel not in ("yes", "no"):
        print(f"  {YELLOW}WARN: unexpected is_relevant='{is_rel}' for {doi}{RESET}",
              file=sys.stderr, flush=True)
    reason = _lower(parsed.get("exclusion_reason"))
    return {
        "doi": doi,
        "is_relevant": is_rel or "parse_error",
        "studies_treatment": _lower(parsed.get("studies_treatment")),
        "trial_type": _lower(parsed.get("trial_type"), "unclear"),
        "phase": _lower(parsed.get("phase"), "unclear"),
        "is_excluded": _lower(parsed.get("is_excluded"), "no"),
        "exclusion_reason": "" if reason in ("null", "") else reason,
        "topics": _s(parsed.get("topics")),
    }


# ── Text reading ─────────────────────────────────────────────────────────────

def _read_text(
    doi: str,
    abstract_path: Path | None,
    pdf_index: dict[str, Path],
    use_pdf: bool,
    max_pages: int = 3,
    max_chars: int = 6000,
) -> tuple[str | None, str]:
    """Read text for classification. Tries abstract first, then PDF fallback.

    When *use_pdf* is False, only the abstract is considered.
    """
    # Try abstract
    if abstract_path and abstract_path.exists():
        try:
            text = abstract_path.read_text(encoding="utf-8", errors="replace").strip()
            if (len(text) >= 100
                    and "*Abstract not available.*" not in text):
                if not use_pdf:
                    return text[:max_chars], "abstract"
                # For relevance/screen: prefer PDF when available, fall back to abstract
                pdf_text = _extract_pdf_text(doi, pdf_index, max_pages, max_chars)
                if pdf_text is not None:
                    return pdf_text, "pdf"
                return text[:max_chars], "abstract"
        except Exception:
            pass

    # No usable abstract; try PDF if allowed
    if use_pdf:
        pdf_text = _extract_pdf_text(doi, pdf_index, max_pages, max_chars)
        if pdf_text is not None:
            return pdf_text, "pdf"

    return None, "none"


def _extract_pdf_text(
    doi: str,
    pdf_index: dict[str, Path],
    max_pages: int = 3,
    max_chars: int = 6000,
) -> str | None:
    stem = doi_to_filename(doi)
    pdf_path = pdf_index.get(stem)
    if pdf_path is None:
        return None
    try:
        import fitz  # optional PDF fallback, not required for abstract screening
        doc = fitz.open(pdf_path)
        text = ""
        for i in range(min(max_pages, len(doc))):
            text += doc[i].get_text()
        doc.close()
        text = text.strip().replace("\x00", "")
        if len(text) < 50:
            return None
        return text[:max_chars]
    except Exception as e:
        print(f"  {YELLOW}WARN: PDF extraction failed for {doi}: {e}{RESET}",
              file=sys.stderr, flush=True)
        return None


# ── Keyword pre-gate ─────────────────────────────────────────────────────────
# The implementation lives in pregate.py (stdlib-only) so the studio server can
# preview a setting without importing this module's heavyweight dependencies.
# Re-exported here under the historical private names.
_is_nonlatin_heavy = is_nonlatin_heavy
_build_pregate_pattern = build_pregate_pattern


# ── Unified worker ───────────────────────────────────────────────────────────

#: Which text the model actually judged, recorded per row as ``text_source``.
#: ``_read_text`` reports "pdf" when it fell back to (or preferred) the PDF.
_TEXT_SOURCE = {"abstract": "abstract", "pdf": "full_text", "none": "none"}


def _blank_row(doi: str, fieldnames: list[str]) -> dict:
    row = {"doi": doi}
    for k in fieldnames:
        row.setdefault(k, "")
    return row


def _classify_one(
    doi: str,
    abstract_path: Path | None,
    source_folder: str,
    pdf_index: dict[str, Path],
    prompt_template: str,
    parse_fn: Callable,
    parse_kwargs: dict,
    fieldnames: list[str],
    use_pdf: bool,
    model: str,
    max_pages: int = 3,
    max_chars: int = 6000,
    pregate_re: "re.Pattern | None" = None,
) -> dict:
    """Classify/screen a single paper. Returns result dict with _cost/_token metrics."""
    if run_halted():
        # Credential rejected or usage stop: nothing can succeed. Return a
        # marker the main loop discards (writing a parse_error row would be
        # harmless — it is retried — but it would also be a lie).
        return {"doi": doi, "_halted": True}

    text, source = _read_text(doi, abstract_path, pdf_index, use_pdf,
                              max_pages, max_chars)

    acc = {"cost": 0.0, "in": 0, "out": 0}

    def _account(resp) -> None:
        acc["cost"] += getattr(resp, "cost_usd", 0.0) or 0.0
        acc["in"] += getattr(resp, "input_tokens", 0) or 0
        acc["out"] += getattr(resp, "output_tokens", 0) or 0

    def _finish(result: dict, **extra) -> dict:
        result["source_folder"] = source_folder
        result["text_source"] = _TEXT_SOURCE.get(source, source)
        result["_cost"] = acc["cost"]
        result["_input_tokens"] = acc["in"]
        result["_output_tokens"] = acc["out"]
        result["_source"] = source
        result.update(extra)
        return result

    if text is None:
        result = _blank_row(doi, fieldnames)
        # Mark as unknown/no_abstract depending on task
        if "is_relevant" in fieldnames:
            result["is_relevant"] = "unknown"
        if "article_type" in fieldnames:
            result["article_type"] = "no_abstract"
        return _finish(result)

    # Keyword pre-gate: if the abstract has a real body but no on-topic term,
    # skip the LLM and label is_relevant=no (confidence marker "pregate" so the
    # row stays auditable / re-runnable). Only active when pregate_re is passed
    # (relevance + quick tasks). Non-Latin (Cyrillic) abstracts are never gated —
    # the English pattern can't read them, so they go to the LLM. Validated 0%
    # recall loss on English known-relevant; direct test surfaced the Cyrillic gap.
    if (pregate_re is not None
            and not pregate_re.search(text)
            and not _is_nonlatin_heavy(text)):
        result = _blank_row(doi, fieldnames)
        if "is_relevant" in fieldnames:
            result["is_relevant"] = "no"
        # relevance subcommand uses "confidence"; quick uses "relevance_confidence"
        for conf_field in ("relevance_confidence", "confidence"):
            if conf_field in fieldnames:
                result[conf_field] = "pregate"
        if "studies_treatment" in fieldnames:
            result["studies_treatment"] = "no"
        if "article_type" in fieldnames:
            result["article_type"] = ""
        if "type_confidence" in fieldnames:
            result["type_confidence"] = ""
        return _finish(result, _pregated=True)

    body = f"[Extracted from PDF]\n\n{text}" if (use_pdf and source == "pdf") else text
    prompt = prompt_template.format(abstract=body)

    result = None
    for attempt in range(MAX_RETRIES):
        resp = call_claude(prompt, model, doi, timeout=CLI_TIMEOUT)
        _account(resp)
        if run_halted():
            return {"doi": doi, "_halted": True}
        session_id = resp.session_id

        def retry(followup: str, _sid=session_id):
            # Repair an off-vocabulary label. With a session, continue it; a
            # stateless provider (codex, OpenRouter) gets the whole prompt again
            # with the validation error appended. Either way it is billed.
            if _sid:
                resp2 = call_claude(followup, model, doi, resume_session=_sid)
            else:
                resp2 = call_claude(f"{prompt}\n\n{followup}", model, doi,
                                    timeout=CLI_TIMEOUT)
            _account(resp2)
            return resp2

        result = parse_fn(resp.text, doi, session_id=session_id, model=model,
                          retry=retry, **parse_kwargs)
        if result is not None:
            break
        if attempt < MAX_RETRIES - 1:
            print(f"  {YELLOW}RETRY {attempt + 1}/{MAX_RETRIES} for {doi}{RESET}",
                  file=sys.stderr, flush=True)
            time.sleep(1)

    if result is None:
        result = _blank_row(doi, fieldnames)
        if "is_relevant" in fieldnames:
            result["is_relevant"] = "parse_error"
        if "article_type" in fieldnames:
            result["article_type"] = "parse_error"

    return _finish(result)


# ── Header-safe CSV writing ──────────────────────────────────────────────────
# Groups of interchangeable column names. When a task appends to a file written
# by a different task (e.g. the migrated legacy split: relevance.csv uses
# `is_long_covid`, classifications.csv uses `article_type`), we align our rows
# to the file's EXISTING header — mapping task fields onto equivalent columns —
# instead of blindly writing in task-field order (which silently shifts every
# value one column and corrupts the file).
_ALIAS_GROUPS = [
    {"is_relevant", "is_long_covid", "relevant", "relevance"},
    {"confidence", "relevance_confidence", "type_confidence"},
]


def _aliases_of(name: str) -> set[str]:
    for g in _ALIAS_GROUPS:
        if name in g:
            return g
    return {name}


def _read_csv_header(path: Path) -> list[str]:
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            return next(csv.reader(f), [])
    except Exception:
        return []


def _col_has_home(field: str, header: list[str]) -> bool:
    """True if task `field` can be stored in some column of `header` (direct/alias)."""
    return bool(_aliases_of(field) & set(header))


def _value_for_col(col: str, result: dict) -> str:
    """Best value from `result` for an output column `col` (direct, then alias)."""
    if col in result and result[col] not in (None, ""):
        return result[col]
    for k in _aliases_of(col):
        if k in result and result[k] not in (None, ""):
            return result[k]
    return result.get(col, "") if result.get(col) is not None else ""


# ── Shared pipeline runner ───────────────────────────────────────────────────

#: Columns added after files already existed in the wild. An existing CSV that
#: lacks one gets it appended to its header (rows get ""), instead of the value
#: being dropped with a "no matching column" warning on every run.
_LATE_COLUMNS = ("text_source",)

#: A run whose first N completions ALL raised is systematically broken (a bug,
#: not a bad paper): stop instead of failing every remaining paper the same way.
_SYSTEMATIC_FAILURE_N = 20


def _add_columns(path: Path, header: list[str], new_cols: list[str]) -> list[str]:
    """Append *new_cols* to an existing CSV's header, atomically."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    full = header + [c for c in new_cols if c not in header]
    write_csv_atomic(path, rows, full)
    return full


def _run_pipeline(
    papers: list[tuple[str, Path | None, str]],
    output_path: Path,
    fieldnames: list[str],
    pdf_index: dict[str, Path],
    prompt_template: str,
    parse_fn: Callable,
    parse_kwargs: dict,
    use_pdf: bool,
    model: str,
    workers: int,
    progress_interval: int = 10,
    counter_update_fn: Callable | None = None,
    max_pages: int = 3,
    max_chars: int = 6000,
    pregate_re: "re.Pattern | None" = None,
    step: str = "3_classify",
) -> dict:
    """Run the classification pipeline with thread pool, CSV output, and progress."""
    # Decide the column order we will WRITE in. For a new file, use the task's
    # fieldnames. For an existing file, reuse its header and align our rows to it
    # (header-safe) so we never shift columns / corrupt a legacy-schema file.
    if not output_path.exists() or output_path.stat().st_size == 0:
        write_fieldnames = list(fieldnames)
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=write_fieldnames).writeheader()
    else:
        existing = _read_csv_header(output_path)
        late = [c for c in _LATE_COLUMNS if c in fieldnames and c not in existing]
        if existing and late:
            existing = _add_columns(output_path, existing, late)
        if existing and set(existing) != set(fieldnames):
            write_fieldnames = existing
            homeless = [fn for fn in fieldnames
                        if fn != "doi" and not _col_has_home(fn, existing)]
            if homeless:
                print(f"  {YELLOW}WARN: '{output_path.name}' has a different schema "
                      f"({existing}); these classifier fields have no matching column "
                      f"and will be dropped: {homeless}{RESET}",
                      file=sys.stderr, flush=True)
        else:
            write_fieldnames = existing or list(fieldnames)

    total = len(papers)
    write_lock = threading.Lock()
    counter = {
        "done": 0, "total": total, "total_cost": 0.0,
        "api_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "pregated": 0, "errors": 0, "halted": 0,
        "start": time.monotonic(),
    }

    def _record(result: dict) -> None:
        cost = result.pop("_cost", 0.0)
        in_tok = result.pop("_input_tokens", 0)
        out_tok = result.pop("_output_tokens", 0)
        result.pop("_source", None)
        pregated = result.pop("_pregated", False)
        # Remove source_folder from row if not in fieldnames
        if "source_folder" not in fieldnames:
            result.pop("source_folder", None)
        with write_lock:
            with open(output_path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=write_fieldnames).writerow(
                    {col: _value_for_col(col, result) for col in write_fieldnames}
                )
            counter["done"] += 1
            counter["total_cost"] += cost
            counter["input_tokens"] += in_tok
            counter["output_tokens"] += out_tok
            if pregated:
                counter["pregated"] += 1
            if cost > 0:
                counter["api_calls"] += 1
            if counter_update_fn:
                counter_update_fn(result, counter)
            if counter["done"] % progress_interval == 0:
                log_progress(
                    counter["done"], counter["total"],
                    cost=counter["total_cost"],
                    start_time=counter["start"],
                    extra=(
                        f"tokens: {counter['input_tokens']:,}in/"
                        f"{counter['output_tokens']:,}out"
                    ),
                )

    pool = ThreadPoolExecutor(max_workers=workers)
    stop_reason = ""
    try:
        futures = {
            pool.submit(
                _classify_one, doi, abst, folder, pdf_index, prompt_template,
                parse_fn, parse_kwargs, fieldnames, use_pdf, model,
                max_pages, max_chars, pregate_re,
            ): doi
            for doi, abst, folder in papers
        }
        for future in as_completed(futures):
            doi = futures[future]
            # One paper's failure must cost that paper, not the run. An
            # exception escaping here used to unwind into the executor's
            # __exit__, which then ran EVERY queued paper through the model and
            # discarded the results.
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — per-paper isolation
                counter["errors"] += 1
                print(f"  {RED}ERROR: {doi}: {type(exc).__name__}: {exc} "
                      f"(not written; retried next run){RESET}",
                      file=sys.stderr, flush=True)
                if (counter["errors"] >= _SYSTEMATIC_FAILURE_N
                        and counter["done"] == 0):
                    stop_reason = (f"the first {counter['errors']} papers all failed; "
                                   f"stopping instead of failing the rest the same way")
                    break
                continue
            if result.pop("_halted", False):
                counter["halted"] += 1
            else:
                _record(result)
            if run_halted():
                stop_reason = "LLM credential rejected or usage limit reached"
                break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        print("\n  Interrupted! Shutting down workers...", file=sys.stderr, flush=True)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        if stop_reason and stop_reason != "interrupted":
            print(f"  {RED}Stopping: {stop_reason}. Cancelling queued papers; "
                  f"finished rows are kept.{RESET}", file=sys.stderr, flush=True)
        # Cancel whatever is still queued (a no-op after a clean drain) and wait
        # only for the few in flight, which return at once once halted.
        pool.shutdown(wait=stop_reason != "interrupted", cancel_futures=True)

    # One machine-readable line per subcommand, so the server reads real cost and
    # counts instead of regexing them back out of the log.
    emit_run_summary(step, counter, outputs=[output_path], model=model,
                     data_dir=output_path.parent)
    if stop_reason and not run_halted():
        # run_halted() exits from main() via raise_if_auth_failed(); anything
        # else that stopped the run is a failure of this step.
        raise SystemExit(f"3_classify: {stop_reason}")
    return counter


# ── Project-aware path resolution ────────────────────────────────────────────

def _resolve_io(args, default_output_name: str
                ) -> tuple[dict, Path, Path]:
    """Return (config, input_dir, output_path) honoring per-project defaults.

    Per-project layout (derived from config['topic']['name']):
        projects/{slug}/abstracts/   <- default --input
        projects/{slug}/data/{name}  <- default --output
    Explicit --input / --output paths from the CLI override these defaults.
    """
    config = load_config(args.config)
    paths = _project_paths(config)
    input_dir = (Path(args.input) if args.input
                 else paths["abstracts_dir"])
    output_path = (Path(args.output) if args.output
                   else paths["data_dir"] / default_output_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Project: {paths['topic_name']!r} -> {paths['project_dir']}",
          file=sys.stderr, flush=True)
    set_log_dest_dir(paths["project_dir"] / "claude_code_logs")
    print(f"Input:   {input_dir}", file=sys.stderr, flush=True)
    print(f"Output:  {output_path}", file=sys.stderr, flush=True)
    return config, input_dir, output_path


def _resolve_pregate(args, config: dict, papers: list) -> tuple["re.Pattern | None", bool]:
    """Build the pre-gate pattern and optionally run --pregate-audit.

    Precedence: --no-pregate (CLI) > `classification.pregate.enabled` (config) >
    on by default. Returns (pregate_re, should_return); should_return is True for
    an audit-only run (nothing classified).
    """
    if getattr(args, "no_pregate", False):
        print("Pre-gate: DISABLED (--no-pregate)", file=sys.stderr, flush=True)
        return None, False

    settings = pregate_settings(config)
    if not settings["enabled"]:
        print("Pre-gate: DISABLED (classification.pregate.enabled=false)",
              file=sys.stderr, flush=True)
        return None, False

    pregate_re = _build_pregate_pattern(config)
    if pregate_re is None:
        # Base terms off with no synonyms/extra terms. Treat as disabled rather
        # than compiling an empty pattern (which would match everything).
        print(f"  {YELLOW}Pre-gate: DISABLED — no terms configured "
              f"(base terms off, no topic synonyms, no extra_terms){RESET}",
              file=sys.stderr, flush=True)
        return None, False

    if getattr(args, "pregate_audit", False):
        a = audit_pregate(pregate_re, papers)
        tot = a["total"] or 1
        print(
            f"\nPREGATE AUDIT over {a['total']} pending papers:\n"
            f"  would SEND to LLM:        {a['would_send']} "
            f"({100*a['would_send']/tot:.1f}%)"
            f"  [{a['term_match']} term-match + {a['nonlatin_bypass']} non-Latin bypass]\n"
            f"  would SKIP (pregate=no):  {a['would_skip']} "
            f"({100*a['would_skip']/tot:.1f}%)\n"
            f"  no usable abstract:       {a['no_abstract']} "
            f"(handled as 'unknown', no call either)\n"
            f"  -> ~{100*a['would_skip']/tot:.0f}% of LLM calls avoided.",
            file=sys.stderr, flush=True,
        )
        return pregate_re, True

    n_syn = len([s for s in (config.get("topic", {}).get("synonyms") or []) if s])
    print(f"Pre-gate: ENABLED ({n_syn} synonyms, "
          f"{len(settings['extra_terms'])} extra terms)",
          file=sys.stderr, flush=True)
    return pregate_re, False


# ── Subcommand: relevance ────────────────────────────────────────────────────

def cmd_relevance(args):
    config, input_dir, output_path = _resolve_io(args, "relevance.csv")
    template = load_prompt_template("classify_relevance.txt", slug=_project_paths(config)["slug"])
    prompt_template = _build_prompt_relevance(config, template)

    print("Building PDF index...", file=sys.stderr, flush=True)
    pdf_index = build_pdf_index([input_dir], recursive=True)
    print(f"Indexed {len(pdf_index)} PDFs", file=sys.stderr, flush=True)

    papers = gather_papers([input_dir], recursive=True)
    print(f"Found {len(papers)} abstract files", file=sys.stderr, flush=True)

    # `is_relevant` is THIS screen's verdict column. Naming it explicitly is
    # load-bearing: the helper's legacy fallback guessed at `is_long_covid` /
    # `article_type`, neither of which this CSV has, so the "unknown" skip-value
    # never matched — rows stuck at unknown counted as done and were never
    # re-screened even after step 2 fetched a real abstract.
    done_dois = load_done_dois_csv(output_path, id_column="doi",
                                   skip_values={"parse_error", "unknown", ""},
                                   status_column=_status_columns("is_relevant"))
    papers = [(d, a, f) for d, a, f in papers if d not in done_dois]
    if done_dois:
        print(f"Skipping {len(done_dois)} already classified, "
              f"{len(papers)} remaining", file=sys.stderr, flush=True)

    if args.limit > 0:
        papers = papers[:args.limit]
    print(f"Processing {len(papers)} files with {args.workers} workers",
          file=sys.stderr, flush=True)

    pregate_re, audit_only = _resolve_pregate(args, config, papers)
    if audit_only:
        return

    fieldnames = ["doi", "is_relevant", "confidence", "studies_treatment", "text_source"]

    counter = _run_pipeline(
        papers, output_path, fieldnames, pdf_index, prompt_template,
        parse_fn=_parse_relevance, parse_kwargs={},
        use_pdf=True, model=_classify_model(config), workers=args.workers,
        progress_interval=50, max_pages=2, max_chars=4000,
        pregate_re=pregate_re,
        step="3_classify_relevance",
    )

    print(
        f"Done. Classified {counter['done']} papers "
        f"({counter.get('pregated', 0)} pre-gated, no LLM call). "
        f"Output: {output_path} | Total cost: ${counter['total_cost']:.4f}",
        file=sys.stderr, flush=True,
    )

    if counter["done"] > 0:
        print("Deduplicating output CSV...", file=sys.stderr, flush=True)
        n = dedup_csv(output_path, id_column="doi")
        print(f"Deduplicated: {n} unique DOIs", file=sys.stderr, flush=True)


# ── Labelled subcommands: type, spray_type, drug_class, study_design, indication ──
#
# These five differ only in their prompt, vocabulary, columns and output file,
# so they share one driver. `quick`, `relevance` and `screen` add a pre-gate,
# PDF reading or stub handling and keep their own drivers below.

def _status_columns(column: str) -> tuple[str, ...]:
    """The verdict column plus its legacy aliases, first match wins.

    long_covid's relevance.csv / trial_screening.csv still carry the legacy
    ``is_long_covid`` header; naming only ``is_relevant`` would make every row
    look undecided there and re-screen the whole file on every run.
    """
    return (column, *sorted(_aliases_of(column) - {column}))


def _load_relevant_dois(csv_path: Path) -> set[str]:
    """Return the set of DOIs marked is_relevant=yes in a classifications CSV."""
    relevant = set()
    if not csv_path.exists():
        return relevant
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("is_relevant") or "").strip().lower() == "yes":
                doi = (row.get("doi") or "").strip()
                if doi:
                    relevant.add(doi)
    return relevant


@dataclass(frozen=True)
class _LabelTask:
    output_name: str
    template: str
    build_prompt: Callable[[dict, str], str]
    parse_fn: Callable
    parse_kwargs: Callable[[dict, object], dict]  # (config, args) -> kwargs
    fieldnames: tuple[str, ...]
    status_column: str
    step: str
    progress_interval: int = 20
    restrict_to_relevant: bool = True
    recursive: bool = True
    dedup: bool = True


def _run_label_task(args, task: _LabelTask) -> dict:
    config, input_dir, output_path = _resolve_io(args, task.output_name)
    # Vocabulary first: an empty article_types list must fail before any prompt
    # is built (see _require_article_types).
    parse_kwargs = task.parse_kwargs(config, args)
    template = load_prompt_template(task.template, slug=_project_paths(config)["slug"])
    prompt_template = task.build_prompt(config, template)

    papers = gather_papers([input_dir], recursive=task.recursive)
    print(f"Found {len(papers)} abstract files", file=sys.stderr, flush=True)

    if task.restrict_to_relevant:
        # Restrict to relevant papers only (default: data/classifications.csv).
        relevant_csv = (Path(args.relevant_csv) if getattr(args, "relevant_csv", None)
                        else output_path.parent / "classifications.csv")
        relevant_dois = _load_relevant_dois(relevant_csv)
        print(f"Relevant DOIs (is_relevant=yes) from {relevant_csv}: "
              f"{len(relevant_dois)}", file=sys.stderr, flush=True)
        if relevant_dois:
            papers = [(d, a, f) for d, a, f in papers if d in relevant_dois]
            print(f"Restricted to {len(papers)} relevant abstracts",
                  file=sys.stderr, flush=True)

    # The verdict column is named so a parse_error row is retried next run.
    done_dois = load_done_dois_csv(output_path, id_column="doi",
                                   skip_values={"parse_error", ""},
                                   status_column=_status_columns(task.status_column))
    papers = [(d, a, f) for d, a, f in papers if d not in done_dois]
    if done_dois:
        print(f"Skipping {len(done_dois)} already classified, "
              f"{len(papers)} remaining", file=sys.stderr, flush=True)

    if args.limit > 0:
        papers = papers[:args.limit]
    print(f"Processing {len(papers)} files with {args.workers} workers",
          file=sys.stderr, flush=True)

    counter = _run_pipeline(
        papers, output_path, list(task.fieldnames), pdf_index={},
        prompt_template=prompt_template,
        parse_fn=task.parse_fn, parse_kwargs=parse_kwargs,
        use_pdf=False, model=_classify_model(config), workers=args.workers,
        progress_interval=task.progress_interval,
        step=task.step,
    )

    print(
        f"Done. Classified {counter['done']} abstracts. Output: {output_path} | "
        f"Total cost: ${counter['total_cost']:.4f}",
        file=sys.stderr, flush=True,
    )

    if task.dedup and counter["done"] > 0:
        print("Deduplicating output CSV...", file=sys.stderr, flush=True)
        n = dedup_csv(output_path, id_column="doi")
        print(f"Deduplicated: {n} unique DOIs", file=sys.stderr, flush=True)
    return counter


_TYPE_TASK = _LabelTask(
    output_name="classifications.csv", template="classify_type.txt",
    build_prompt=_build_prompt_type, parse_fn=_parse_type,
    parse_kwargs=lambda config, args: {
        "valid_types": _require_article_types(config, args.config)},
    fieldnames=("doi", "article_type", "confidence"), status_column="article_type",
    step="3_classify_type", progress_interval=50,
    restrict_to_relevant=False, recursive=False, dedup=False,
)

# Mirrors drug_class for nasal-spray topics: category + specific agent.
_SPRAY_TASK = _LabelTask(
    output_name="spray_types.csv", template="classify_spray_type.txt",
    build_prompt=_build_prompt_spray_type, parse_fn=_parse_spray_type,
    parse_kwargs=lambda config, args: {
        "valid_categories": set(config.get("intervention_categories", []))},
    fieldnames=("doi", "spray_type", "agent", "confidence"), status_column="spray_type",
    step="3_classify_spray_type",
)

# Treatment drug class + specific drug (e.g. restless legs): the meta-analysis
# grouping unit and the screening page's drug breakdown.
_DRUG_TASK = _LabelTask(
    output_name="drug_types.csv", template="classify_drug_class.txt",
    build_prompt=_build_prompt_drug_class, parse_fn=_parse_drug_class,
    parse_kwargs=lambda config, args: {
        "valid_categories": set(_drug_class_vocab(config))},
    fieldnames=("doi", "drug_class", "drug_name", "confidence"), status_column="drug_class",
    step="3_classify_drug_class",
)

_DESIGN_TASK = _LabelTask(
    output_name="study_designs.csv", template="classify_study_design.txt",
    build_prompt=_build_prompt_study_design, parse_fn=_parse_study_design,
    parse_kwargs=lambda config, args: {
        "valid_categories": set(config.get("study_design_categories", []))},
    fieldnames=("doi", "study_design", "confidence"), status_column="study_design",
    step="3_classify_study_design",
)

_INDICATION_TASK = _LabelTask(
    output_name="indications.csv", template="classify_indication.txt",
    build_prompt=_build_prompt_indication, parse_fn=_parse_indication,
    parse_kwargs=lambda config, args: {
        "valid_organisms": set(config.get("organism_categories", [])),
        "valid_indications": set(config.get("indication_categories", []))},
    fieldnames=("doi", "organism", "indication", "confidence"), status_column="organism",
    step="3_classify_indication",
)


def cmd_type(args):
    _run_label_task(args, _TYPE_TASK)


def cmd_spray_type(args):
    _run_label_task(args, _SPRAY_TASK)


def cmd_drug_class(args):
    _run_label_task(args, _DRUG_TASK)


def cmd_study_design(args):
    _run_label_task(args, _DESIGN_TASK)


def cmd_indication(args):
    _run_label_task(args, _INDICATION_TASK)


# ── Subcommand: quick (combined relevance + type) ────────────────────────────

ABSTRACT_STUB_MARKER = "*Abstract not available.*"


def _is_abstract_stub(abstract_path) -> bool:
    """True when the abstract file is fetch_abstracts' title-only placeholder."""
    if not abstract_path:
        return True
    try:
        return ABSTRACT_STUB_MARKER in Path(abstract_path).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False


def _load_no_abstract_dois(output_path: Path) -> set[str]:
    """DOIs already screened and found to have no abstract.

    Keyed on the screen's own `article_type == "no_abstract"` verdict rather
    than re-reading the files, so it reflects what the model actually saw.
    """
    out: set[str] = set()
    if not output_path.exists():
        return out
    try:
        with open(output_path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                if (row.get("article_type") or "").strip() == "no_abstract":
                    doi = (row.get("doi") or "").strip()
                    if doi:
                        out.add(doi)
    except OSError:
        return set()
    return out


def cmd_quick(args):
    config, input_dir, output_path = _resolve_io(args, "classifications.csv")
    valid_types = _require_article_types(config, args.config)
    template = load_prompt_template("classify_quick.txt", slug=_project_paths(config)["slug"])
    prompt_template = _build_prompt_quick(config, template)

    papers = gather_papers([input_dir], recursive=True)
    print(f"Found {len(papers)} abstract files", file=sys.stderr, flush=True)

    # `is_relevant` is THIS screen's verdict column. Naming it explicitly is
    # load-bearing: the helper's legacy fallback guessed at `is_long_covid` /
    # `article_type`, neither of which this CSV has, so the "unknown" skip-value
    # never matched — rows stuck at unknown counted as done and were never
    # re-screened even after step 2 fetched a real abstract.
    done_dois = load_done_dois_csv(output_path, id_column="doi",
                                   skip_values={"parse_error", "unknown", ""},
                                   status_column=_status_columns("is_relevant"))
    papers = [(d, a, f) for d, a, f in papers if d not in done_dois]
    if done_dois:
        print(f"Skipping {len(done_dois)} already classified, "
              f"{len(papers)} remaining", file=sys.stderr, flush=True)

    # Papers with NO abstract can never resolve, and must not be re-screened
    # forever. The model correctly answers "unknown" on a title-only stub, but
    # "unknown" is a retry value (above) -- deliberately, so a paper IS
    # re-screened once step 2 fetches real text. For a stub that text never
    # arrives, so the row re-queued on every run and was re-screened at cost:
    # 772 papers on long_covid. Skip while the file is still a stub; re-admit
    # automatically the moment it is not.
    prior_no_abstract = _load_no_abstract_dois(output_path)
    stub_skipped = 0
    if prior_no_abstract:
        kept = []
        for d, a, f in papers:
            if d in prior_no_abstract and _is_abstract_stub(a):
                stub_skipped += 1
                continue
            kept.append((d, a, f))
        papers = kept
        if stub_skipped:
            print(f"Skipping {stub_skipped} paper(s) with no abstract text "
                  f"(already screened no_abstract; re-screened automatically "
                  f"if real text arrives)", file=sys.stderr, flush=True)

    if args.limit > 0:
        papers = papers[:args.limit]
    print(f"Processing {len(papers)} files with {args.workers} workers",
          file=sys.stderr, flush=True)

    pregate_re, audit_only = _resolve_pregate(args, config, papers)
    if audit_only:
        return

    fieldnames = [
        "doi", "is_relevant", "relevance_confidence", "studies_treatment",
        "article_type", "type_confidence",
        # The merged initial screen adds the keep/drop decision + an overall
        # certainty that drives adaptive full-text escalation.
        "is_excluded", "exclusion_reason", "certainty",
        # Abstract-level organism guess (human|mouse|other_animal|other); the
        # full-text screen refines it, and non-human papers are excluded by
        # default unless the review opts into animal studies.
        "organism",
    ]

    counter = _run_pipeline(
        papers, output_path, fieldnames, pdf_index={}, prompt_template=prompt_template,
        parse_fn=_parse_quick, parse_kwargs={"valid_types": valid_types},
        use_pdf=False, model=_classify_model(config), workers=args.workers,
        progress_interval=50, pregate_re=pregate_re,
        step="3_classify_quick",
    )

    print(
        f"Done. Classified {counter['done']} abstracts "
        f"({counter.get('pregated', 0)} pre-gated, no LLM call). "
        f"Output: {output_path} | Total cost: ${counter['total_cost']:.4f}",
        file=sys.stderr, flush=True,
    )

    if counter["done"] > 0:
        print("Deduplicating output CSV...", file=sys.stderr, flush=True)
        n = dedup_csv(output_path, id_column="doi")
        print(f"Deduplicated: {n} unique DOIs", file=sys.stderr, flush=True)

    _refile_store(config, output_path)


def _refile_store(config: dict, classifications_csv: Path) -> None:
    """Re-file the paper store to match the classifications just written.

    The type folder is derived from `article_type`, so a re-screen that changes
    a paper's type has to move its folder or the tree drifts out of sync with
    the CSV. Idempotent and cheap (renames only); a project with no store yet is
    a no-op.
    """
    paths = _project_paths(config)
    project_dir = paths["project_dir"]
    root = paper_store.papers_root(project_dir, config=config)
    if not root.is_dir():
        return
    paper_store.remember_root(project_dir, config)
    types = migrate_to_paper_store.load_types(classifications_csv.parent)
    # refile() takes the store lock exclusively and skips (retrying next run)
    # while the PDF queue is downloading or converting into these folders.
    moves = paper_store.refile(project_dir, types, execute=True, root=root,
                               valid_types=config.get("article_types") or None)
    if moves:
        # With the root: without it relink resolved <project>/papers, found
        # nothing under a configured base_pdf_dir, and left every eligible_*
        # link dangling — pass 2 then silently dropped those papers.
        paper_store.relink_buckets(project_dir, root=root)
        print(f"Re-filed {len(moves)} paper(s) into their current type folders",
              file=sys.stderr, flush=True)


# ── Subcommand: escalate (which screened-in papers need full-text re-screen) ──

#: An abstract shorter than this (or the "not available" sentinel) can't be
#: screened confidently — mirrors the `len(text) >= 100` gate in `_read_text`.
MIN_ABSTRACT_CHARS = 100


def _abstract_len_index(abstracts_dir: Path) -> dict[str, int]:
    """doi -> abstract character count, for the short-abstract escalation rule."""
    out: dict[str, int] = {}
    if not abstracts_dir.is_dir():
        return out
    for doi, path, _ in gather_papers([abstracts_dir], recursive=True):
        if path is None:
            out[doi] = 0
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        out[doi] = 0 if "*Abstract not available.*" in text else len(text)
    return out


def select_escalation_dois(screen_csv: Path, abstracts_dir: Path) -> list[str]:
    """DOIs whose abstract screen should be re-run on full text.

    A paper escalates when it is screened IN (is_relevant=yes AND not excluded)
    AND the abstract was insufficient: certainty=low OR the abstract is unusually
    short/missing. The two triggers are INDEPENDENT on purpose — a thin abstract
    can produce false-high certainty, so the length rule is a separate safety net.
    """
    if not screen_csv.exists():
        return []
    lens = _abstract_len_index(abstracts_dir)
    escalate: list[str] = []
    with open(screen_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip()
            if not doi:
                continue
            if (row.get("is_relevant") or "").strip().lower() != "yes":
                continue
            if (row.get("is_excluded") or "no").strip().lower() == "yes":
                continue
            low_certainty = (row.get("certainty") or "").strip().lower() == "low"
            short_abstract = lens.get(doi, 0) < MIN_ABSTRACT_CHARS
            if low_certainty or short_abstract:
                escalate.append(doi)
    return escalate


def cmd_escalate(args):
    """Write the DOI list needing a full-text re-screen (one doi per line)."""
    config = load_config(args.config)
    paths = _project_paths(config)
    screen_csv = (Path(args.input) if args.input
                  else paths["data_dir"] / "classifications.csv")
    dois = select_escalation_dois(screen_csv, paths["abstracts_dir"])
    out_path = (Path(args.output) if args.output
                else paths["data_dir"] / "escalation_dois.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("doi\n")
        for doi in dois:
            f.write(f"{doi}\n")
    print(f"Escalation: {len(dois)} screened-in papers need full-text re-screen "
          f"-> {out_path}", file=sys.stderr, flush=True)


# ── Subcommand: screen ───────────────────────────────────────────────────────

#: How much of each paper the TREATMENT-TRIAL screen reads. The generic
#: classification default is 6,000 chars, which is abstract-sized: a screen
#: sourcing from the paper store reported `source_folder="papers"` while
#: actually seeing only the first ~6k characters, so a trial whose design is
#: stated in Methods could be judged on its introduction alone.
#:
#: Raised because the marginal cost is near zero. Measured on codex_cli/Luna:
#: ~12,400 tokens of fixed CLI context per call against ~2,100 tokens of paper,
#: i.e. 85% overhead. Tripling the paper budget raises total input tokens by
#: ~40%, and that provider bills by subscription, not tokens.
SCREEN_MAX_CHARS = 20000
#: PDF fallback must scale with the char budget or it caps the text first.
SCREEN_MAX_PAGES = 10


def cmd_screen(args):
    config = load_config(args.config)
    paths = _project_paths(config)
    template = load_prompt_template("screen_trials.txt", slug=_project_paths(config)["slug"])
    prompt_template = _build_prompt_screen(config, template)

    # Resolve directories. Explicit --dirs / config screening_dirs are treated as
    # flat folders of papers (non-recursive — legacy behavior for other projects).
    # When neither is set, default to the project's canonical paper store
    # (papers/<Type>/<stem>/), which must be scanned recursively. This makes
    # `screen --config` hands-free and keeps the screened set in sync with
    # whatever full text has been fetched and converted.
    recursive = False
    if args.dirs:
        search_dirs = [Path(d) for d in args.dirs]
    else:
        # Ignored in a studio-managed run: a user-editable config must not
        # point the screen at directories outside the review's workspace.
        cfg_dirs = configured_dir(config, "screening_dirs") or []
        if isinstance(cfg_dirs, str):
            cfg_dirs = [cfg_dirs]
        if cfg_dirs:
            search_dirs = [Path(d) for d in cfg_dirs]
        else:
            store = paper_store.papers_root(paths["project_dir"], config=config)
            if not store.is_dir():
                store = paths["project_dir"] / "converted"   # un-migrated project
            if not store.is_dir():
                print(f"  {RED}ERROR: No directories specified and no paper "
                      f"store at {store}. Use --dirs or set "
                      f"dirs.screening_dirs in config.{RESET}",
                      file=sys.stderr, flush=True)
                sys.exit(1)
            search_dirs = [store]
            recursive = True
            print(f"No --dirs/screening_dirs set; defaulting to paper store "
                  f"{store} (recursive)", file=sys.stderr, flush=True)

    output_path = (Path(args.output) if args.output
                   else paths["data_dir"] / "trial_screening.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Project: {paths['topic_name']!r} -> {paths['project_dir']}",
          file=sys.stderr, flush=True)
    print(f"Output:  {output_path}", file=sys.stderr, flush=True)

    print("Building PDF index...", file=sys.stderr, flush=True)
    pdf_index = build_pdf_index(search_dirs, recursive=recursive)
    print(f"Indexed {len(pdf_index)} PDFs", file=sys.stderr, flush=True)

    papers = gather_papers(search_dirs, include_pdf_only=True, recursive=recursive)
    print(f"Found {len(papers)} papers across {len(search_dirs)} directories",
          file=sys.stderr, flush=True)

    # Pair each paper with its real abstract from the abstracts dir when the
    # screening source doesn't carry one (the converted/ store holds PDFs, not
    # <doi>_abstract.md files), so the screener sees clean abstract + PDF text.
    abstracts_dir = paths["abstracts_dir"]
    abstract_map: dict[str, Path] = {}
    if abstracts_dir.is_dir():
        abstract_map = {d: a for d, a, _ in gather_papers([abstracts_dir],
                                                           recursive=True)}
        papers = [(d, a or abstract_map.get(d), f) for d, a, f in papers]

    # When sourcing from the converted/ store (the hands-free default), also
    # screen relevant papers that have NO PDF yet (download failed/pending) using
    # their abstract alone, so the screened set covers the whole relevant corpus
    # rather than only full-text papers. _read_text falls back to the abstract
    # when no PDF is indexed; these rows are tagged source_folder="abstract_only"
    # so abstract-only (lower-confidence) screens are distinguishable downstream.
    if recursive and abstract_map:
        relevant_csv = (Path(args.relevant_csv) if getattr(args, "relevant_csv", None)
                        else paths["data_dir"] / "classifications.csv")
        relevant_dois = _load_relevant_dois(relevant_csv)
        if relevant_dois:
            have = {d for d, _, _ in papers}
            extra = [(d, abstract_map[d], "abstract_only")
                     for d in relevant_dois
                     if d not in have and d in abstract_map]
            papers.extend(extra)
            print(f"Added {len(extra)} relevant papers w/o full text for "
                  f"abstract-only screening (from {relevant_csv})",
                  file=sys.stderr, flush=True)

    # Resumability
    if args.redo and output_path.exists():
        output_path.unlink()
        print("--redo: cleared previous results", file=sys.stderr, flush=True)
    done_dois = (
        # See the note on the other call sites: the verdict column must be
        # named, or "unknown" rows are wrongly treated as already screened.
        load_done_dois_csv(output_path, id_column="doi",
                           skip_values={"parse_error", "unknown", ""},
                           status_column=_status_columns("is_relevant"))
        if not args.redo else set()
    )
    papers = [(d, a, f) for d, a, f in papers if d not in done_dois]
    if done_dois:
        print(f"Skipping {len(done_dois)} already screened, "
              f"{len(papers)} remaining", file=sys.stderr, flush=True)

    if args.limit > 0:
        papers = papers[:args.limit]
    print(f"Processing {len(papers)} papers with {args.workers} workers\n",
          file=sys.stderr, flush=True)

    if not papers:
        print("Nothing to do.", file=sys.stderr, flush=True)
        return

    fieldnames = [
        "doi", "source_folder", "is_relevant", "studies_treatment",
        "trial_type", "phase", "is_excluded", "exclusion_reason", "topics",
        # abstract | full_text: which text the verdict was made on.
        "text_source",
    ]

    # Custom counter: track treatment trial candidates
    def update_counter(result, counter):
        if (result.get("is_relevant") == "yes"
                and result.get("studies_treatment") == "yes"
                and result.get("is_excluded") != "yes"):
            counter.setdefault("relevant_tx", 0)
            counter["relevant_tx"] = counter.get("relevant_tx", 0) + 1

    counter = _run_pipeline(
        papers, output_path, fieldnames, pdf_index, prompt_template,
        parse_fn=_parse_screen, parse_kwargs={},
        use_pdf=True, model=_classify_model(config), workers=args.workers,
        progress_interval=100, counter_update_fn=update_counter,
        max_chars=args.max_chars, max_pages=args.max_pages,
        step="3_classify_screen",
    )

    # Dedup (preserve extra columns from enrichment)
    print("Deduplicating...", file=sys.stderr, flush=True)
    n = dedup_csv(output_path, id_column="doi", preserve_extra_columns=True)

    candidates = counter.get("relevant_tx", 0)
    print(
        f"\n  Done. {n} papers screened | "
        f"treatment trial candidates: {candidates} | "
        f"cost: ${counter['total_cost']:.2f} | "
        f"tokens: {counter['input_tokens']:,}in/{counter['output_tokens']:,}out",
        file=sys.stderr, flush=True,
    )


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Classify and screen papers using Claude",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Per-subcommand defaults: --input defaults to projects/{slug}/abstracts,
    # --output defaults to projects/{slug}/data/<task-specific>.csv

    # -- relevance --
    p_rel = sub.add_parser("relevance",
                           help="Classify topic relevance (yes/no)")
    p_rel.add_argument("--input", default=None,
                       help="Directory of *_abstract.md files "
                            "(default: projects/{slug}/abstracts)")
    p_rel.add_argument("--output", default=None,
                       help="Output CSV path "
                            "(default: projects/{slug}/data/relevance.csv)")
    p_rel.add_argument("--config", required=True, help="The review's YAML config")
    p_rel.add_argument("--workers", type=int, default=5)
    p_rel.add_argument("--limit", type=int, default=0,
                       help="Limit number of files to process (0 = all)")
    p_rel.add_argument("--no-pregate", action="store_true",
                       help="Disable the keyword pre-gate (classify every abstract via LLM)")
    p_rel.add_argument("--pregate-audit", action="store_true",
                       help="Report how many papers the pre-gate would skip, then exit")

    # -- type --
    p_type = sub.add_parser("type",
                            help="Classify article type")
    p_type.add_argument("--input", default=None,
                        help="Directory of *_abstract.md files "
                             "(default: projects/{slug}/abstracts)")
    p_type.add_argument("--output", default=None,
                        help="Output CSV path "
                             "(default: projects/{slug}/data/classifications.csv)")
    p_type.add_argument("--config", required=True, help="The review's YAML config")
    p_type.add_argument("--workers", type=int, default=10)
    p_type.add_argument("--limit", type=int, default=0,
                        help="Limit number of files to process (0 = all)")

    # -- spray_type (intervention category) --
    p_spray = sub.add_parser("spray_type",
                             help="Classify nasal-spray intervention category")
    p_spray.add_argument("--input", default=None,
                         help="Directory of *_abstract.md files "
                              "(default: projects/{slug}/abstracts)")
    p_spray.add_argument("--output", default=None,
                         help="Output CSV path "
                              "(default: projects/{slug}/data/spray_types.csv)")
    p_spray.add_argument("--relevant-csv", default=None,
                         help="Classifications CSV used to restrict to "
                              "is_relevant=yes papers "
                              "(default: projects/{slug}/data/classifications.csv)")
    p_spray.add_argument("--config", required=True, help="The review's YAML config")
    p_spray.add_argument("--workers", type=int, default=10)
    p_spray.add_argument("--limit", type=int, default=0,
                         help="Limit number of files to process (0 = all)")

    # -- drug_class (treatment drug class + specific drug; spray_type analog) --
    p_drug = sub.add_parser("drug_class",
                            help="Classify treatment drug class + specific drug")
    p_drug.add_argument("--input", default=None,
                        help="Directory of *_abstract.md files "
                             "(default: projects/{slug}/abstracts)")
    p_drug.add_argument("--output", default=None,
                        help="Output CSV path "
                             "(default: projects/{slug}/data/drug_types.csv)")
    p_drug.add_argument("--relevant-csv", default=None,
                        help="Classifications CSV used to restrict to "
                             "is_relevant=yes papers "
                             "(default: projects/{slug}/data/classifications.csv)")
    p_drug.add_argument("--config", required=True, help="The review's YAML config")
    p_drug.add_argument("--workers", type=int, default=10)
    p_drug.add_argument("--limit", type=int, default=0,
                        help="Limit number of files to process (0 = all)")

    # -- study_design (granular clinical + preclinical design) --
    p_design = sub.add_parser("study_design",
                              help="Classify granular study design (clinical + preclinical)")
    p_design.add_argument("--input", default=None,
                          help="Directory of *_abstract.md files "
                               "(default: projects/{slug}/abstracts)")
    p_design.add_argument("--output", default=None,
                          help="Output CSV path "
                               "(default: projects/{slug}/data/study_designs.csv)")
    p_design.add_argument("--relevant-csv", default=None,
                          help="Classifications CSV used to restrict to "
                               "is_relevant=yes papers "
                               "(default: projects/{slug}/data/classifications.csv)")
    p_design.add_argument("--config", required=True, help="The review's YAML config")
    p_design.add_argument("--workers", type=int, default=10)
    p_design.add_argument("--limit", type=int, default=0,
                          help="Limit number of files to process (0 = all)")

    # -- indication (organism + prevention/treatment) --
    p_ind = sub.add_parser("indication",
                           help="Classify organism (human/animal/in-vitro/...) "
                                "and indication (prevention vs treatment)")
    p_ind.add_argument("--input", default=None,
                       help="Directory of *_abstract.md files "
                            "(default: projects/{slug}/abstracts)")
    p_ind.add_argument("--output", default=None,
                       help="Output CSV path "
                            "(default: projects/{slug}/data/indications.csv)")
    p_ind.add_argument("--relevant-csv", default=None,
                       help="Classifications CSV used to restrict to "
                            "is_relevant=yes papers "
                            "(default: projects/{slug}/data/classifications.csv)")
    p_ind.add_argument("--config", required=True, help="The review's YAML config")
    p_ind.add_argument("--workers", type=int, default=10)
    p_ind.add_argument("--limit", type=int, default=0,
                       help="Limit number of files to process (0 = all)")

    # -- quick (combined relevance + type) --
    p_quick = sub.add_parser("quick",
                             help="Combined relevance + article-type in one call")
    p_quick.add_argument("--input", default=None,
                         help="Directory of *_abstract.md files "
                              "(default: projects/{slug}/abstracts)")
    p_quick.add_argument("--output", default=None,
                         help="Output CSV path "
                              "(default: projects/{slug}/data/classifications.csv)")
    p_quick.add_argument("--config", required=True, help="The review's YAML config")
    p_quick.add_argument("--workers", type=int, default=10)
    p_quick.add_argument("--limit", type=int, default=0,
                         help="Limit number of files to process (0 = all)")
    p_quick.add_argument("--no-pregate", action="store_true",
                         help="Disable the keyword pre-gate (classify every abstract via LLM)")
    p_quick.add_argument("--pregate-audit", action="store_true",
                         help="Report how many papers the pre-gate would skip, then exit")

    # -- screen --
    p_screen = sub.add_parser("screen",
                              help="Screen for treatment/intervention trials")
    p_screen.add_argument("--dirs", nargs="+", default=None,
                          help="Directories to screen (overrides config)")
    p_screen.add_argument("--output", default=None,
                          help="Output CSV path "
                               "(default: projects/{slug}/data/trial_screening.csv)")
    p_screen.add_argument("--config", required=True, help="The review's YAML config")
    p_screen.add_argument("--workers", type=int, default=5)
    p_screen.add_argument("--max-chars", dest="max_chars", type=int,
                          default=SCREEN_MAX_CHARS,
                          help=f"Characters of each paper to send (default "
                               f"{SCREEN_MAX_CHARS}). The generic classifier "
                               f"default is 6000, which is abstract-sized.")
    p_screen.add_argument("--max-pages", dest="max_pages", type=int,
                          default=SCREEN_MAX_PAGES,
                          help=f"PDF pages to read when falling back to the PDF "
                               f"(default {SCREEN_MAX_PAGES}).")
    p_screen.add_argument("--limit", type=int, default=0,
                          help="Limit number of papers to process (0 = all)")
    p_screen.add_argument("--redo", action="store_true",
                          help="Re-screen all papers, ignoring previous results")
    p_screen.add_argument("--relevant-csv", default=None,
                          help="Classifications CSV; when screening the converted/ "
                               "store, relevant papers without a PDF are also "
                               "screened on abstract alone "
                               "(default: projects/{slug}/data/classifications.csv)")

    # -- escalate --
    p_esc = sub.add_parser(
        "escalate",
        help="List screened-in papers whose abstract was insufficient "
             "(low certainty or too short) and need a full-text re-screen")
    p_esc.add_argument("--input", default=None,
                       help="Merged screen CSV "
                            "(default: projects/{slug}/data/classifications.csv)")
    p_esc.add_argument("--output", default=None,
                       help="DOI list output "
                            "(default: projects/{slug}/data/escalation_dois.csv)")
    p_esc.add_argument("--config", required=True, help="The review's YAML config")

    args = parser.parse_args()

    if args.command == "relevance":
        cmd_relevance(args)
    elif args.command == "type":
        cmd_type(args)
    elif args.command == "spray_type":
        cmd_spray_type(args)
    elif args.command == "drug_class":
        cmd_drug_class(args)
    elif args.command == "study_design":
        cmd_study_design(args)
    elif args.command == "indication":
        cmd_indication(args)
    elif args.command == "quick":
        cmd_quick(args)
    elif args.command == "screen":
        cmd_screen(args)
    elif args.command == "escalate":
        cmd_escalate(args)


if __name__ == "__main__":
    main()
    # A rejected credential (expired setup-token, revoked API key) is fatal and
    # unrecoverable — but every per-paper worker catches Exception so one bad
    # paper cannot abort a run, which would turn this into "every paper failed"
    # and a successful-looking run that produced nothing. claude_cli latches the
    # failure and short-circuits the remaining calls; this converts that into a
    # non-zero exit, from the main thread, once writes have flushed.
    raise_if_auth_failed()
