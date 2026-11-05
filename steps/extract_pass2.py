#!/usr/bin/env python3
"""Pass 2: extract full quantitative trial data for meta-analysis.

Runs bounded model calls per eligible paper to extract the detailed schema the
dashboard and pooling need: per-arm results, between-group effects + CIs, risk of
bias, sample sizes. Supports both RCT and observational designs via --design-type
(merges the previously separate ..._rct.py / ..._observational.py scripts).

Reads:
    Staged eligible paper text under projects/{slug}/ for the chosen design bucket
    (resolve_input_dir + discover_papers + read_best_text). Design-specific prompt
    template prompts/[{slug}/]extract_pass2_{design_type}.txt.
Writes:
    RCT -> projects/{slug}/data/trial_extractions_pass2.jsonl;
    observational -> projects/{slug}/data/observational_extractions.jsonl
    (append mode; --tag suffixes the filename). Per-record fields include _status
    ("ok" | "no_text" | "low_quality_text" — boilerplate-only converted text |
    "parse_error" | "policy_skip" | "refused" — prose refusal, no retries |
    "ineligible" — schema-compliant all-null extraction, the model's structured
    way of declining; "truncated" output triggers a retry-resume), _cost,
    _input_tokens, _output_tokens.
Models:
    config.models.extraction_pass2 (default "sonnet"; overridable via --model);
    CLI_TIMEOUT = 600s per call, MAX_RETRIES = 3.
Resumability:
    Skips paper_ids already present in the output jsonl (load_done_ids_jsonl).
Side effects:
    Appends to the output jsonl under a write lock (parallel ThreadPoolExecutor);
    writes Claude call logs via set_log_dest_dir; prints progress to stderr.
"""
import argparse
import re
import hashlib
import json
import os
import sys
import threading
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.claude_cli import (
    call_claude, parse_json_response, is_truncated, is_refusal, has_json_continuation,
    raise_if_auth_failed, run_halted, set_log_dest_dir, resolve_model, effective_provider,
)
from ber_core.quote_verification import verify_record
from ber_core.utils import (
    load_config, load_prompt_template, ensure_format_note_placeholder,
    load_done_ids_jsonl, requeue_no_text, configured_dir,
    log_status, log_progress, emit_run_summary, RED, YELLOW, RESET,
    project_paths, resolve_input_dir, discover_papers, read_best_text, PaperRef,
    xml_plain_text,
)
from ber_core.constants import format_note_for
from ber_core.extraction_inputs import (
    build_extraction_inputs, merge_extraction_records, stamp_extraction_source,
    MAX_BATCHES, ExtractionInputs, InputBatch,
)
from ber_core.conversion import read_conversion_quality

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLI_TIMEOUT = 600  # longer for Pass 2 -- larger output expected
MAX_RETRIES = 3
#: Character budget for the XML branch, separate from the 80k markdown default.
#: Serialized markup is tag-heavier per unit of content, and a shared number
#: would truncate XML far earlier than markdown for the same paper. Measured on
#: a real Elsevier corpus: full-text records land at 60-160k chars once
#: references and front matter are pruned, so this fits whole papers.
XML_MAX_CHARS = 180_000

_RETRY_RESUME_PROMPT = """Your previous JSON output was incomplete. Please output the COMPLETE, valid JSON object again from scratch.
Preserve the originally requested extraction scope, schema types, outcomes,
comparisons, timepoints, subgroup results, and quantitative values. Do not reduce
coverage to make the response shorter. Keep narrative explanations concise and
avoid repeated prose, while retaining all requested data and provenance.
The JSON MUST be complete and valid. Do not truncate."""

# For a NON-truncated parse failure. The resume prompt above asserts the output
# was too long, which is false here — and a model that (rightly) never produced
# JSON pushes back on being told its "previous JSON output" was malformed.
_RETRY_MALFORMED_PROMPT = """Your previous response could not be parsed as a \
JSON object. Respond with ONLY the complete, valid JSON object for the \
extraction — no prose, no markdown fences, nothing before or after it. If the \
paper is not an extractable trial, follow the schema instructions for \
inapplicable fields (null / empty arrays) rather than explaining in prose."""

_CONDENSED_ADDENDUM = _RETRY_RESUME_PROMPT

# Module-level state set from CLI args in main()
_MODEL = "sonnet"
_WRITE_LOCK = threading.Lock()
_OUTPUT_PATH: Path | None = None

#: Concurrent extraction workers when neither --workers nor config sets one.
#: Each worker is one full-text Sonnet call; the studio surfaces this as a
#: per-review `extraction_workers` config key on the Extract stage.
from ber_core.defaults import DEFAULT_EXTRACTION_WORKERS


# ---------------------------------------------------------------------------
# Configurable extraction blocks (extraction_pass2_fields)
# ---------------------------------------------------------------------------
# Which schema sections the model is asked to fill. The full schema stays in the
# template (so the JSON example never breaks); when a block is DISABLED we append
# an instruction telling the model to skip it (leave it null / empty). This cuts
# the expensive OUTPUT tokens — the per-arm / per-timepoint data the model
# generates — which is where pass-2 cost lives. Whole-schema stripping was
# rejected: it breaks the comma-separated JSON example the model reads.
#
# Three tiers, mapped to a user-facing group on the Extract page:
#   CORE      — the dashboard/evidence contract (dashboard_summary.project_row):
#               study design, PICO, sample size, the PRIMARY outcome + its
#               between-group effect (→ the positive/negative/no-effect verdict),
#               and risk of bias. ALWAYS emitted regardless of flags — disabling
#               one would break the results table + verdict, so the UI warns but
#               the prompt still asks for it.
#   STANDARD  — useful but droppable: baseline demographics, adverse events,
#               follow-up detail, study-design methodology.
#   DEEP      — the meta-analysis payload, default OFF: ALL outcomes (secondaries
#               too) across every arm and timepoint, per-arm sample detail,
#               subgroup analyses, and the verbatim provenance quotes.
#
# Each optional/deep block maps to a "skip instruction" appended to the prompt
# when the block is off. The instruction names the exact schema field(s).
_PASS2_SKIP_INSTRUCTIONS: dict[str, str] = {
    # STANDARD (default on)
    "baseline_demographics":
        'Leave "baseline_demographics" as null — do not extract per-arm ages, sex, BMI, or comorbidities.',
    "adverse_events":
        'Leave "adverse_events" as null — do not extract adverse-event counts.',
    "follow_up":
        'Leave "follow_up" as null — do not extract follow-up durations.',
    "study_design_methodology":
        'In "study_design", leave the methodology detail fields null (randomization_method, '
        'allocation_concealment, stratification_factors, data_collection, matching_method, '
        'adjustment_method, adjustment_variables). Still fill design_type, arms, blinding, setting, countries.',
    # DEEP (default off)
    "all_outcomes":
        'Extract ONLY the PRIMARY outcome(s) in "outcomes" — omit secondary outcomes entirely. '
        'For the primary outcome, extract only the final/end-of-treatment timepoint (skip intermediate '
        'timepoints) and its between_group_effects; you may omit per-arm arm_results if a between-group '
        'effect with a p-value is reported.',
    "per_arm_sample_detail":
        'In "sample_sizes.per_arm", extract only n_randomized/n_enrolled per arm — omit n_completed, '
        'n_analyzed_primary, analysis_population, withdrawals, and withdrawal_reasons.',
    "subgroup_analyses":
        'Leave "subgroup_analyses" as an empty array [] — do not extract subgroups.',
    "provenance_quotes":
        'Set every "*_quote" field to null — do not copy verbatim source quotes. (Still fill the '
        'numeric values and their *_source fields.)',
}

# Core blocks are listed for the UI/tests; they are never skipped in the prompt.
PASS2_CORE_BLOCKS: tuple[str, ...] = (
    "study_design", "participants", "sample_sizes", "primary_outcomes", "risk_of_bias",
)

# Default on/off. Core blocks are implicitly always-on (not listed). Standard on,
# deep off — the "basic extraction" the user gets by default.
from ber_core.defaults import PASS2_FIELD_DEFAULTS


def resolve_pass2_field_flags(cfg: dict) -> dict[str, bool]:
    """Merge the review's `extraction_pass2_fields` over the defaults."""
    flags = dict(PASS2_FIELD_DEFAULTS)
    flags.update(cfg.get("extraction_pass2_fields") or {})
    return flags


def _block_skip_block(field_flags: dict[str, bool]) -> str:
    """The 'skip these blocks' instruction appended to the prompt, or '' if the
    full (all-blocks-on) extraction is requested. Core blocks are never skippable."""
    skips = [
        instr for block, instr in _PASS2_SKIP_INSTRUCTIONS.items()
        if not field_flags.get(block, PASS2_FIELD_DEFAULTS.get(block, True))
    ]
    if not skips:
        return ""
    body = "\n".join(f"- {s}" for s in skips)
    return (
        "\n\nEXTRACTION SCOPE — this review has narrowed what to extract. "
        "Follow these exactly (they override the schema example above):\n" + body
    )


def _custom_questions_block(cfg: dict) -> str:
    """The review's free-form extraction questions (`custom_extraction_questions`),
    rendered as an instruction to fill a top-level ``custom_answers`` array, or ''
    when none are configured. The block is brace-escaped because it is appended to
    the user template BEFORE the per-paper ``.format()`` — a question containing
    ``{`` would otherwise raise KeyError (or silently swallow text) at format time.
    """
    questions = [q.strip() for q in (cfg.get("custom_extraction_questions") or [])
                 if isinstance(q, str) and q.strip()]
    if not questions:
        return ""
    body = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    block = (
        "\n\nCUSTOM QUESTIONS — this review also asks the following questions about "
        "each paper. Add a top-level \"custom_answers\" field to the JSON object: an "
        "array with one entry per question, in order, each shaped "
        '{"question": "<the question, verbatim>", "answer": "<a concise answer '
        'grounded in this paper\'s text>"}. If the paper does not address a '
        "question, set its \"answer\" to null — do not guess.\n" + body
    )
    return block.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def _load_prompt_parts(
    design_type: str, cfg: dict, slug: str | None = None,
    field_flags: dict | None = None,
) -> tuple[str, str]:
    """Load and fill prompt template for the given design type.

    Returns (system_prompt, user_prompt_template) where user_prompt_template
    still has single-braced {full_text} and {paper_id} placeholders for
    per-paper filling via str.format (JSON literal braces stay doubled).

    If *slug* is given and ``prompts/{slug}/extract_pass2_{design_type}.txt``
    exists, that per-project override is used (mirrors classify.py); otherwise
    the shared template is loaded.

    ``field_flags`` (from ``extraction_pass2_fields``) decides which OPTIONAL /
    DEEP blocks the model is told to skip; core blocks are always extracted. The
    skip instruction is appended to the user prompt AFTER the schema example, so
    the JSON the model sees stays valid. When None, the full extraction is used.
    """
    template_name = f"extract_pass2_{design_type}.txt"
    raw = ensure_format_note_placeholder(load_prompt_template(template_name, slug=slug))

    # Fill config-level placeholders
    topic = cfg.get("topic", {})
    topic_name = topic.get("name", "the condition")
    intervention_cats = cfg.get("intervention_categories", [])
    intervention_categories_str = " | ".join(intervention_cats) if intervention_cats else "other"
    outcome_domains = cfg.get("outcome_domains", [])
    outcome_domains_str = " | ".join(outcome_domains) if outcome_domains else "other"
    # Per-topic name for the "how was the condition defined" participants field.
    # Defaults to the long_covid key so existing topics are unaffected.
    topic_condition_field = topic.get("condition_field", "long_covid_definition")

    raw = raw.replace("{topic_name}", topic_name)
    raw = raw.replace("{intervention_categories_str}", intervention_categories_str)
    raw = raw.replace("{outcome_domains_str}", outcome_domains_str)
    raw = raw.replace("{topic_condition_field}", topic_condition_field)

    # Split SYSTEM: and USER: sections
    if "SYSTEM:" in raw and "USER:" in raw:
        parts = raw.split("USER:", 1)
        system_prompt = parts[0].replace("SYSTEM:", "").strip()
        user_template = parts[1].strip()
    else:
        # Fallback: treat entire thing as a combined prompt
        system_prompt = ""
        user_template = raw

    # Append the extraction-scope instruction (blocks the review turned off).
    # Contains no {placeholders}, so it survives the later per-paper .format().
    if field_flags is not None:
        user_template += _block_skip_block(field_flags)

    # Append the review's free-form questions (brace-escaped inside the helper).
    user_template += _custom_questions_block(cfg)

    return system_prompt, user_template


# ---------------------------------------------------------------------------
# JSONL writing
# ---------------------------------------------------------------------------

def _write_result(result: dict) -> None:
    """Append one JSON record to output JSONL, flushed to disk immediately."""
    with _WRITE_LOCK:
        with open(_OUTPUT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
            f.flush()
            os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_extraction(response: str, paper_id: str) -> dict | None:
    """Parse Claude's response into an extraction dict. Returns None on parse failure."""
    if is_truncated(response):
        print(f"  {YELLOW}WARN: Truncated response for {paper_id} (output too large){RESET}",
              file=sys.stderr, flush=True)
        return None
    # PDF glyphs can survive as literal control characters in quoted evidence.
    # Decode the outer object before the generic reader scans nested objects.
    # strict=False permits those characters INSIDE strings; it does not repair
    # missing delimiters or invent/truncate values. json.dumps escapes them on
    # disk, and the original quote is retained for source verification.
    text = re.sub(r"```(?:json)?\s*", "", response).strip()
    parsed = None
    start = text.find("{")
    if start >= 0:
        try:
            outer, end = json.JSONDecoder(strict=False).raw_decode(text, start)
            if has_json_continuation(text, end):
                return None
            if isinstance(outer, dict):
                parsed = outer
                try:
                    json.JSONDecoder().raw_decode(text, start)
                except ValueError:
                    parsed.setdefault("_parse_diagnostics", []).append(
                        "literal_control_characters_preserved_in_strings")
        except ValueError:
            pass
    if parsed is None:
        parsed = parse_json_response(response, paper_id)
    if parsed is None:
        return None
    if not any(key in parsed for key in _CORE_SECTIONS):
        # The generic JSON reader can recover a nested object when the outer
        # payload is malformed. A lone arm/design object is not an explicit
        # all-null extraction and must never become an eligibility verdict.
        print(f"  {YELLOW}WARN: Missing extraction sections for {paper_id}{RESET}",
              file=sys.stderr, flush=True)
        return None
    parsed["paper_id"] = paper_id
    return parsed


_CORE_SECTIONS = ("study_design", "sample_sizes", "outcomes")


def _is_null_extraction(parsed: dict) -> bool:
    """Schema-compliant all-nulls = the model's structured way of declining.

    The prompt tells the model to use null/empty for inapplicable fields, so a
    paper with no extractable trial data legitimately comes back as
    ``{"study_design": null, "sample_sizes": null, "outcomes": [], ...}``.
    That is a verdict ("nothing here"), not data — record it as ineligible
    instead of crashing on the nulls or counting it as an extraction.

    A list of nulls (``"outcomes": [null]``) is the same verdict wearing a
    truthy shape: a bare ``not parsed.get(k)`` test reads it as content, so the
    record was billed as an extraction and carried empty sections downstream.
    Null elements are stripped before the emptiness test.
    """
    def _empty(v):
        if isinstance(v, list):
            return not [x for x in v if x]
        return not v

    return all(_empty(parsed.get(k)) for k in _CORE_SECTIONS)


def _declared_extraction_omissions(record: dict) -> list[str]:
    """Retain explicit model admissions that the returned extraction was shortened."""
    records = [record] + [part.get("record", {})
                          for part in record.get("_batch_metadata", [])]
    pattern = re.compile(
        r"\b(?:outcome(?:s| array| data)?|schema outcome array|extraction|results)\s+"
        r"(?:(?:have been|has been|were|was|is|are)\s+)?"
        r"(?:abbreviated|truncated|abridged)\b|"
        r"\bonly\s+(?:selected|representative)\s+(?:category\s+)?"
        r"(?:outcomes|results)\s+(?:are|were)\s+(?:represented|included|extracted)\b", re.I)
    notes = [(item.get("extraction_metadata") or {}).get("extractor_notes") or ""
             for item in records]
    return list(dict.fromkeys(note for note in notes
                             if isinstance(note, str) and pattern.search(note)))


# ---------------------------------------------------------------------------
# Postprocessing / validation
# ---------------------------------------------------------------------------

def _postprocess(parsed: dict, paper_id: str, text_source: str,
                 design_type: str) -> dict:
    """Validate and add warnings after extraction.

    Tolerates explicit JSON nulls everywhere: ``parsed.get(k, {})`` returns the
    stored None when the model emitted ``"k": null``, so every section access
    goes through ``or {}`` / ``or []``.
    """
    warnings = []

    # Record text source
    if text_source == "pdf":
        warnings.append("used_pdf_fallback_text")
        meta = parsed.get("extraction_metadata") or {}
        notes = meta.get("extractor_notes") or ""
        meta["extractor_notes"] = f"MD was garbled, used PDF text. {notes}".strip()
        parsed["extraction_metadata"] = meta

    # Check arm_id referential integrity.
    #
    # Every list walked here can contain a null ELEMENT, not just be null
    # itself: a model declining a paper it judges unextractable emits
    # schema-shaped output like `"outcomes": [null]`, which is valid JSON and
    # truthy, so it slips past both `or []` and `_is_null_extraction`. Before
    # the isinstance guards that crashed with "'NoneType' object has no
    # attribute 'get'" AFTER the paid call — and, because `load_done_ids_jsonl`
    # treats the resulting `_status: "error"` as not-done, the paper was
    # re-extracted and re-billed on every subsequent run.
    design_arm_ids = set()
    for arm in (parsed.get("study_design") or {}).get("arms") or []:
        if not isinstance(arm, dict):
            continue
        aid = arm.get("arm_id")
        if aid is not None:
            design_arm_ids.add(aid)

    for section_name in ("sample_sizes", "baseline_demographics", "adverse_events"):
        section = parsed.get(section_name) or {}
        per_arm = section.get("per_arm") or []
        for entry in per_arm:
            if not isinstance(entry, dict):
                continue
            aid = entry.get("arm_id")
            if aid is not None and aid not in design_arm_ids:
                warnings.append(f"orphan_arm_id={aid}_in_{section_name}")

    for outcome in parsed.get("outcomes") or []:
        if not isinstance(outcome, dict):
            continue
        for ar in outcome.get("arm_results") or []:
            if not isinstance(ar, dict):
                continue
            aid = ar.get("arm_id")
            if aid is not None and aid not in design_arm_ids:
                warnings.append(f"orphan_arm_id={aid}_in_outcome_{outcome.get('outcome_id')}")

    # Check sample size consistency -- design-type-specific field names
    ss = parsed.get("sample_sizes") or {}
    per_arm = [a for a in (ss.get("per_arm") or []) if isinstance(a, dict)]

    if design_type == "rct":
        from ber_core.scientific import design
        total = ss.get("n_randomized_total")
        # Treatment exposures overlap in crossover trials; adding them does
        # not give the number of randomized people.
        if total is not None and per_arm and design(parsed) != "crossover":
            arm_sum = sum(a.get("n_randomized") or 0 for a in per_arm)
            if arm_sum > 0 and abs(arm_sum - total) > 2:
                warnings.append(f"n_randomized_mismatch: total={total} sum_arms={arm_sum}")
    else:  # observational
        total = ss.get("n_enrolled_total")
        if total is not None and per_arm:
            arm_sum = sum(a.get("n_enrolled") or 0 for a in per_arm)
            if arm_sum > 0 and abs(arm_sum - total) > 2:
                warnings.append(f"n_enrolled_mismatch: total={total} sum_arms={arm_sum}")

    # Flag empty outcomes
    outcomes = [o for o in (parsed.get("outcomes") or []) if isinstance(o, dict)]
    if not outcomes:
        warnings.append("no_outcomes_extracted")
    else:
        n_with_data = sum(1 for o in outcomes if o.get("arm_results"))
        if n_with_data == 0:
            warnings.append("outcomes_have_no_arm_results")

    # Flag if no between-group effects anywhere
    has_effects = any(
        o.get("between_group_effects")
        for o in outcomes
    )
    if outcomes and not has_effects:
        warnings.append("no_between_group_effects_extracted")

    if warnings:
        parsed["_warnings"] = warnings
    return parsed


# ---------------------------------------------------------------------------
# Per-paper extraction
# ---------------------------------------------------------------------------

class _Extraction(NamedTuple):
    """One prompt run, all the way through its parse retries."""
    result: dict | None
    status: str | None       # terminal verdict the caller must honor, else None
    reason: str
    cost: float
    elapsed: float
    input_tokens: int
    output_tokens: int


def _batch_identity_context(result: dict, max_chars: int = 16000) -> tuple[str, dict | None]:
    """Keep arm identities when a paper has too many outcomes for the context.

    Omitting all context made later sections redefine the same arms without
    their dose or duration, leaving valid results unmerged. Keep complete
    outcome identities that fit and explicitly name those that do not; never
    truncate JSON or silently discard the study/participant definitions.
    """
    fields = ("outcome_id", "name", "measurement_instrument", "unit",
              "outcome_concept", "synthesis_population", "analysis_basis",
              "analysis_population", "timepoints", "primary_timepoint_id")
    outcomes = [{k: o.get(k) for k in fields}
                for o in (result.get("outcomes") or []) if isinstance(o, dict)]
    context = {"study_design": result.get("study_design"),
               "participants": result.get("participants"), "outcomes": outcomes}
    def encode():
        return json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    text = encode()
    if len(text) <= max_chars:
        return text, None
    context["outcomes"] = []
    omitted = []
    for outcome in outcomes:
        context["outcomes"].append(outcome)
        if len(encode()) > max_chars:
            context["outcomes"].pop()
            omitted.append(outcome.get("outcome_id"))
    # The source remains complete; it is only earlier identity hints that are
    # bounded. The prompt already requires missing identities from the source.
    text = encode()
    return text, {"code": "batch_outcome_context_omitted",
                  "omitted_outcome_ids": omitted,
                  "retained_outcomes": len(context["outcomes"]),
                  "study_context_exceeds_allowance": len(text) > max_chars}


def _run_extraction(prompt: str, paper_id: str, seq: int, total: int,
                    read_files: list[Path] | None = None) -> _Extraction:
    """One extraction attempt on one prompt, with the JSON-parse retry loop.

    Factored out of ``extract_one`` so the source-fallback chain can run it more
    than once per paper against different text. Pure with respect to the paper:
    it neither writes results nor decides the paper's fate — it reports what the
    model said and what it cost.

    ``read_files`` hands the model the file itself instead of extracted text
    (see the pdf-native rung); providers that cannot do that say so and the
    caller falls back.
    """
    cost = elapsed = 0.0
    input_tokens = output_tokens = 0
    result = None
    session_id = None
    last_truncated = False

    for attempt in range(MAX_RETRIES):
        if attempt == 0:
            # First attempt: fresh call, prompt sent via stdin
            resp = call_claude(prompt, _MODEL, paper_id, timeout=CLI_TIMEOUT,
                               use_stdin=True, read_files=read_files)
            session_id = resp.session_id
        elif session_id:
            # Retry: resume the same session so the model already has context.
            # The prompt must be honest about WHY we're retrying — telling a
            # model that never produced JSON that its "JSON was malformed"
            # just makes it argue back for two more full-price rounds.
            retry_prompt = (_RETRY_RESUME_PROMPT if last_truncated
                            else _RETRY_MALFORMED_PROMPT)
            resp = call_claude(retry_prompt, _MODEL, paper_id,
                               timeout=CLI_TIMEOUT, use_stdin=True,
                               resume_session=session_id)
        else:
            # Stateless retries retain the source and original scope. Empty
            # timeouts and malformed replies do not prove output truncation.
            retry_prompt = (_CONDENSED_ADDENDUM if last_truncated
                            else _RETRY_MALFORMED_PROMPT)
            resp = call_claude(prompt + "\n\n" + retry_prompt, _MODEL, paper_id,
                               timeout=CLI_TIMEOUT, use_stdin=True,
                               read_files=read_files)
            session_id = resp.session_id

        elapsed += resp.elapsed
        cost += resp.cost_usd
        input_tokens += resp.input_tokens
        output_tokens += resp.output_tokens
        if run_halted():
            # Credential rejected / usage stop: the empty reply is not the
            # model's answer, so it must not become a parse_error verdict.
            return _Extraction(None, "halted", "", cost, elapsed,
                               input_tokens, output_tokens)
        if resp.is_policy_error:
            return _Extraction(None, "policy_skip", "", cost, elapsed,
                               input_tokens, output_tokens)
        result = _parse_extraction(resp.text, paper_id)
        if result is not None:
            break
        # An empty transport/timeout response contains no evidence of a
        # truncated JSON object. Keep that retry distinct from partial output.
        last_truncated = bool(resp.text.strip()) and is_truncated(resp.text)
        if not last_truncated and is_refusal(resp.text):
            # The model declined the task in prose (wrong paper for the
            # schema, empty text, …). A retry re-buys the same answer, so
            # record the verdict and stop paying.
            return _Extraction(None, "refused", resp.text.strip()[:500],
                               cost, elapsed, input_tokens, output_tokens)
        if attempt < MAX_RETRIES - 1:
            reason = "truncated" if last_truncated else "parse error"
            method = "resuming session" if session_id else "fresh call"
            log_status(seq, total, "RETRY",
                       f"attempt {attempt + 2}/{MAX_RETRIES} ({reason}, {method})")
            time.sleep(2)

    return _Extraction(result, None, "", cost, elapsed, input_tokens, output_tokens)


#: Per-arm keys that mean "this extraction actually captured numbers".
_ARM_VALUE_KEYS = ("mean", "sd", "se", "median", "events", "total", "percent",
                   "n", "value")


def has_outcome_data(rec: dict) -> bool:
    """Did this extraction capture pool-able numbers?

    Deliberately stricter than ``_is_null_extraction``, which asks whether
    study_design/sample_sizes/outcomes are ALL empty. The failure this predicate
    exists for is the opposite shape: a well-formed record with a design, arms
    and named outcomes whose ``arm_results`` and ``between_group_effects`` are
    all null — the signature of a source whose TABLES were lost, which is
    exactly what the XML rung is meant to rescue.
    """
    for outcome in (rec.get("outcomes") or []):
        if not isinstance(outcome, dict):
            continue
        for effect in (outcome.get("between_group_effects") or []):
            if isinstance(effect, dict) and effect.get("effect_value") is not None:
                return True
        for arm in (outcome.get("arm_results") or []):
            if isinstance(arm, dict) and any(
                    arm.get(k) is not None for k in _ARM_VALUE_KEYS):
                return True
    return False


def _source_file_for(ref: PaperRef, source: str) -> str | None:
    """The file an extraction actually read, for the record's audit trail."""
    path = {"md": ref.md_path, "xml": ref.xml_path,
            "pdf": ref.pdf_path, "pdf-native": ref.pdf_path}.get(source)
    return path.name if path else None


def _archive_fallback_candidate(record: dict, source: str | None,
                                source_manifest: dict) -> list[dict]:
    """Retain the earlier candidate without promoting or remapping its results.

    A no-poolable-numbers result can still contain p-values, within-group
    changes, safety results or unmerged numeric supplement results. Make every
    nested unmerged candidate separately visible to outcome-level source
    reconciliation while preserving the complete original diagnostic tree.
    """
    archived, seen = [], set()

    def retain(candidate, source, prior_entry=None):
        saved = deepcopy(candidate)
        digest = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        entry = deepcopy(prior_entry or {})
        entry.update(source=source, record=saved, record_sha256=digest)
        if prior_entry is None:
            entry.update(reason="source_fallback_requires_reconciliation",
                         source_manifest=deepcopy(source_manifest))
        archived.append(entry)
        for previous in candidate.get("_unmerged_extractions") or []:
            if isinstance(previous, dict) and isinstance(previous.get("record"), dict):
                retain(previous["record"], previous.get("source"), previous)

    retain(record, source)
    return archived


def _fallback_sources(ref: PaperRef, used: str):
    """The rungs still worth trying after *used* produced no numbers.

    Ordered best-first and yielded lazily, so each is only paid for if the one
    before it also came up empty:

      xml -> body.md      the XML's table was published as an image; the PDF
                          conversion may have OCR'd it
      any -> pdf-native   the model reads the PDF's pages as images itself,
                          which is the only rung that can read a table no text
                          extraction ever captured

    Yields ``(text, source, read_files)``; ``read_files`` is None except for the
    native rung, where the model is handed the file instead of extracted text.
    Never re-offers the source that already ran, and never walks UP (an md->xml
    retry would be re-reading a source the chain already preferred).
    """
    if used == "xml" and ref.md_path and ref.md_path.exists():
        text, source = read_best_text(ref, xml_first=False, reject_boilerplate=True,
                                      max_chars=None)
        if text and source == "md":
            yield text, source, None

    if used != "pdf-native" and ref.pdf_path and ref.pdf_path.exists():
        # The prompt carries no document text: the model reads the file.
        yield (f"(The paper is the PDF at {ref.pdf_path.name}. Read it before "
               f"answering — it is attached to this conversation.)",
               "pdf-native", [ref.pdf_path])


#: Summary for a paper not attempted because the run halted (credential
#: rejected or usage stop). Nothing is written for it, so it simply runs again.
_HALTED = {"_status": "halted", "_cost": 0.0, "_input_tokens": 0,
           "_output_tokens": 0, "n_outcomes": 0, "n_effects": 0}


def extract_one(ref: PaperRef, seq: int, total: int,
                system_prompt: str, user_template: str,
                design_type: str, xml_first: bool = True,
                retry_on_empty: bool = True, all_outcomes: bool = False,
                max_input_batches: int = MAX_BATCHES,
                input_max_chars: int | None = None,
                input_packaging: str = "table-aware") -> dict:
    """Extract data from a single paper. Writes result to JSONL immediately.

    Returns a lightweight summary dict for progress tracking (not the full result).
    """
    paper_id = ref.paper_id
    if run_halted():
        return dict(_HALTED)

    cost_usd = 0.0
    elapsed = 0.0
    input_tokens = 0
    output_tokens = 0

    full_text, text_source = read_best_text(ref, xml_first=xml_first,
                                            reject_boilerplate=True,
                                            max_chars=None)
    if full_text is None:
        if text_source == "boilerplate":
            # Library download stamps / paywall covers: >200 chars of "text"
            # with zero article content. Distinct status so it's auditable.
            log_status(seq, total, "SKIP", f"boilerplate-only text  {ref.stem}")
            _write_result({"paper_id": paper_id, "_status": "low_quality_text"})
            return {"_status": "low_quality_text", "_cost": 0.0,
                    "_input_tokens": 0, "_output_tokens": 0,
                    "n_outcomes": 0, "n_effects": 0}
        log_status(seq, total, "SKIP", f"no extractable text  {ref.stem}")
        _write_result({"paper_id": paper_id, "_status": "no_text"})
        return {"_status": "no_text", "_cost": 0.0, "_input_tokens": 0,
                "_output_tokens": 0, "n_outcomes": 0, "n_effects": 0}

    def _inputs(text, source, include_supplementary=True):
        if input_packaging == "legacy":
            # An explicit benchmark control: reproduce the historical MD cut
            # exactly, including a possible partial table. Never the default.
            budget = input_max_chars or (XML_MAX_CHARS if source == "xml" else 80000)
            path = getattr(ref, {"md": "md_path", "xml": "xml_path", "pdf": "pdf_path", "pdf-native": "pdf_path"}.get(source, "md_path"), None)
            name = path.name if path else "main." + source
            raw = path.read_bytes() if path and path.exists() else text.encode()
            legacy = ExtractionInputs(
                batches=[InputBatch("legacy-part-1", name, text[:budget], source)],
                sources=[{"source": name, "sha256": hashlib.sha256(raw).hexdigest(), "chars": len(text)}])
            if len(text) > budget:
                legacy.issues.append({"code": "legacy_input_truncated", "source": name,
                                      "omitted_chars": len(text) - budget})
            if getattr(ref, "dir", None) and (Path(ref.dir) / "supplementary").exists():
                legacy.issues.append({"code": "legacy_supplements_not_loaded", "source": "supplementary/"})
            return legacy
        if input_packaging != "table-aware":
            raise ValueError("input_packaging must be table-aware or legacy")
        return build_extraction_inputs(
            ref, text, source, all_outcomes=all_outcomes, include_supplementary=include_supplementary,
            max_chars=input_max_chars or (XML_MAX_CHARS if source == "xml" else 80000),
            max_batches=max_input_batches)

    inputs = _inputs(full_text, text_source)
    if not inputs.batches:
        _write_result({"paper_id": paper_id, "_status": "low_quality_text",
                       "_extraction_inputs": inputs.manifest(),
                       "_warnings": ["no_complete_input_within_budget"]})
        return {"_status": "low_quality_text", "_cost": 0.0, "_input_tokens": 0,
                "_output_tokens": 0, "n_outcomes": 0, "n_effects": 0}
    full_text = inputs.batches[0].text
    src_tag = f" [{text_source}]" if text_source != "md" else ""
    # Fill per-paper placeholders (double-braced in template -> single brace
    # after .format). `format_note` is passed UNCONDITIONALLY: an unused kwarg
    # is ignored, but a template that has the placeholder and doesn't get the
    # kwarg raises KeyError.
    def _build_prompt(text: str, source: str) -> str:
        user_prompt = user_template.format(full_text=text, paper_id=paper_id,
                                           format_note=format_note_for(source))
        return f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt

    prompt = _build_prompt(full_text, text_source)

    log_status(seq, total, "START", f"{len(full_text):,} chars{src_tag}")

    outcome = _run_extraction(prompt, paper_id, seq, total)
    result = outcome.result
    cost_usd += outcome.cost
    elapsed += outcome.elapsed
    input_tokens += outcome.input_tokens
    output_tokens += outcome.output_tokens

    if outcome.status == "halted":
        return {**_HALTED, "_cost": cost_usd, "_input_tokens": input_tokens,
                "_output_tokens": output_tokens}
    if outcome.status == "policy_skip":
        log_status(seq, total, "SKIP", f"policy refusal  {paper_id}")
        _write_result({"paper_id": paper_id, "_status": "policy_skip"})
        return {"_status": "policy_skip", "_cost": cost_usd, "_input_tokens": input_tokens,
                "_output_tokens": output_tokens, "n_outcomes": 0, "n_effects": 0}
    if outcome.status == "refused":
        log_status(seq, total, "SKIP", f"model refusal  {paper_id}")
        _write_result({"paper_id": paper_id, "_status": "refused",
                       "_reason": outcome.reason})
        return {"_status": "refused", "_cost": cost_usd, "_input_tokens": input_tokens,
                "_output_tokens": output_tokens, "n_outcomes": 0, "n_effects": 0}

    if result is None:
        log_status(seq, total, "FAIL", f"${cost_usd:.3f}  {MAX_RETRIES} attempts")
        _write_result({"paper_id": paper_id, "_status": "parse_error"})
        return {"_status": "parse_error", "_cost": cost_usd, "_input_tokens": input_tokens,
                "_output_tokens": output_tokens, "n_outcomes": 0, "n_effects": 0}

    def _remaining_batches(result, inputs, first_text, text_source):
        nonlocal cost_usd, elapsed, input_tokens, output_tokens
        result = stamp_extraction_source(result, inputs.batches[0].source)
        # A successful first result can still be missing every table after the old
        # 80k cut. Extract remaining complete batches independently, including
        # canonical supplements even when the review asks only for primary outcomes.
        batch_audit = [{"batch_id": inputs.batches[0].batch_id, "source": inputs.batches[0].source,
                        "status": "ok"}]
        quote_texts = [first_text]
        for batch in inputs.batches[1:]:
            context_text, context_issue = _batch_identity_context(result)
            if context_issue:
                inputs.issues.append({**context_issue, "source": batch.source,
                                      "batch_id": batch.batch_id})
            prompt = _build_prompt(batch.text, "md" if batch.source_kind == "supplement" else batch.source_kind)
            prompt += ("\n\nThis is another source section from the SAME paper, source " + batch.source
                       + ". Extract only evidence in this section. Reuse the exact arm descriptions and IDs below "
                       "when they match. Reuse the exact outcome identity fields and timepoint definitions below "
                       "for the same measurement; keep genuinely different units, populations or analysis bases separate. "
                       "Include study_design.arms and outcome.timepoints so identities can be checked, and explicit "
                       "intervention_arm_id/comparator_arm_id for every between-group result, even a p-value-only result. "
                       "Do not repeat results solely from the context. Cite this source filename, table, row and headers. "
                       "Earlier outcome identities may be omitted to fit the context allowance; resolve absent "
                       "identities from this section and never infer that an absent outcome was not measured. "
                       "Keep the extraction scope specified above.\nStudy context:\n" + context_text)
            log_status(seq, total, "BATCH", f"{batch.batch_id}: {len(batch.text):,} chars")
            try:
                extra = _run_extraction(prompt, paper_id, seq, total)
            except Exception as exc:
                if run_halted():
                    return None, quote_texts, batch_audit
                inputs.issues.append({"code": "batch_failed", "source": batch.source,
                                      "batch_id": batch.batch_id, "detail": str(exc)[:300]})
                batch_audit.append({"batch_id": batch.batch_id, "source": batch.source, "status": "error"})
                continue
            cost_usd += extra.cost
            elapsed += extra.elapsed
            input_tokens += extra.input_tokens
            output_tokens += extra.output_tokens
            if extra.status == "halted":
                return None, quote_texts, batch_audit
            if extra.result is None:
                status = extra.status or "parse_error"
                inputs.issues.append({"code": "batch_failed", "source": batch.source,
                                      "batch_id": batch.batch_id, "status": status})
                batch_audit.append({"batch_id": batch.batch_id, "source": batch.source, "status": status})
                continue
            # Methods/safety-only sections still contain metadata worth keeping.
            # Merge also retains their complete source-labelled metadata for review.
            has_outcomes = any(isinstance(o, dict) for o in (extra.result.get("outcomes") or []))
            result = merge_extraction_records(result, extra.result, batch.source)
            batch_audit.append({"batch_id": batch.batch_id, "source": batch.source,
                                "status": "ok" if has_outcomes else "no_outcomes",
                                "provenance": verify_record(extra.result, batch.text,
                                                            alt_text=xml_plain_text(batch.text))})
            quote_texts.append(batch.text)
        return result, quote_texts, batch_audit

    result, quote_texts, batch_audit = _remaining_batches(result, inputs, full_text, text_source)
    if result is None:
        return {**_HALTED, "_cost": cost_usd, "_input_tokens": input_tokens,
                "_output_tokens": output_tokens}
    full_text = "\n\n".join(quote_texts)

    # --- fall down the source chain when the numbers aren't there ------------
    # A record can be well-formed and still carry no arm results or effect
    # sizes. Usually that means the source lost its TABLES: an image-only
    # <table-wrap> in the XML, or a `grobid-only` conversion of the PDF. Re-run
    # against the next rung, retaining earlier evidence for source comparison.
    # Costs an extra call only for papers that produced nothing.
    refetched_from = None
    retained_source_candidates = []
    if retry_on_empty and not has_outcome_data(result):
        for alt_text, alt_source, alt_files in _fallback_sources(ref, text_source):
            if alt_files and effective_provider() in {"codex_cli", "openrouter"}:
                # These transports cannot read a PDF attachment. An empty
                # transport reply is neither truncated JSON nor evidence that
                # the paper is ineligible. Retain the text result for review.
                inputs.issues.append({"code": "fallback_source_unavailable",
                                      "source": _source_file_for(ref, alt_source),
                                      "source_kind": alt_source,
                                      "provider": effective_provider()})
                log_status(seq, total, "REVIEW",
                           f"{alt_source} unavailable with {effective_provider()}; retaining text result")
                continue
            alt_inputs = _inputs(alt_text, alt_source, include_supplementary=False)
            if not alt_inputs.batches:
                continue
            alt_text = alt_inputs.batches[0].text
            log_status(seq, total, "RETRY",
                       f"{text_source} yielded no numbers -> {alt_source}")
            alt = _run_extraction(_build_prompt(alt_text, alt_source), paper_id,
                                  seq, total, read_files=alt_files)
            cost_usd += alt.cost
            elapsed += alt.elapsed
            input_tokens += alt.input_tokens
            output_tokens += alt.output_tokens
            if alt.status == "halted":
                return {**_HALTED, "_cost": cost_usd, "_input_tokens": input_tokens,
                        "_output_tokens": output_tokens}
            if alt.result is None:
                continue
            alt_result, alt_texts, alt_audit = _remaining_batches(alt.result, alt_inputs, alt_text, alt_source)
            if alt_result is None:
                return {**_HALTED, "_cost": cost_usd, "_input_tokens": input_tokens,
                        "_output_tokens": output_tokens}
            if not has_outcome_data(alt_result):
                continue
            retained_source_candidates = _archive_fallback_candidate(
                result, _source_file_for(ref, text_source),
                {**inputs.manifest(), "packaging": input_packaging,
                 "batch_results": batch_audit})
            alt_result.setdefault("_source_findings", []).append({
                "code": "extraction_source_fallback_requires_review", "path": "outcomes",
                "severity": "blocking", "source_file": _source_file_for(ref, text_source),
                "reason": "Earlier source candidates remain in _unmerged_extractions; all prior metadata and outcomes require source comparison before using the fallback results."})
            # Rebind full_text WITH result: verify_record checks every quote
            # against this haystack, so leaving the old text here would score
            # every quote from the new record as a fabrication and collapse
            # traceable_rate corpus-wide, silently.
            result, full_text, text_source = alt_result, "\n\n".join(alt_texts), alt_source
            quote_texts, batch_audit = alt_texts, alt_audit
            # All original source hashes and input issues remain relevant.
            # Full batch diagnostics are also archived with the prior candidate.
            alt_inputs.issues.extend(inputs.issues)
            alt_inputs.issues.append({"code": "source_fallback_requires_reconciliation",
                                      "source": retained_source_candidates[0]["source"],
                                      "record_sha256": retained_source_candidates[0]["record_sha256"]})
            alt_inputs.sources.extend(deepcopy(s) for s in inputs.sources if s not in alt_inputs.sources)
            inputs = alt_inputs
            refetched_from = alt_source
            break


    if _is_null_extraction(result):
        uncertain = bool(inputs.issues or result.get("_unmerged_extractions"))
        status = "low_quality_text" if uncertain else "ineligible"
        reason = ("all-null extraction with incomplete or unmerged source inputs" if uncertain else
                  "all-null extraction (model judged no extractable trial data)")
        log_status(seq, total, "SKIP", f"{status} ({reason})  {paper_id}")
        notes = ((result.get("extraction_metadata") or {}).get("extractor_notes")
                 or "")[:500]
        _write_result({"paper_id": paper_id, "_status": status,
                       "_reason": reason,
                       "_notes": notes, "_model": _MODEL,
                       "_unmerged_extractions": result.get("_unmerged_extractions", []),
                       "_extraction_inputs": {**inputs.manifest(), "packaging": input_packaging,
                                              "status": "partial" if uncertain else "complete",
                                              "requires_review": uncertain,
                                              "batch_results": batch_audit}})
        return {"_status": status, "_cost": cost_usd, "_input_tokens": input_tokens,
                "_output_tokens": output_tokens, "n_outcomes": 0, "n_effects": 0}

    result = _postprocess(result, paper_id, text_source, design_type)
    result["_text_source"] = text_source
    result["_source_file"] = _source_file_for(ref, text_source)
    # After _postprocess, which ASSIGNS _warnings rather than extending it.
    if refetched_from:
        result.setdefault("_warnings", []).append(f"refetched_from_{refetched_from}")
    manifest = inputs.manifest()
    manifest["packaging"] = input_packaging
    manifest["batch_results"] = batch_audit
    omissions = _declared_extraction_omissions(result)
    if omissions:
        manifest.update(status="partial", requires_review=True)
        manifest.setdefault("issues", []).append({
            "code": "model_declared_incomplete_extraction", "notes": omissions})
        result.setdefault("_source_findings", []).append({
            "code": "model_declared_incomplete_extraction", "path": "outcomes",
            "severity": "blocking",
            "reason": "The model explicitly abbreviated the returned extraction; missing results require source review."})
    if (result.get("_merge_conflicts") or result.get("_unmerged_extractions")
            or result.get("_metadata_review") or retained_source_candidates):
        manifest.update(status="partial", requires_review=True)
        result.setdefault("_warnings", []).append("extraction_batch_merge_requires_review")
    result["_extraction_inputs"] = manifest
    if manifest["requires_review"]:
        result.setdefault("_warnings", []).append("extraction_inputs_require_review")
        result.setdefault("_source_findings", []).append({"code": "incomplete_extraction_inputs",
            "path": "_extraction_inputs", "severity": "warning",
            "reason": "Some source sections are unreadable, omitted, failed or require adjudication; see the input manifest."})
    # XML results are not downgraded because an unused PDF conversion failed.
    if text_source == "md" and getattr(ref, "dir", None) is not None:
        quality = read_conversion_quality(Path(ref.dir))
        result["_conversion_quality"] = quality
        if quality.get("requires_review") or quality.get("degraded"):
            result.setdefault("_warnings", []).append("conversion_requires_review")
            result.setdefault("_source_findings", []).append({"code": "conversion_requires_review",
                "path": "_conversion_quality", "severity": "warning",
                "reason": "The converted source requires review; see conversion quality diagnostics."})

    n_outcomes = len(result.get("outcomes") or [])
    n_effects = sum(len(o.get("between_group_effects") or [])
                    for o in result.get("outcomes") or [])
    design = (result.get("study_design") or {}).get("design_type") or "?"
    warns = result.get("_warnings", [])
    warn_str = f"  [{', '.join(warns)}]" if warns else ""
    log_status(seq, total, "DONE",
               f"{design:<6s}  {n_outcomes}out  {n_effects}fx  ${cost_usd:.3f}  {elapsed:5.1f}s{warn_str}")

    result["_status"] = "ok"
    result["_model"] = _MODEL
    # Check every *_quote against the text it claims to come from. A quote the
    # model invented is worse than no quote, so this is what makes the schema's
    # provenance fields mean something. Reported, never enforced: converted PDFs
    # lose table rows, so `absent` can be a conversion failure rather than a
    # fabrication, and refusing the record would discard good extractions.
    #
    # For XML and HTML tables in Markdown the raw markup is the wrong haystack
    # on its own: a quote that is
    # contiguous in a rendered table sits either side of `</td><td>` in the
    # markup, so a legitimate quote scores absent. `alt_text` is tried second,
    # only for quotes that already failed, leaving md/pdf results untouched.
    if text_source == "pdf-native":
        # Page images — there is no text to check quotes against. Say so rather
        # than report a traceable_rate of zero, which reads as fabrication.
        prov = {"status": "unverifiable",
                "reason": "extracted from PDF page images; no text haystack"}
    else:
        prov = verify_record(
            result, full_text,
            alt_text=xml_plain_text(full_text) if (
                text_source == "xml" or len(quote_texts) > 1 or
                re.search(r"<table\b", full_text, re.I)) else None)
    result["_provenance"] = prov
    # verify_record recursively annotates quotes. Attach these immutable prior
    # candidates only afterwards: their XML/supplement quotes must never be
    # checked against the winning PDF text or have their stored hashes changed.
    if retained_source_candidates:
        result.setdefault("_unmerged_extractions", []).extend(retained_source_candidates)
    from ber_core.scientific import validate_record
    result["_scientific_validation"] = validate_record(result)
    _write_result(result)

    return {"_status": "ok", "_cost": cost_usd, "_input_tokens": input_tokens,
            "_output_tokens": output_tokens, "n_outcomes": n_outcomes,
            "n_effects": n_effects, "_prov": prov}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pass 2: Extract full quantitative trial data for meta-analysis")
    parser.add_argument(
        "--config", required=True,
        help="Path to the review's YAML config")
    parser.add_argument(
        "--design-type", default="rct", choices=["rct", "observational"],
        help="Study design type: 'rct' (default) or 'observational'. "
             "Determines which prompt template and postprocessing rules to use.")
    parser.add_argument(
        "--input", default=None,
        help="Directory containing eligible MD + PDF files (overrides config)")
    parser.add_argument(
        "--output", default=None,
        help="Output JSONL path (default: projects/{slug}/data/trial_extractions_pass2.jsonl "
             "for RCT, observational_extractions.jsonl for observational)")
    parser.add_argument(
        "--model", default=None,
        help="Claude model to use (overrides config). E.g. sonnet, opus, haiku")
    parser.add_argument(
        "--tag", default=None,
        help="Tag appended to output filename and stamped in each record. "
             "E.g. --tag opus produces trial_extractions_pass2_opus.jsonl")
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Number of concurrent workers. Overrides config `extraction_workers` "
             "(which itself defaults to 10 when unset).")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Limit number of files to process (0 = all)")
    parser.add_argument(
        "--xml-first", dest="xml_first", action="store_true", default=None,
        help="Prefer publisher XML over the PDF-derived markdown. ON by default: "
             "XML keeps the table structure the conversion drops. Config key "
             "`fulltext.xml_first`.")
    parser.add_argument(
        "--no-xml-first", dest="xml_first", action="store_false",
        help="Read markdown first, as before structured text existed.")
    parser.add_argument(
        "--retry-on-empty", dest="retry_on_empty", action="store_true", default=None,
        help="When an extraction returns no numbers, re-run it against the next "
             "source (markdown, then the model reading the PDF pages itself). "
             "ON by default; config key `fulltext.retry_on_empty`.")
    parser.add_argument(
        "--no-retry-on-empty", dest="retry_on_empty", action="store_false",
        help="One extraction per paper, whatever it returns.")
    parser.add_argument(
        "--input-packaging", choices=["table-aware", "legacy"], default="table-aware",
        help="Source packaging. legacy explicitly reproduces the historical single text cut for benchmark controls; it omits supplements.")
    parser.add_argument(
        "--input-budget-chars", type=int, default=None,
        help="Characters per source batch (default: 80000 for Markdown, 180000 for XML).")
    parser.add_argument(
        "--max-input-batches", type=int, default=None,
        help="Maximum source batches per paper/source attempt, 1-20 (default: 6).")
    args = parser.parse_args()
    if args.input_budget_chars is not None and args.input_budget_chars < 256:
        parser.error("--input-budget-chars must be at least 256")
    if args.max_input_batches is not None and not 1 <= args.max_input_batches <= 20:
        parser.error("--max-input-batches must be between 1 and 20")

    # Load config
    cfg = load_config(args.config)

    # Worker count: explicit --workers wins; else the review's config
    # `extraction_workers`; else the module default. Clamp to >= 1.
    if args.workers is None:
        try:
            args.workers = int(cfg.get("extraction_workers") or DEFAULT_EXTRACTION_WORKERS)
        except (TypeError, ValueError):
            args.workers = DEFAULT_EXTRACTION_WORKERS
    args.workers = max(1, args.workers)

    # Source preference and the empty-result fallback: --flag > config > default.
    # Both default ON — publisher XML is the only source that carries table
    # structure, and a paper that extracted no numbers is worth one more look.
    _ft = cfg.get("fulltext") or {}
    max_input_batches = (args.max_input_batches if args.max_input_batches is not None
                         else _ft.get("extraction_max_batches", MAX_BATCHES))
    if isinstance(max_input_batches, bool) or not isinstance(max_input_batches, int) or not 1 <= max_input_batches <= 20:
        parser.error("fulltext.extraction_max_batches must be an integer between 1 and 20")
    xml_first = (args.xml_first if args.xml_first is not None
                 else bool(_ft.get("xml_first", True)))
    retry_on_empty = (args.retry_on_empty if args.retry_on_empty is not None
                      else bool(_ft.get("retry_on_empty", True)))

    paths = project_paths(cfg)
    print(f"Project: {paths['topic_name']!r} -> {paths['project_dir']}",
          file=sys.stderr, flush=True)
    set_log_dest_dir(paths["project_dir"] / "claude_code_logs")

    # Set module globals
    global _MODEL, _OUTPUT_PATH
    _MODEL = resolve_model(args.model or cfg.get("models", {}).get("extraction_pass2", "sonnet"))

    # Load and fill prompt template, gated by the review's extraction_pass2_fields.
    field_flags = resolve_pass2_field_flags(cfg)
    system_prompt, user_template = _load_prompt_parts(
        args.design_type, cfg, slug=paths["slug"], field_flags=field_flags)
    _skipped = [b for b, on in field_flags.items() if not on]
    print(f"Pass-2 extraction blocks: "
          f"{'basic (deep off)' if _skipped else 'full'}"
          + (f" | skipping: {', '.join(_skipped)}" if _skipped else ""),
          file=sys.stderr, flush=True)
    _n_custom = len([q for q in (cfg.get("custom_extraction_questions") or [])
                     if isinstance(q, str) and q.strip()])
    if _n_custom:
        print(f"Custom extraction questions: {_n_custom}", file=sys.stderr, flush=True)

    # Resolve input directory.
    #
    # Pass 2 is the EXPENSIVE step: one full-text Sonnet call per paper. It must
    # only ever see the papers stage_eligible.py routed into the design bucket
    # (pass-1 eligible + force-included, minus user_exclusions.json). So unlike
    # pass 1 — which legitimately screens the whole converted corpus and shares
    # `resolve_input_dir`'s converted/ fallback — pass 2 must NEVER fall back to
    # converted/. Doing so silently extracts the entire corpus at ~25x the cost
    # and launders pass-1-ineligible papers into the evidence table, PRISMA
    # counts, and the meta-analysis pool (evidence.py ranks "extracted" highly).
    #
    # Priority: --input (explicit operator override) -> configured dirs.* (only
    # if non-blank; preserves hand-written configs that point elsewhere, and
    # IGNORED in a studio-managed run, where the config is user-editable and a
    # path there could reach another review's papers) -> the project's own
    # eligible_{design}/ bucket, which is the hardcoded convention
    # stage_eligible.py writes. A MISSING bucket is a hard error: it means
    # staging never ran, and the honest answer is to say so rather than to
    # quietly extract something else.
    configured = configured_dir(
        cfg, "eligible_trials" if args.design_type == "rct" else "observational_treatment")
    staged_dir = paths["project_dir"] / f"eligible_{args.design_type}"
    input_dir = resolve_input_dir(args.input, configured, staged_dir)

    if not input_dir.is_dir():
        print(f"{RED}ERROR: input directory does not exist: {input_dir}{RESET}\n"
              f"  Pass 2 extracts only papers staged into eligible_"
              f"{args.design_type}/ by stage_eligible.py.\n"
              f"  Run the full-text eligibility screen first (studio: it fires\n"
              f"  automatically as full text arrives; CLI: extract_pass1.py\n"
              f"  then stage_eligible.py --config <cfg> --execute).",
              file=sys.stderr, flush=True)
        return 2

    # Resolve output path
    if args.output:
        default_output = Path(args.output)
    elif args.design_type == "rct":
        default_output = paths["data_dir"] / "trial_extractions_pass2.jsonl"
    else:
        default_output = paths["data_dir"] / "observational_extractions.jsonl"

    if args.tag:
        output_path = default_output.with_stem(f"{default_output.stem}_{args.tag}")
    else:
        output_path = default_output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH = output_path

    # Gather papers (nested converted/<stem>/ or flat layout).
    papers = discover_papers(input_dir)
    n_md = sum(1 for r in papers if r.md_path)
    n_xml_only = sum(1 for r in papers if not r.md_path and r.xml_path)
    n_pdf_only = sum(1 for r in papers if not r.md_path and not r.xml_path and r.pdf_path)
    print(f"Design type: {args.design_type}  Model: {_MODEL}  Output: {output_path}",
          file=sys.stderr, flush=True)
    print(f"Found {len(papers)} papers in {input_dir} "
          f"({n_md} MD, {n_xml_only} XML-only, {n_pdf_only} PDF-only)",
          file=sys.stderr, flush=True)

    # Load existing results for resumability. `no_text` is re-tried: the text
    # may have arrived since, and re-reading costs nothing when it has not.
    n_requeued = requeue_no_text(output_path, [r.paper_id for r in papers])
    if n_requeued:
        print(f"Re-trying {n_requeued} paper(s) previously recorded as no_text",
              file=sys.stderr, flush=True)
    done_ids = load_done_ids_jsonl(output_path)
    papers = [r for r in papers if r.paper_id not in done_ids]
    if done_ids:
        print(f"Skipping {len(done_ids)} already processed, {len(papers)} remaining",
              file=sys.stderr, flush=True)

    if args.limit > 0:
        papers = papers[:args.limit]
    print(f"Processing {len(papers)} papers with {args.workers} workers\n",
          file=sys.stderr, flush=True)

    total = len(papers)
    counter = {
        "done": 0, "total": total, "total_cost": 0.0,
        "errors": 0, "skipped": 0,
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_outcomes": 0, "total_effects": 0,
        # Quote-verification tallies; see ber_core/quote_verification.py. The traceable rate
        # is the headline honesty metric for the whole extraction.
        "quotes_checked": 0, "quotes_exact": 0, "quotes_fuzzy": 0,
        "quotes_absent": 0,
        "start": time.monotonic(),
    }

    def _progress():
        # utils.log_progress, not a private copy — see the same change in
        # extract_pass1.py. Both scripts had a line-for-line duplicate of it.
        log_progress(
            counter["done"], counter["total"],
            cost=counter["total_cost"], start_time=counter["start"],
            extra=(f"outcomes: {counter['total_outcomes']}  "
                   f"effects: {counter['total_effects']}  "
                   f"errors: {counter['errors']}  "
                   f"skipped: {counter['skipped']}"),
        )

    # Emit a progress line up front so the total is known immediately — otherwise
    # the studio's live bar sits on "starting…" until the first batch finishes,
    # which reads as a stuck run on small corpora with slow per-paper calls.
    if total:
        _progress()

    halted = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(extract_one, r, i + 1, total,
                        system_prompt, user_template,
                        args.design_type, xml_first, retry_on_empty,
                        bool(field_flags.get("all_outcomes")),
                        max_input_batches, args.input_budget_chars,
                        args.input_packaging): r
            for i, r in enumerate(papers)
        }
        try:
            for future in as_completed(futures):
                if run_halted() and not halted:
                    # Credential rejected / usage stop: cancel the queue. Workers
                    # write their own records, so the few in flight finish (and
                    # return at once) while the rest never start.
                    halted = True
                    for pending in futures:
                        pending.cancel()
                    print(f"  {RED}Stopping: LLM credential rejected or usage "
                          f"limit reached. Queued papers cancelled.{RESET}",
                          file=sys.stderr, flush=True)
                if future.cancelled():
                    continue
                try:
                    summary = future.result()
                except Exception as exc:
                    paper_id = futures[future].paper_id
                    print(f"  {RED}UNHANDLED ERROR: {paper_id}: {exc}{RESET}",
                          file=sys.stderr, flush=True)
                    _write_result({"paper_id": paper_id, "_status": "error"})
                    summary = {"_status": "error", "_cost": 0.0, "_input_tokens": 0,
                               "_output_tokens": 0, "n_outcomes": 0, "n_effects": 0}

                status = summary["_status"]
                if status == "halted":
                    counter["total_cost"] += summary.get("_cost", 0.0)
                    continue
                counter["done"] += 1
                counter["total_cost"] += summary.get("_cost", 0.0)
                counter["total_input_tokens"] += summary.get("_input_tokens", 0)
                counter["total_output_tokens"] += summary.get("_output_tokens", 0)
                counter["total_outcomes"] += summary.get("n_outcomes", 0)
                counter["total_effects"] += summary.get("n_effects", 0)
                prov = summary.get("_prov") or {}
                counter["quotes_checked"] += prov.get("n_quoted", 0)
                counter["quotes_exact"] += prov.get("n_exact", 0)
                counter["quotes_fuzzy"] += prov.get("n_fuzzy", 0)
                counter["quotes_absent"] += prov.get("n_absent", 0)
                if status in ("parse_error", "error"):
                    counter["errors"] += 1
                if status == "no_text":
                    counter["skipped"] += 1

                # Emit on every completion so the studio's live progress bar
                # advances paper-by-paper (the terminal ETA line is cheap). The
                # old every-10th cadence left small runs looking stuck.
                _progress()
        except KeyboardInterrupt:
            print("\n  Interrupted! Shutting down workers...", file=sys.stderr, flush=True)
            pool.shutdown(wait=False, cancel_futures=True)

    _progress()
    print(
        f"\n  Done. {counter['done']} papers processed | "
        f"outcomes: {counter['total_outcomes']} | effects: {counter['total_effects']} | "
        f"errors: {counter['errors']} | skipped: {counter['skipped']} | "
        f"cost: ${counter['total_cost']:.2f}",
        file=sys.stderr, flush=True,
    )
    # The headline honesty metric: of the quotes we could check against the
    # source text, how many actually appear in it.
    checked = counter["quotes_checked"]
    if checked:
        rate = (counter["quotes_exact"] + counter["quotes_fuzzy"]) / checked
        counter["traceable_rate"] = round(rate, 3)
        print(f"  Provenance: {rate:.1%} of {checked} extracted values traceable "
              f"to a verbatim span "
              f"({counter['quotes_exact']} exact, {counter['quotes_fuzzy']} fuzzy, "
              f"{counter['quotes_absent']} not found)",
              file=sys.stderr, flush=True)

    emit_run_summary(f"9_extract_pass2_{args.design_type}", counter,
                     outputs=[output_path], model=_MODEL,
                     data_dir=output_path.parent)


if __name__ == "__main__":
    # main() returns a non-zero code on unrecoverable setup errors (e.g. the
    # staged eligible_*/ bucket is missing). The runner treats a non-zero exit
    # as the failure signal, so it must be propagated.
    code = main() or 0
    # A rejected credential (expired setup-token, revoked API key) is fatal and
    # unrecoverable — but every per-paper worker catches Exception so one bad
    # paper cannot abort a run, which would turn this into "every paper failed"
    # and a successful-looking run that produced nothing. claude_cli latches the
    # failure and short-circuits the remaining calls; this converts that into a
    # non-zero exit, from the main thread, once writes have flushed.
    raise_if_auth_failed()
    sys.exit(code)
