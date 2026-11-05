#!/usr/bin/env python3
"""Pass 1: per-paper eligibility screening + basic metadata extraction from text.

Runs one Claude call per converted paper to decide inclusion eligibility and pull
lightweight basics (design, population, intervention) that gate the later passes.

Reads:
    Converted paper text under projects/{slug}/ (XML/markdown/text; discovered via
    discover_papers + read_best_text). Prompt template prompts/extract_pass1.txt,
    gated by config.extraction_pass1_fields.
Writes:
    projects/{slug}/data/trial_extractions.jsonl (one JSON record per line; append
    mode). Per-record fields include _status ("ok" | "no_text" | "parse_error"),
    _cost, _input_tokens, _output_tokens.
Models:
    config.models.extraction_pass1 (default "sonnet"); CLI_TIMEOUT = 300s per call,
    MAX_RETRIES = 3.
Input:
    The project's paper store (``dirs.base_pdf_dir`` or projects/{slug}/papers),
    or ``--input``. ``dirs.eligible_trials`` is NOT an input: that bucket is what
    stage_eligible.py rebuilds FROM this pass's output, so reading it here was
    circular.
Resumability:
    Skips paper_ids already present in the output jsonl (load_done_ids_jsonl), so
    re-running only processes new/missing papers. ``no_text`` verdicts are
    re-tried every run (free when there is still no text).
Side effects:
    Appends to the output jsonl under a write lock (parallel ThreadPoolExecutor);
    writes Claude call logs via set_log_dest_dir; prints progress to stderr.
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.claude_cli import (
    call_claude, parse_json_response, is_truncated, is_refusal,
    raise_if_auth_failed, run_halted, set_log_dest_dir, resolve_model,
)
from ber_core.utils import (
    load_config, load_prompt_template, load_done_ids_jsonl, requeue_no_text,
    ensure_format_note_placeholder,
    log_status, log_progress, emit_run_summary, RED, RESET,
    project_paths, resolve_input_dir, discover_papers, read_best_text, PaperRef,
    norm_paper_id, load_inclusion_dois, drop_jsonl_records,
)
from ber_core.constants import format_note_for

CLI_TIMEOUT = 300  # seconds per claude call (sonnet is slower, longer text)
MAX_RETRIES = 3

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

# Known DOI prefix -> journal name mappings for fallback
_DOI_JOURNAL_MAP = {
    "10.1001/jamanetworkopen": "JAMA Network Open",
    "10.1001/jamainternmed": "JAMA Internal Medicine",
    "10.1001/jamaneurol": "JAMA Neurology",
    "10.1001/jamaoto": "JAMA Otolaryngology-Head & Neck Surgery",
    "10.1001/jama.": "JAMA",
    "10.1002/alr": "International Forum of Allergy & Rhinology",
    "10.1002/clc": "Clinical Cardiology",
    "10.1002/fsn3": "Food Science & Nutrition",
    "10.1002/hsr2": "Health Science Reports",
    "10.1002/ctm2": "Clinical and Translational Medicine",
    "10.1002/jbio": "Journal of Biophotonics",
    "10.1002/jcsm": "Journal of Cachexia, Sarcopenia and Muscle",
    "10.1016/j.eclinm": "eClinicalMedicine",
    "10.1016/j.lanepe": "The Lancet Regional Health - Europe",
    "10.1016/j.lancet": "The Lancet",
    "10.1038/s41591": "Nature Medicine",
    "10.1038/s41586": "Nature",
    "10.1136/bmj": "BMJ",
    "10.1056/nejmoa": "New England Journal of Medicine",
    "10.1093/cid": "Clinical Infectious Diseases",
    "10.7554/elife": "eLife",
}


def _doi_to_journal(doi: str) -> str | None:
    """Try to infer journal name from DOI prefix."""
    doi_lower = doi.lower()
    for prefix, journal in _DOI_JOURNAL_MAP.items():
        if doi_lower.startswith(prefix):
            return journal
    return None


def _year_from_doi(doi: str) -> int | None:
    """Try to extract publication year from DOI."""
    m = re.search(r"\.(20[12]\d)\.", doi)
    if m:
        return int(m.group(1))
    return None


def _postprocess(parsed: dict, paper_id: str) -> dict:
    """Apply deterministic fixes and add warnings after LLM extraction."""
    warnings = []
    meta = parsed.get("metadata", {})

    # Fill DOI from paper_id if missing
    if not meta.get("doi"):
        meta["doi"] = paper_id
        warnings.append("doi_filled_from_paper_id")

    # Fill or validate year from DOI
    doi_year = _year_from_doi(paper_id)
    if doi_year:
        extracted_year = meta.get("year")
        if extracted_year is None:
            meta["year"] = doi_year
            warnings.append(f"year_filled_from_doi={doi_year}")
        elif extracted_year != doi_year:
            warnings.append(f"year_mismatch: extracted={extracted_year} doi_implies={doi_year}")

    # Fill journal from DOI prefix if missing
    if not meta.get("journal"):
        inferred = _doi_to_journal(paper_id)
        if inferred:
            meta["journal"] = inferred
            warnings.append(f"journal_filled_from_doi={inferred}")

    # Flag protocol documents
    screening = parsed.get("screening", {})
    is_protocol = screening.get("is_protocol_not_article", {})
    if isinstance(is_protocol, dict) and is_protocol.get("verdict") is True:
        warnings.append("document_is_protocol_not_article")

    if warnings:
        parsed["_warnings"] = warnings

    return parsed


def _parse_extraction(response: str, paper_id: str) -> dict | None:
    """Parse Claude's response into an extraction dict. Returns None on parse failure."""
    parsed = parse_json_response(response, paper_id)
    if parsed is None:
        return None
    parsed["paper_id"] = paper_id
    return _postprocess(parsed, paper_id)


# --- Optional field fragments (JSON schema + RULES lines) --------------
# Always emitted: report originality, protocol status and minimal metadata.
# All other field groups are gated by config.extraction_pass1_fields.
_FIELD_FRAGMENTS = {
    "study_design_and_control_arm": {
        "schema": (
            '    "is_clinical_trial": {{\n'
            '      "verdict": "yes | no | unclear",\n'
            '      "study_design": "RCT | quasi-experimental | crossover | single-arm | observational | other",\n'
            '      "has_control_arm": true | false | null,\n'
            '      "reasoning": "One sentence."\n'
            '    }}'
        ),
        "location": "screening",
        "rule": "- Set is_clinical_trial.has_control_arm based on whether a comparator arm (placebo, active control, usual care) is described in the methods.",
    },
    "organism": {
        "schema": (
            '    "organism": {{\n'
            '      "verdict": "human | mouse | other_animal | other",\n'
            '      "reasoning": "One sentence."\n'
            '    }}'
        ),
        "location": "screening",
        "rule": (
            "- organism.verdict: the biological system the study's MAIN result was produced in. "
            "\"human\" = human participants/patients/volunteers (even with a small supporting lab component); "
            "\"mouse\" = experiments mainly in mice (incl. humanized-mouse models); "
            "\"other_animal\" = live-animal experiments in any other species (rat, dog, pig, primate, zebrafish, …); "
            "\"other\" = in-vitro / cell-culture / ex-vivo / in-silico or when the organism cannot be determined."
        ),
    },
    "relevance_recheck": {
        "schema": (
            '    "is_relevant": {{\n'
            '      "verdict": "yes | no | unclear",\n'
            '      "reasoning": "One sentence.",\n'
            '      "condition_terms_used": ["terms the paper uses"]\n'
            '    }}'
        ),
        "location": "screening",
        "rule": (
            '- is_relevant: "yes" only if the paper directly studies {topic_name} '
            'within the configured review scope; "unclear" if related but '
            'ambiguous. Assess the population, clinical findings, and timing; '
            'the literal condition name is not required when the reported '
            'condition satisfies the scope. Record the terms actually used. '
            'A passing mention or follow-up without relevant findings does not '
            'establish relevance.'
        ),
    },
    "authors_affiliations": {
        "schema": (
            '    "authors": [\n'
            '      {{\n'
            '        "name": "Last, First",\n'
            '        "affiliations": [\n'
            '          {{\n'
            '            "institution": "Full name",\n'
            '            "type": "university | hospital | company | government | research_institute | other",\n'
            '            "country": "Country"\n'
            '          }}\n'
            '        ]\n'
            '      }}\n'
            '    ]'
        ),
        "location": "metadata",
        "rule": "- List ALL authors with ALL affiliations.",
    },
    "funding": {
        "schema": (
            '    "funding": [\n'
            '      {{\n'
            '        "funder": "Funding body name",\n'
            '        "grant_numbers": ["Grant number(s)"],\n'
            '        "funder_type": "government | industry | nonprofit | academic | other"\n'
            '      }}\n'
            '    ],\n'
            '    "funding_statement_verbatim": "Exact funding statement or null"'
        ),
        "location": "metadata",
        "rule": "- FUNDING: Group by distinct funding organization; include the verbatim funding statement.",
    },
    "coi": {
        "schema": (
            '    "coi_statement": "Summary of COI disclosure",\n'
            '    "coi_has_industry_ties": true | false | null'
        ),
        "location": "metadata",
        "rule": "- COI: summarize the conflict-of-interest statement; set coi_has_industry_ties true if any author discloses ties to a company that could profit from the intervention.",
    },
    "trial_registry": {
        "schema": (
            '    "nct_numbers": ["NCT numbers if any"],\n'
            '    "other_registry_ids": ["Non-NCT registry IDs if any"]'
        ),
        "location": "metadata",
        "rule": "- TRIAL REGISTRATION: Look for ALL trial registry IDs (NCT, jRCT, ISRCTN, ACTRN, DRKS, ChiCTR, EudraCT, CTRI, KCT, UMIN, etc.).",
    },
}

from ber_core.defaults import PASS1_FIELD_DEFAULTS


def build_pass1_prompt(base_template: str, topic_name: str, field_flags: dict,
                      topic_context: dict | None = None) -> str:
    """Assemble the pass-1 prompt by injecting selected field fragments into the base template."""
    screening_blocks = [
        '    "reports_original_data": {{\n'
        '      "verdict": true | false | null,\n'
        '      "report_type": "primary_analysis | secondary_analysis | news | review | protocol | unclear",\n'
        '      "reasoning": "Does THIS report present its own study data or analysis, rather than describe another publication?"\n'
        '    }}',
        '    "is_protocol_not_article": {{\n'
        '      "verdict": true | false,\n'
        '      "reasoning": "One sentence."\n'
        '    }}'
    ]
    metadata_blocks = [
        '    "title": "Full title",\n'
        '    "journal": "Journal name",\n'
        '    "year": 2024,\n'
        '    "doi": "DOI or null"'
    ]
    rules = [
        '- REPORT ORIGINALITY: Judge THIS document, not a study it describes. Set reports_original_data.verdict to true for a report presenting its own completed study data or a secondary analysis of original participant data, including a conference abstract with actual results. Set it to false for news stories, press summaries, editorials, or reviews merely describing other publications, even when they reproduce randomized-trial results and exact effect estimates. A trial mentioned or cited by a news story does not make that story a clinical trial. Set it to null when the available source cannot establish originality; do not invent missing methods or data. A secondary analysis need not recruit new participants to be original, but must report an analysis of participant data rather than summarize published study results.',
        '- PROTOCOL DETECTION: If the document has no Results section, no journal header/branding, and is labeled "protocol", "study plan", or "statistical analysis plan", set is_protocol_not_article to true. A methods-only paper (no results reported) also counts as is_protocol_not_article = true.',
    ]
    if topic_context:
        # The completed prompt is formatted once more for each paper. Preserve
        # literal braces in config text instead of treating them as placeholders.
        scope = {key: topic_context[key] for key in
                 ("description", "synonyms", "not_about") if key in topic_context}
        if scope:
            rendered = json.dumps(scope, ensure_ascii=False)
            rendered = rendered.replace("{", "{{").replace("}", "}}")
            rules.append("- CONFIGURED REVIEW SCOPE: " + rendered)

    for key, enabled in field_flags.items():
        if not enabled or key not in _FIELD_FRAGMENTS:
            continue
        frag = _FIELD_FRAGMENTS[key]
        if frag["location"] == "screening":
            screening_blocks.append(frag["schema"])
        else:
            metadata_blocks.append(frag["schema"])
        rules.append(frag["rule"])

    eligible_conds = ["reports_original_data = true", "is_protocol_not_article = false"]
    if field_flags.get("relevance_recheck"):
        eligible_conds.append('is_relevant = "yes"')
    if field_flags.get("study_design_and_control_arm"):
        if field_flags.get("include_observational"):
            # Match the Python eligibility and staging gates: descriptive
            # observational evidence need not have a comparator. Intervention
            # trials still require one. Poolability is assessed after extraction.
            eligible_conds.append(
                '(study_design = "observational" OR '
                '(is_clinical_trial = "yes" AND has_control_arm = true))')
            rules.append(
                '- OBSERVATIONAL SCOPE: Completed descriptive cohorts, patient '
                'surveys, qualitative studies, and observations of routine clinical '
                'care do not require a comparison group or treatment. Prospective '
                'data collection and pre/post measurement alone do not establish '
                'research assignment of treatment. Use explicit design and '
                'assignment details, including statements that care occurred '
                'outside a trial. Preserve '
                'their limitations for extraction; lack of poolable effects is '
                'not an eligibility exclusion. A prospectively research-assigned '
                'intervention without a comparator remains a single-arm trial, '
                'not an observational study. Do not relabel it to bypass the '
                'trial comparator requirement. If assignment is unclear, report '
                'that uncertainty in the study-design reasoning.')
        else:
            eligible_conds.append('is_clinical_trial = "yes"')
            eligible_conds.append("has_control_arm = true")
    # Organism gate: when the review has NOT opted into animal studies, only human
    # studies are eligible — the default. The `include_animal_studies` flag (from
    # review config) relaxes it: with animals included, organism never gates.
    if field_flags.get("organism") and not field_flags.get("include_animal_studies"):
        eligible_conds.append('organism = "human"')
    rules.append(
        "- Set eligible_for_pass2 to true ONLY if " + " AND ".join(eligible_conds) + "."
    )

    screening_blocks.append('    "eligible_for_pass2": true | false')

    json_schema = (
        "{{\n"
        '  "paper_id": "{{paper_id}}",\n\n'
        '  "screening": {{\n'
        + ",\n".join(screening_blocks) + "\n"
        '  }},\n\n'
        '  "metadata": {{\n'
        + ",\n".join(metadata_blocks) + "\n"
        '  }}\n'
        "}}"
    )

    rules_block = "\n".join(rules)
    # Inject the blocks FIRST, then substitute {topic_name} -- the injected
    # fragments can themselves contain it (relevance_recheck's rule does). Doing
    # topic_name first left a literal "{topic_name}" inside the rules block,
    # which survived into the final prompt and raised KeyError('topic_name') on
    # the later .format() -- every paper failed before any LLM call, so pass-1
    # reported 100% errors at $0.00 cost whenever that field was enabled.
    prompt = base_template.replace("{json_schema}", json_schema)
    prompt = prompt.replace("{rules}", rules_block)
    prompt = prompt.replace("{topic_name}", topic_name)
    return prompt


def _screened_out_ids(data_dir: Path) -> set[str]:
    """Papers the abstract screen ruled out, as ``norm_paper_id`` keys.

    ``is_relevant = "no"`` (off-topic) or ``is_excluded = "yes"`` (wrong
    publication type). "unknown"/blank rows are NOT in the set — the screen
    didn't rule, so the full-text pass should look.
    """
    path = data_dir / "classifications.csv"
    out: set[str] = set()
    if not path.exists():
        return out
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pid = norm_paper_id(row.get("doi") or "")
                if not pid:
                    continue
                relevant = (row.get("is_relevant") or "").strip().lower()
                excluded = (row.get("is_excluded") or "").strip().lower()
                if relevant == "no" or excluded == "yes":
                    out.add(pid)
    except OSError:
        return set()
    return out


def _prune_stale_records(output_path: Path, screened_out: set[str],
                         forced: set[str]) -> int:
    """Drop stored pass-1 records the CURRENT abstract screen contradicts.

    The pass-1 JSONL is append-only and resume skips already-decided papers —
    so without this, a re-screen that flips a paper to irrelevant/excluded
    never invalidates its old ``eligible_for_pass2: true``, and
    ``stage_eligible`` faithfully rebuilds the pass-2 bucket from the stale
    verdict (root cause of a 329-extracted vs 33-included run). Rewrites the
    file without records whose paper is now screened out (force-includes
    exempt); the paper then has NO pass-1 record, which is the correct state
    for a screened-out paper. Returns the number of records dropped.
    """
    if not output_path.exists() or not screened_out:
        return 0

    def _stale(rec: dict) -> bool:
        pid = norm_paper_id(rec.get("paper_id") or "")
        return bool(pid) and pid in screened_out and pid not in forced

    # Malformed lines are kept verbatim (not ours to judge); atomic rewrite.
    return drop_jsonl_records(output_path, _stale)


def _enforce_eligibility(result: dict, field_flags: dict) -> None:
    """Recompute the eligibility gate in Python and enforce it.

    ``build_pass1_prompt`` only ASKS the model to set ``eligible_for_pass2``
    from its component verdicts — nothing checked that it actually did, so a
    self-contradictory record (``organism: "mouse"`` but ``eligible: true``)
    sailed straight into the pass-2 bucket. The model's self-report is no
    longer load-bearing: any explicit disqualifying component verdict flips
    eligibility off, with a ``gate_override_*`` warning naming why.

    Only CLEAR negatives flip the gate (verdict "no", ``has_control_arm``
    False, a named non-human organism). "unclear"/null stay with the model's
    call — over-strictness here would silently shrink the eligible set.
    """
    screening = result.get("screening")
    if not isinstance(screening, dict) or not screening.get("eligible_for_pass2"):
        return

    def _verdict(key):
        v = screening.get(key)
        return v.get("verdict") if isinstance(v, dict) else v

    reasons = []
    if _verdict("reports_original_data") is False:
        reasons.append("not_original_report")
    if _verdict("is_protocol_not_article") is True:
        reasons.append("protocol")
    if field_flags.get("study_design_and_control_arm"):
        ict = screening.get("is_clinical_trial")
        if isinstance(ict, dict):
            observational_ok = (
                field_flags.get("include_observational")
                and (ict.get("study_design") or "").strip().lower() == "observational"
            )
            # An observational review accepts a non-trial design, so "not a trial"
            # is only disqualifying when the review is trial-only. Mirrors the
            # prompt gate above — the two must agree, or this enforcement would
            # silently re-block what the prompt just allowed.
            if ict.get("verdict") == "no" and not observational_ok:
                reasons.append("not_a_trial")
            if ict.get("has_control_arm") is False and not observational_ok:
                reasons.append("no_control_arm")
    if field_flags.get("organism") and not field_flags.get("include_animal_studies"):
        org = _verdict("organism")
        if org in ("mouse", "other_animal", "other"):
            reasons.append(f"organism_{org}")
    if field_flags.get("relevance_recheck") and _verdict("is_relevant") == "no":
        reasons.append("not_relevant")

    if reasons:
        screening["eligible_for_pass2"] = False
        result.setdefault("_warnings", []).append(
            "gate_override_" + "+".join(reasons))


def _source_file_for(ref: PaperRef, source: str) -> str | None:
    """The file this verdict was read from, for the record's audit trail."""
    path = {"md": ref.md_path, "xml": ref.xml_path,
            "pdf": ref.pdf_path}.get(source)
    return path.name if path else None


def extract_one(ref: PaperRef, seq: int, total: int, prompt_template: str,
                model: str, field_flags: dict | None = None,
                xml_first: bool = True) -> dict:
    """Extract data from a single paper. Returns result dict."""
    paper_id = ref.paper_id
    if run_halted():
        # Credential rejected / usage stop: return without a verdict, so the
        # paper is neither recorded as failed nor charged.
        return {"paper_id": paper_id, "_halted": True}

    cost_usd = 0.0
    elapsed = 0.0
    input_tokens = 0
    output_tokens = 0

    # Tables are excluded here on purpose: this pass decides ELIGIBILITY and
    # never needs a number out of a table, and it runs on every screened-in
    # paper — so carrying table markup corpus-wide would be pure cost.
    full_text, source = read_best_text(ref, max_chars=50000,
                                       xml_first=xml_first,
                                       xml_max_chars=50000, xml_tables=False,
                                       reject_boilerplate=True)
    if full_text is None:
        if source == "boilerplate":
            log_status(seq, total, "SKIP", "boilerplate-only text")
            return {"paper_id": paper_id, "_status": "low_quality_text"}
        log_status(seq, total, "SKIP", "no extractable text")
        return {"paper_id": paper_id, "_status": "no_text"}

    # `format_note` is passed unconditionally: an unused kwarg is ignored, but a
    # template carrying the placeholder without it raises KeyError.
    prompt = prompt_template.format(full_text=full_text, paper_id=paper_id,
                                    format_note=format_note_for(source))

    log_status(seq, total, "START", f"{len(full_text):,} chars [{source}]")

    result = None
    for attempt in range(MAX_RETRIES):
        resp = call_claude(prompt, model, paper_id, timeout=CLI_TIMEOUT)
        if run_halted():
            return {"paper_id": paper_id, "_halted": True}
        elapsed += resp.elapsed
        cost_usd += resp.cost_usd
        input_tokens += resp.input_tokens
        output_tokens += resp.output_tokens
        result = _parse_extraction(resp.text, paper_id)
        if result is not None:
            break
        if not is_truncated(resp.text) and is_refusal(resp.text):
            # Prose refusal (not-a-trial, empty text, …): retrying re-buys the
            # same answer. Terminal — and correctly ineligible for pass 2.
            log_status(seq, total, "SKIP", f"model refusal  {paper_id}")
            return {"paper_id": paper_id, "_status": "refused",
                    "_reason": resp.text.strip()[:500]}
        if attempt < MAX_RETRIES - 1:
            log_status(seq, total, "RETRY", f"attempt {attempt + 2}/{MAX_RETRIES}")
            time.sleep(2)

    if result is None:
        log_status(seq, total, "FAIL", f"${cost_usd:.3f}  {MAX_RETRIES} attempts")
        return {"paper_id": paper_id, "_status": "parse_error"}

    if field_flags is not None:
        _enforce_eligibility(result, field_flags)

    screening = result.get("screening") or {}
    eligible = screening.get("eligible_for_pass2", False)
    is_protocol = screening.get("is_protocol_not_article", {})
    if isinstance(is_protocol, dict) and is_protocol.get("verdict") is True:
        tag = "PROTOCOL"
    elif eligible:
        tag = "eligible"
    else:
        tag = "excluded"
    title = ((result.get("metadata") or {}).get("title") or "")[:60]
    warns = result.get("_warnings", [])
    warn_str = f"  [{', '.join(warns)}]" if warns else ""
    log_status(seq, total, "DONE", f"{tag:<8s}  ${cost_usd:.3f}  {elapsed:5.1f}s  {title}{warn_str}")

    result["_status"] = "ok"
    result["_cost"] = cost_usd
    result["_elapsed"] = elapsed
    result["_input_tokens"] = input_tokens
    result["_output_tokens"] = output_tokens
    # Which source the verdict was actually read from. Computed here since the
    # first release and thrown away ever since, which made "why did this paper
    # screen out?" unanswerable whenever the answer was "we read the wrong file".
    result["_text_source"] = source
    result["_source_file"] = _source_file_for(ref, source)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Pass 1: Eligibility screening and basic metadata extraction from PDFs")
    parser.add_argument(
        "--config", required=True,
        help="Path to the review's YAML config")
    parser.add_argument("--input", default=None,
                        help="Directory of papers to screen (default: the "
                             "project's paper store)")
    parser.add_argument("--output", default=None,
                        help="Output JSONL path "
                             "(default: projects/{slug}/data/trial_extractions.jsonl)")
    parser.add_argument("--workers", type=int, default=3,
                        help="Number of concurrent workers (default: 3)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit number of files to process (0 = all)")
    parser.add_argument("--no-screen-filter", action="store_true",
                        help="Also screen papers the abstract screen ruled out "
                             "(is_relevant=no / is_excluded=yes). By default "
                             "they are skipped — each one costs a full-text "
                             "LLM call that can only re-discover the verdict.")
    args = parser.parse_args()

    # Load config
    cfg = load_config(args.config)
    topic_name = cfg.get("topic", {}).get("name", "the condition")
    model = resolve_model(cfg.get("models", {}).get("extraction_pass1", "sonnet"))
    paths = project_paths(cfg)
    print(f"Project: {paths['topic_name']!r} -> {paths['project_dir']}",
          file=sys.stderr, flush=True)
    set_log_dest_dir(paths["project_dir"] / "claude_code_logs")

    # Resolve input directory: --input, else the project's own paper store
    # (legacy converted/ for an un-migrated project). NOT dirs.eligible_trials:
    # that is the bucket stage_eligible.py rebuilds from THIS pass's verdicts,
    # so screening it was circular — and in a studio run a user-supplied path
    # there could point at another review's papers. resolve_input_dir never
    # collapses to "." (which would glob the whole repo).
    fallback = paths["papers_dir"] if paths["papers_dir"].exists() else paths["converted_dir"]
    input_dir = resolve_input_dir(args.input, None, fallback)

    # Load base template and assemble prompt from configured field flags.
    # slug=: a per-project prompt override (prompts/<slug>/) must win.
    base_template = ensure_format_note_placeholder(
        load_prompt_template("extract_pass1.txt", slug=paths["slug"]))
    field_flags = dict(PASS1_FIELD_DEFAULTS)
    field_flags.update(cfg.get("extraction_pass1_fields") or {})
    # Review-level animal policy (not an extraction-field flag): default is
    # human-only, so non-human papers are marked ineligible. Threaded through
    # field_flags so build_pass1_prompt can gate `eligible_for_pass2` on it.
    field_flags["include_animal_studies"] = bool(cfg.get("include_animal_studies", False))
    # Review-level design policy, same shape as the animal policy above. When the
    # review includes observational studies, eligibility must NOT require a
    # randomized/controlled design — see the gate in build_pass1_prompt.
    field_flags["include_observational"] = bool(cfg.get("include_observational", True))
    prompt_template = build_pass1_prompt(
        base_template, topic_name, field_flags, cfg.get("topic"))
    # Publisher XML first, same default as pass 2 (config key `fulltext.xml_first`).
    xml_first = bool((cfg.get("fulltext") or {}).get("xml_first", True))
    enabled = [k for k, v in field_flags.items() if v]
    print(f"Pass-1 optional fields enabled: {enabled or '(minimal only)'}",
          file=sys.stderr, flush=True)
    # {full_text} and {paper_id} are filled per-paper in extract_one()

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = paths["data_dir"] / "trial_extractions.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Gather papers (nested converted/<stem>/ or flat layout).
    if args.input:
        papers = discover_papers(input_dir)
    else:
        from ber_core.paper_store import discover_project_papers
        papers = discover_project_papers(paths["project_dir"], root=paths["papers_dir"])
    print(f"Found {len(papers)} papers in {input_dir}", file=sys.stderr, flush=True)

    # Skip papers the abstract screen already ruled out (unless force-included).
    # Pass 1 legitimately reads the whole converted corpus, but a paper the
    # screen marked irrelevant/excluded only reaches full text through stale
    # artifacts — and each one costs a full-price call that can only
    # re-discover the verdict. Fail-open: papers with no classification row
    # are KEPT (standalone runs where the classify step never ran).
    if not args.no_screen_filter:
        screened_out = _screened_out_ids(paths["data_dir"])
        if screened_out:
            forced = load_inclusion_dois(paths["data_dir"] / "user_inclusions.json")
            n_before = len(papers)
            papers = [r for r in papers
                      if norm_paper_id(r.paper_id) not in screened_out
                      or norm_paper_id(r.paper_id) in forced]
            n_skip = n_before - len(papers)
            if n_skip:
                print(f"Screen filter: skipping {n_skip} of {n_before} converted "
                      f"papers (screened out at the abstract stage; "
                      f"--no-screen-filter keeps them)",
                      file=sys.stderr, flush=True)
            # And retire stored verdicts the current screen contradicts, so a
            # re-screen actually propagates: append-only resume would otherwise
            # keep a stale eligible_for_pass2=true staging the paper forever.
            n_pruned = _prune_stale_records(output_path, screened_out, forced)
            if n_pruned:
                print(f"Screen filter: pruned {n_pruned} stored pass-1 "
                      f"verdict(s) for papers the current screen rules out",
                      file=sys.stderr, flush=True)

    # Load existing results for resumability. `no_text` is not a verdict: the
    # paper's text may have arrived since (body.md converted, XML fetched,
    # PyMuPDF installed), so those records are dropped and re-tried — free
    # when there is still nothing to read.
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
    print(f"Processing {len(papers)} papers with {args.workers} workers",
          file=sys.stderr, flush=True)

    total = len(papers)
    write_lock = threading.Lock()
    counter = {"done": 0, "total": total, "total_cost": 0.0, "eligible": 0,
               "errors": 0, "skipped": 0, "protocols": 0,
               "total_input_tokens": 0, "total_output_tokens": 0,
               "start": time.monotonic()}

    def _progress():
        # utils.log_progress, not a private copy: it emits the one format the
        # server's parse_progress treats as canonical, and this script's
        # hand-rolled ETA was a line-for-line duplicate of it.
        log_progress(
            counter["done"], counter["total"],
            cost=counter["total_cost"], start_time=counter["start"],
            extra=(f"eligible: {counter['eligible']}  "
                   f"protocols: {counter['protocols']}  "
                   f"errors: {counter['errors']}  "
                   f"skipped: {counter['skipped']}"),
        )

    # Emit progress up front so the total is known immediately (the studio's live
    # bar otherwise reads "starting…" until the first batch finishes).
    if total:
        _progress()

    pool = ThreadPoolExecutor(max_workers=args.workers)
    halted = interrupted = False
    try:
        futures = {pool.submit(extract_one, r, i + 1, total, prompt_template,
                               model, field_flags, xml_first): r
                   for i, r in enumerate(papers)}
        try:
            for future in as_completed(futures):
                if run_halted() and not halted:
                    # Credential rejected / usage stop. Cancel everything still
                    # queued, but keep draining: verdicts are written HERE, on
                    # the main thread, and the in-flight few that finished
                    # before the halt are paid for.
                    halted = True
                    for pending in futures:
                        pending.cancel()
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                except Exception as exc:
                    # Mirror extract_pass2.py: an unhandled per-paper error
                    # must not take down the whole screen. Unguarded, one
                    # crashing paper aborted the run and discarded every
                    # in-flight result; the error is recorded instead so the
                    # paper is retried on the next (resumable) run.
                    paper_id = futures[future].paper_id
                    print(f"  {RED}UNHANDLED ERROR: {paper_id}: {exc}{RESET}",
                          file=sys.stderr, flush=True)
                    result = {"paper_id": paper_id, "_status": "error"}

                if result.pop("_halted", False):
                    continue

                cost_usd = result.pop("_cost", 0.0)
                result.pop("_elapsed", 0.0)
                in_tok = result.pop("_input_tokens", 0)
                out_tok = result.pop("_output_tokens", 0)
                status = result.pop("_status", "ok")

                result["_status"] = status

                with write_lock:
                    # Durable per record (fsync), like pass 2: a crash must not
                    # lose verdicts the run already paid for.
                    with open(output_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(result) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                    counter["done"] += 1
                    counter["total_cost"] += cost_usd
                    counter["total_input_tokens"] += in_tok
                    counter["total_output_tokens"] += out_tok
                    if status == "ok" and (result.get("screening") or {}).get("eligible_for_pass2"):
                        counter["eligible"] += 1
                    is_prot = (result.get("screening") or {}).get("is_protocol_not_article", {})
                    if isinstance(is_prot, dict) and is_prot.get("verdict") is True:
                        counter["protocols"] += 1
                    if status in ("parse_error", "error"):
                        counter["errors"] += 1
                    if status == "no_text":
                        counter["skipped"] += 1

                    # Every completion, so the studio bar advances paper-by-paper.
                    _progress()
        except KeyboardInterrupt:
            print("\n  Interrupted! Shutting down workers...", file=sys.stderr, flush=True)
            interrupted = True
            raise
    finally:
        if halted:
            print(f"  {RED}Stopping: LLM credential rejected or usage limit "
                  f"reached. Queued papers cancelled; finished verdicts kept.{RESET}",
                  file=sys.stderr, flush=True)
        pool.shutdown(wait=not interrupted, cancel_futures=True)

    _progress()
    print(
        f"\n  Done. {counter['done']} papers processed | "
        f"eligible: {counter['eligible']} | protocols: {counter['protocols']} | "
        f"errors: {counter['errors']} | skipped: {counter['skipped']} | "
        f"cost: ${counter['total_cost']:.2f}",
        file=sys.stderr, flush=True,
    )
    emit_run_summary("8_extract_pass1", counter, outputs=[output_path],
                     model=model, data_dir=output_path.parent)


if __name__ == "__main__":
    main()
    # A rejected credential (expired setup-token, revoked API key) is fatal and
    # unrecoverable — but every per-paper worker catches Exception so one bad
    # paper cannot abort a run, which would turn this into "every paper failed"
    # and a successful-looking run that produced nothing. claude_cli latches the
    # failure and short-circuits the remaining calls; this converts that into a
    # non-zero exit, from the main thread, once writes have flushed.
    raise_if_auth_failed()
