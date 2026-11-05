"""Shared pipeline constants.

Centralizes the extraction `_status` sentinels, the outcome-direction hints and
the extraction format notes so they live in one place instead of being re-typed
in each stage script.
"""

import re

# --- Extraction _status sentinels ---------------------------------------------
# Failure sentinels. A record carrying one of these has no usable extraction and
# is never published, pooled or counted as included.
STATUS_NO_TEXT = "no_text"          # no extractable paper text
STATUS_PARSE_ERROR = "parse_error"  # response never parsed as valid JSON
STATUS_ERROR = "error"              # generic extraction error
STATUS_NO_JSON = "no_json"          # no JSON found in the response
STATUS_TRUNCATED = "truncated"      # output hit the length limit
STATUS_POLICY_SKIP = "policy_skip"  # model declined on policy grounds (pass-2)
STATUS_REFUSED = "refused"          # prose refusal, no retries
STATUS_INELIGIBLE = "ineligible"    # schema-compliant all-null extraction
STATUS_LOW_QUALITY_TEXT = "low_quality_text"  # boilerplate-only converted text
STATUS_FAILED = "failed"            # generic failure label used by older tooling

# THE definition of "this record carries no usable extraction". Every non-"ok"
# status the extraction passes can write is here; "refused", "ineligible" and
# "low_quality_text" are terminal verdicts that resumability treats as done, but
# no consumer may pool, publish or count them as data.
# ``utils.FAILED_EXTRACTION_STATUSES`` is an alias of this set.
FAILED_STATUS = frozenset({
    STATUS_NO_TEXT,
    STATUS_PARSE_ERROR,
    STATUS_ERROR,
    STATUS_NO_JSON,
    STATUS_TRUNCATED,
    STATUS_POLICY_SKIP,
    STATUS_REFUSED,
    STATUS_INELIGIBLE,
    STATUS_LOW_QUALITY_TEXT,
    STATUS_FAILED,
})


def is_failed_status(status) -> bool:
    """True when an extraction ``_status`` marks the record as unusable."""
    return status in FAILED_STATUS

# --- Country-name canonicalization --------------------------------------------
# Free-text country names extracted by the LLM arrive in many spellings for the
# same place ("USA" / "US" / "United States of America" all mean "United States";
# "Russian Federation" means "Russia"). The dashboard's "Trials by country" map
# joins these names onto world-atlas polygons and, before this map existed, two
# spellings of one country could land on the same polygon and OVERWRITE each
# other — e.g. the US once showed 2 trials because "United States of America" (2
# records) clobbered "United States" (238). Canonicalizing at export time means
# every dashboard feed carries one name per country, so counts merge correctly
# and future review dashboards inherit the fix for free.
#
# Canonical values are chosen to be the common English short name; the website's
# CountryMap is responsible for the final canonical -> geo-polygon-name step
# (e.g. "United States" -> "United States of America") and MUST also accumulate
# rather than overwrite, as a defense in depth. Keys are matched case- and
# whitespace-insensitively (see canonicalize_country); add new synonyms here.
#
# Genuinely multi-country / region values ("Global", "Multiple", "Europe", "165
# countries worldwide") have no single polygon and are intentionally left as-is
# (they simply don't render on the map). Map them only if a real country is meant.
#
# canonicalize_country also strips a single TRAILING "(...)" group as a fallback
# (after a direct lookup), to recover trial-ID / annotation contamination like
# "United States (SP792)" or "Germany (Europe - SP790)". Parenthetical synonym
# keys below (e.g. "iran (islamic republic of)") still resolve because the direct
# lookup runs first.
COUNTRY_SYNONYMS = {
    # United States
    "usa": "United States",
    "u.s.a.": "United States",
    "us": "United States",
    "u.s.": "United States",
    "united states of america": "United States",
    "america": "United States",
    # United Kingdom
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "britain": "United Kingdom",
    # Russia
    "russian federation": "Russia",
    # South Korea
    "republic of korea": "South Korea",
    "korea": "South Korea",
    "korea, republic of": "South Korea",
    # North Korea
    "democratic people's republic of korea": "North Korea",
    # Netherlands
    "the netherlands": "Netherlands",
    "holland": "Netherlands",
    # Turkey
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    # Czechia
    "czech republic": "Czech Republic",  # keep; website maps -> "Czechia"
    "czechia": "Czech Republic",
    # Iran
    "islamic republic of iran": "Iran",
    "iran (islamic republic of)": "Iran",
    # China (mainland; SAR variants kept separate where the polygon differs)
    "people's republic of china": "China",
    "mainland china": "China",
    # Egypt
    "arab republic of egypt": "Egypt",
    # Syria
    "syrian arab republic": "Syria",
    # Vietnam
    "viet nam": "Vietnam",
    # Tanzania
    "united republic of tanzania": "Tanzania",
    # Bolivia / Venezuela / Moldova (common long forms)
    "bolivia (plurinational state of)": "Bolivia",
    "venezuela (bolivarian republic of)": "Venezuela",
    "republic of moldova": "Moldova",
    # Belarus
    "republic of belarus": "Belarus",
    # Misc edge/stale names. "USSR" maps to its closest mappable modern polygon
    # (imperfect, but better than dropping the trial off the map entirely).
    "ussr": "Russia",
    "hong kong, china": "Hong Kong",
}


_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def canonicalize_country(name: str) -> str:
    """Map a free-text country name to its canonical short form.

    Case- and surrounding-whitespace-insensitive. Lookup order:
      1. Direct synonym match (so parenthetical synonym keys like
         "iran (islamic republic of)" still resolve).
      2. If that misses and the value ends in a "(...)" group, strip that single
         trailing group and retry — recovers trial-ID / annotation contamination
         such as "United States (SP792)" -> "United States".
      3. Otherwise pass through unchanged (trimmed).

    Multi-country/region strings are not special-cased and pass through as-is
    (region blobs like "Europe (multiple)" become "Europe", still non-mappable) —
    see COUNTRY_SYNONYMS for the rationale.
    """
    if not isinstance(name, str):
        return name
    base = name.strip()
    direct = COUNTRY_SYNONYMS.get(base.lower())
    if direct is not None:
        return direct
    stripped = _TRAILING_PAREN.sub("", base).strip()
    if stripped and stripped != base:
        return COUNTRY_SYNONYMS.get(stripped.lower(), stripped)
    return base


# NOTE: CLI_TIMEOUT_* and DEFAULT_MODELS used to live here. They were never
# imported — each stage script defines its own CLI_TIMEOUT / default model
# literal — so they were a copy that could silently drift from the values they
# claimed to mirror. The real ones are in classify.py, extract_pass1.py and
# extract_pass2.py. If a shared source is ever wanted, import it there rather
# than re-adding a mirror.

# --- Article type taxonomy ----------------------------------------------------
# The canonical vocabulary handed to the `type`/`quick` classifiers, and the set
# their answers are validated against. A review's config.yaml `article_types` key
# normally carries its own copy; this is the default seeded into new reviews and
# the fallback when a config omits the key.
#
# A missing/empty list is NOT harmless: classify.py renders it into the prompt,
# so an empty list asks the model to pick from nothing and it invents free-text
# labels. Keep this list in sync with the mirror in
# server/app/services/workspace.py (the server deliberately does not import the
# pipeline package); test_classification_config.py asserts the two are identical.
#
# Strings are load-bearing beyond the prompt: ber_core/projects.py slugifies them into
# projects/*/pdfs/<type>/ directory names that already exist on disk.
from ber_core import defaults as _defaults

ARTICLE_TYPES = _defaults.ARTICLE_TYPES  # the one definition lives in ber_core.defaults

# Values written by earlier taxonomy revisions that still appear in existing
# classifications.csv files (long_covid alone has ~17k "Observational/Cohort
# Study" rows). New classifications never produce these, but any code that
# decides eligibility by article_type must still recognize them or it will
# silently reclassify tens of thousands of already-processed papers.
LEGACY_ARTICLE_TYPES = (
    "Observational/Cohort Study",  # superseded by the three specific cohort types
    "Case Report/Series",          # split into "Case Report" + "Case Series"
)

# Types worth fetching a full PDF for: primary studies with extractable results.
# Reviews, editorials, protocols and errata are deliberately excluded — the
# pipeline extracts trial data, not commentary.
#
# This is the default for download_pdfs.py's --types filter. Too narrow a set
# is a silent pipeline killer: if no classified paper matches, the download step
# fetches nothing, leaves pdfs/ empty and fails output verification.
PDF_ELIGIBLE_TYPES = (
    "Original Research",
    "Clinical Trial",
    "Prospective Cohort Study",
    "Retrospective Cohort Study",
    "Other Observational Study",
    "Case-Control Study",
    "Cross-Sectional Study",
    "Survey Study",
    "Qualitative Study",
    "Mixed Methods Study",
) + LEGACY_ARTICLE_TYPES

# Types counted as "passed the article-type screen" in the PRISMA flow diagram.
# Kept separate from PDF_ELIGIBLE_TYPES on purpose: one is a reporting criterion,
# the other a fetch filter, and conflating them couples two unrelated decisions.
PRISMA_ELIGIBLE_TYPES = (
    "Original Research",
    "Clinical Trial",
    "Prospective Cohort Study",
    "Retrospective Cohort Study",
    "Other Observational Study",
    "Case-Control Study",
    "Cross-Sectional Study",
    "Survey Study",
    "Qualitative Study",
    "Mixed Methods Study",
) + LEGACY_ARTICLE_TYPES


# --- Outcome orientation sanity check ----------------------------------------
# `higher_is_better` decides a SIGN FLIP on every effect an outcome contributes
# (grading.smd_from_outcome: `sign = 1.0 if hib else -1.0`), and a null value
# drops the study from the pool entirely. The extraction prompt says as much:
# "a wrong or missing value silently biases the pooled estimate". Nothing
# checked it until now.
#
# These are instruments whose direction is not a judgment call. Matching is by
# case-insensitive substring against the outcome's instrument/name, so keep the
# keys distinctive enough not to collide (e.g. "bmi" would match "bmi z-score",
# which is fine — same direction).
#
# Absent from this table => unknown => no opinion, no flag. Adding an entry is a
# claim that the direction is unambiguous, so add conservatively.
OUTCOME_DIRECTION_HINTS: dict[str, bool] = {
    # Lower is better — disease burden, symptom severity, risk markers.
    "bmi": False,
    "body weight": False,
    "body mass index": False,
    "waist circumference": False,
    "systolic blood pressure": False,
    "diastolic blood pressure": False,
    "ldl": False,
    "triglyceride": False,
    "hba1c": False,
    "fasting glucose": False,
    "c-reactive protein": False,
    "crp": False,
    "fibrinogen": False,
    "d-dimer": False,
    "pain": False,
    "fatigue severity": False,
    "depression": False,
    "anxiety": False,
    "mortality": False,
    "adverse event": False,
    "relapse": False,
    "hospitalization": False,
    "viral load": False,
    "irls": False,               # International RLS severity scale
    "epworth": False,            # Epworth Sleepiness Scale
    "phq-9": False,
    "gad-7": False,
    "hamilton": False,
    # Higher is better — function, capacity, quality of life.
    "quality of life": True,
    "sf-36": True,
    "eq-5d": True,
    "6-minute walk": True,
    "6mwt": True,
    "grip strength": True,
    "vo2 max": True,
    "hdl": True,
    "responder rate": True,
    "remission rate": True,
    "adherence": True,
}


def expected_direction(instrument: str) -> "bool | None":
    """Known `higher_is_better` for an instrument, or None when unknown.

    Longest key wins, so a specific instrument beats a generic substring.
    """
    s = (instrument or "").strip().lower()
    if not s:
        return None
    hits = [(k, v) for k, v in OUTCOME_DIRECTION_HINTS.items() if k in s]
    if not hits:
        return None
    return max(hits, key=lambda kv: len(kv[0]))[1]


# --- Extraction format notes -------------------------------------------------
# Interpolated into the extraction prompts as {format_note}, chosen by which
# source `utils.read_best_text` actually returned. Telling a model reading raw
# publisher XML that "some tables use | pipe separators" is worse than saying
# nothing: it describes a format that is not in front of it.

#: PDF -> markdown text (docling pipe tables, GROBID prose).
FORMAT_NOTE_MD = ("NOTE: In this text, § means ±, &lt; means <, &gt; means >. "
                  "Some tables use | pipe separators.")

#: pymupdf text extraction — same entity conventions, no table structure at all.
FORMAT_NOTE_PDF = FORMAT_NOTE_MD

#: Raw publisher XML. The CALS sentence matters: that model is what fetchpdf's
#: JATS->Markdown converter refuses to handle, so it only ever reaches a model
#: through this path, and a model that ignores namest/nameend reads a spanning
#: header as a single column and shifts every value beside it.
FORMAT_NOTE_XML = (
    "NOTE: The text below is the publisher's original full-text XML, not prose. "
    "Tables are real markup: read <th>/<td> and honor colspan/rowspan, AND the "
    "CALS attributes namest/nameend/morerows, when mapping a cell to its column "
    "— a spanning header that you read as one column shifts every value to its "
    "right. Numbers in tables are authoritative; prefer them over any prose "
    "restatement. References and front matter have been removed. When filling a "
    "*_quote field, quote the TEXT ONLY — never include XML tags."
)

#: The model is reading the PDF's pages as images via its own file-read tool.
FORMAT_NOTE_PDF_NATIVE = (
    "NOTE: You are reading the paper's PDF pages directly as images. Read every "
    "table cell off the page, including footnotes under the table — they carry "
    "the units and denominators the numbers above them depend on. Quote text "
    "exactly as printed."
)

FORMAT_NOTES = {
    "md": FORMAT_NOTE_MD,
    "xml": FORMAT_NOTE_XML,
    "pdf": FORMAT_NOTE_PDF,
    "pdf-native": FORMAT_NOTE_PDF_NATIVE,
}


def format_note_for(source: str) -> str:
    """The prompt note matching a `read_best_text` source tag."""
    return FORMAT_NOTES.get(source, FORMAT_NOTE_MD)
