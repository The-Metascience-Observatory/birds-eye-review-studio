"""Generate per-source search queries via the Claude CLI."""
from __future__ import annotations

from ber_core.claude_cli import call_claude, parse_json_response

SOURCES = ("pubmed", "openalex_title", "openalex_abstract",
           "europepmc", "semantic_scholar", "cyberleninka")


def _build_prompt(
    *,
    topic_name: str,
    description: str,
    synonyms: list[str],
    mesh_terms: list[str],
    not_about: list[str],
    question: str = "",
) -> str:
    syn_block = "\n".join(f"  - {s}" for s in synonyms) or "  (none)"
    mesh_block = "\n".join(f"  - {m}" for m in mesh_terms) or "  (none)"
    not_block = "\n".join(f"  - {n}" for n in not_about) or "  (none)"
    question_block = (
        f"Research question: {question}\n\n" if (question or "").strip() else ""
    )
    return f"""You are designing literature-search queries for a systematic review.

{question_block}Topic: {topic_name}

Description:
{description or '(none)'}

Synonyms / alternate names:
{syn_block}

MeSH terms (NCBI controlled vocabulary):
{mesh_block}

Out-of-scope (these should NOT be retrieved):
{not_block}

Generate search queries for six literature sources. Each source expects a list
of query strings — multiple queries are run independently (OR'd). Use the
syntax appropriate to each source:

- "pubmed": PubMed/Entrez syntax. Use [Title/Abstract], [MeSH Terms],
  [Supplementary Concept] as appropriate. Quote multi-word phrases.
  Combine synonyms with OR. Typically 1-3 queries.
- "openalex_title": plain phrases that should appear in the title.
  No operators — one phrase per list element.
- "openalex_abstract": plain phrases that should appear in the abstract.
  No operators — one phrase per list element.
- "europepmc": Europe PMC syntax (TITLE:"...", ABSTRACT:"...", AND, OR,
  SRC:MED). Typically 1-2 queries combining title-only and abstract-only.
- "semantic_scholar": short keyword phrases, quoted. One per list element.
- "cyberleninka": Russian-language open-access repository. Provide queries
  in RUSSIAN (Cyrillic) covering the topic and its main synonyms — one
  Russian phrase per list element. CyberLeninka uses simple full-text
  matching (no operators). Typically 2-5 queries; include both formal
  medical terminology and common Russian variants. Skip this source only
  if the topic is intrinsically non-translatable.

Return STRICT JSON only — no prose, no markdown fences. Schema:

{{
  "pubmed": ["...", ...],
  "openalex_title": ["...", ...],
  "openalex_abstract": ["...", ...],
  "europepmc": ["...", ...],
  "semantic_scholar": ["...", ...],
  "cyberleninka": ["...", ...]
}}

Be precise: prefer high-recall queries that capture the topic's variants while
avoiding obvious off-topic matches implied by the out-of-scope list."""


def generate_search_queries(
    *,
    topic_name: str,
    description: str = "",
    synonyms: list[str] | None = None,
    mesh_terms: list[str] | None = None,
    not_about: list[str] | None = None,
    question: str = "",
    model: str = "sonnet",
    timeout: int = 180,
) -> tuple[dict[str, list[str]], dict]:
    """Ask Claude CLI to draft per-source search queries.

    Returns (queries_dict, meta) where queries_dict has the five SOURCES keys
    (each a list of strings; missing keys come back as []) and meta carries
    {cost_usd, input_tokens, output_tokens, elapsed, raw}.

    Raises RuntimeError on subprocess / parse failure.
    """
    if not (topic_name or "").strip():
        raise ValueError("topic_name is required.")

    prompt = _build_prompt(
        topic_name=topic_name.strip(),
        description=description.strip(),
        synonyms=[s.strip() for s in (synonyms or []) if s.strip()],
        mesh_terms=[m.strip() for m in (mesh_terms or []) if m.strip()],
        not_about=[n.strip() for n in (not_about or []) if n.strip()],
        question=(question or "").strip(),
    )

    resp = call_claude(
        prompt=prompt,
        model=model,
        identifier=f"search_queries:{topic_name}",
        timeout=timeout,
        use_stdin=True,  # prompts can be long
    )
    if not resp.text:
        raise RuntimeError("Claude returned an empty response.")

    parsed = parse_json_response(resp.text, identifier=topic_name)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Could not parse JSON from Claude. Raw:\n{resp.text[:600]}")

    out: dict[str, list[str]] = {}
    for src in SOURCES:
        val = parsed.get(src, [])
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            val = []
        out[src] = [str(x).strip() for x in val if str(x).strip()]

    meta = {
        "cost_usd": resp.cost_usd,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "elapsed": resp.elapsed,
        "model": model,
        "raw": resp.text,
    }
    return out, meta
