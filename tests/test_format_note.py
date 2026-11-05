"""Every extraction template must format with (full_text, paper_id, format_note).

The asymmetry that makes this safe is also what makes it a trap: `.format()`
IGNORES an unused kwarg but RAISES on a placeholder the caller didn't supply. So
callers must pass `format_note` unconditionally, and any template carrying the
placeholder must be reachable by a caller that does.

The four per-project overrides were forked before the placeholder existed and
still hardcode the markdown-only note — which is actively wrong in front of raw
XML, since it describes a format that is not there.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ber_core.constants import (
    FORMAT_NOTE_MD, FORMAT_NOTE_XML, FORMAT_NOTES, format_note_for,
)
from ber_core.utils import ensure_format_note_placeholder

TEMPLATES = sorted(Path("prompts").rglob("extract_pass*.txt"))


def test_there_are_templates_to_check():
    # A glob that silently matches nothing would make every test below vacuous.
    # The shipped set is pass 1 + pass 2 RCT + pass 2 observational; local
    # per-review overrides (prompts/<slug>/, git-ignored) are checked too.
    assert len(TEMPLATES) >= 3


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p))
def test_every_template_formats_with_the_three_kwargs(path):
    raw = ensure_format_note_placeholder(path.read_text(encoding="utf-8"))
    # Config-level placeholders are filled by .replace() before .format() ever
    # runs, so strip them the same way the loaders do.
    for token in ("{topic_name}", "{intervention_categories_str}",
                  "{outcome_domains_str}", "{topic_condition_field}",
                  "{json_schema}", "{rules}"):
        raw = raw.replace(token, "X")
    body = raw.split("USER:", 1)[-1]
    body.format(full_text="TEXT", paper_id="10.1/a", format_note="NOTE")


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p))
def test_every_template_ends_up_with_a_placeholder(path):
    """Either it ships with one, or the legacy note is swapped for one."""
    raw = ensure_format_note_placeholder(path.read_text(encoding="utf-8"))
    assert "{format_note}" in raw, f"{path} would get no format note at all"


def test_the_legacy_note_is_replaced_not_duplicated():
    legacy = ("Preamble\n\nNOTE: In this text, § means ±, &lt; means <, "
              "&gt; means >. Some tables use | pipe separators.\n\nRest")
    out = ensure_format_note_placeholder(legacy)
    assert "{format_note}" in out
    assert "pipe separators" not in out


def test_a_template_that_already_has_the_placeholder_is_untouched():
    raw = "before {format_note} after"
    assert ensure_format_note_placeholder(raw) == raw


def test_a_template_with_neither_is_left_alone():
    """No placeholder and no legacy line: it simply gets no note, not a crash."""
    raw = "just <paper>{full_text}</paper> and {paper_id}"
    out = ensure_format_note_placeholder(raw)
    assert out == raw
    out.format(full_text="t", paper_id="p", format_note="ignored")


# --- the notes themselves ---------------------------------------------------

def test_xml_note_names_both_table_models():
    """A model that ignores CALS spans reads a spanning header as one column and
    shifts every value beside it — and CALS only ever reaches a model through
    the raw-XML path, because the Markdown converter refuses it."""
    for attr in ("colspan", "rowspan", "namest", "nameend"):
        assert attr in FORMAT_NOTE_XML


def test_xml_note_tells_the_model_not_to_quote_tags():
    """Quote verification strips tags from its second haystack; a quote that
    includes markup would defeat that."""
    assert "quote the TEXT ONLY" in FORMAT_NOTE_XML


def test_markdown_note_is_unchanged_for_markdown():
    assert "pipe separators" in FORMAT_NOTE_MD
    assert format_note_for("md") == FORMAT_NOTE_MD


@pytest.mark.parametrize("source", ["md", "xml", "pdf", "pdf-native"])
def test_every_source_has_a_note(source):
    assert FORMAT_NOTES[source] and format_note_for(source)


def test_an_unknown_source_falls_back_to_markdown():
    # read_best_text can return "boilerplate"/"none"; those never reach a prompt,
    # but a missing key must not raise mid-run.
    assert format_note_for("something-new") == FORMAT_NOTE_MD
