#!/usr/bin/env python3
"""Build the review's dashboard bundle from the extracted trial data.

Builds the single, merged dashboard feed a dashboard reads and writes the
standardized per-topic bundle (by default into ``projects/<slug>/dashboard/``;
``--website-dir`` targets a website checkout's ``data/birds_eye_reviews/<slug>/``):

    <bundle dir>/
        trial_extractions.jsonl   (one record per line; dashboard's primary feed)
        doi_metadata.json         ({doi: {first_author, author_count, journal, year}})
        trial_screening.csv       (copied through from the project data dir)
        meta_analysis.json        (precomputed forest-plot groups; see below)
        spray_types.csv           (copied through, if present)
        mechanism_of_action.json  (copied through, if present)

THE MERGE (why this script is more than a copy)
-----------------------------------------------
A bird's-eye review extracts RCTs and observational studies into *separate*
files (``trial_extractions_pass2.jsonl`` and ``observational_extractions.jsonl``)
with different schemas (RoB2 vs ROBINS-I enums, n_randomized_total vs
n_enrolled_total). The dashboard consumes a *single* merged feed. This script
unions the two, so re-running the pipeline never silently drops the
observational half of the dataset.

  * ``--no-observational`` reproduces the RCT/pass-2 feed exactly (strict parity).
  * By default the observational file is merged in additively: every status-ok
    observational record whose paper_id is not already in the pass-2 feed is
    appended. No currently-published record is ever removed.

SAFETY GUARDRAIL
----------------
Before writing, the new feed is diffed against the currently-deployed website
file. If any previously-published paper_id would disappear (or any headline
chart metric would shrink), the script reports it and exits non-zero unless
``--allow-shrink`` is passed. This turns "silently lost 511 studies" into a
loud failure.

Pooling (meta_analysis.json): inverse-variance meta-analysis via the shared
meta_analysis.py engine. Methodology (fixed/random effects; DL/PM/REML tau^2;
Wald/Knapp-Hartung CIs; prediction intervals; Egger's + trim-and-fill; leave-
one-out; subgroups) comes from the config `meta_analysis:` block, overridable
per run with --ma-* flags. Defaults reproduce the legacy DL random-effects Wald
pooling. Ratio measures are pooled on the log scale; difference measures on the
raw scale. A group needs >= --min-trials usable trials to get a pooled diamond.

Usage:
    # Full update for a topic (merge RCT + observational), write into a website checkout:
    python steps/export_dashboard.py --config configs/config_my_topic.yaml \
        --website-dir <website checkout>

    # Strict parity check against the current data, writing nowhere:
    python steps/export_dashboard.py --config configs/config_my_topic.yaml \
        --no-observational --no-metadata-fetch --dry-run
"""

import argparse
import copy
import csv
import datetime
import json
import os
import re
import sys
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:  # progress bar is optional
    tqdm = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.utils import (
    is_unpublishable_record, load_config, load_duplicate_dois,
    load_exclusion_dois, log_progress, norm_paper_id, read_jsonl, slugify,
)
from ber_core.constants import canonicalize_country
from ber_core.scientific import is_randomized
# Aggregates + the schema-drift probe live in an importable module so this script
# and generate_dashboard.py (neither of which can be imported normally, being
# numeric-prefixed) share ONE implementation. See dashboard_summary.__doc__.
from ber_core import dashboard_summary as ds
from ber_core import grading as ma_lib
from ber_core.dashboard_summary import (
    chart_metrics as _chart_metrics,
    validate_contract as _validate_contract,
)
# The pooling statistics live in meta_analysis.py, shared with pool_meta_analysis.
from ber_core import meta_analysis as ma

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

# Extraction-failure sentinels (`_status`) are constants.FAILED_STATUS, applied
# through utils.is_unpublishable_record. REQUIRED_KEYS / KNOWN_ROB /
# KNOWN_ARM_TYPE live in dashboard_summary.py.


# ── IO helpers ───────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    """The feed's records. A corrupt line ABORTS the export (deliberately):
    publishing a silently smaller trial set is worse than not publishing."""
    return read_jsonl(path, on_error="raise")


def _load_spray_types(path: Path) -> dict[str, str]:
    """norm DOI -> spray_type from spray_types.csv (empty dict if absent).

    Keyed by norm_paper_id, the same normalization every lookup uses; a raw
    `.strip()` key silently missed any DOI whose case or `--` escaping differed
    from the record's paper_id."""
    out = {}
    if not path.exists():
        return out
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = norm_paper_id(row.get("doi") or "")
            st = (row.get("spray_type") or "").strip()
            if doi and st:
                out[doi] = st
    return out


def _load_ingredient_map(spray_csv: Path, canon_path: Path) -> dict[str, str]:
    """doi -> canonical ingredient (compound) name.

    Raw agent from spray_types.csv,
    canonicalised via agent_canonical_map.json (raw->canonical, only where they
    differ); agents not in the map fall back to their lowercased raw name. Empty
    dict if neither file exists (non-antiviral projects keep spray_type grouping).
    """
    if not spray_csv.exists() or not canon_path.exists():
        return {}
    canon = json.loads(canon_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    with open(spray_csv, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = norm_paper_id(row.get("doi") or "")  # see _load_spray_types
            agent = (row.get("agent") or "").strip()
            if not doi or not agent or agent.lower() == "unspecified":
                continue
            out[doi] = canon.get(agent, agent.lower())
    return out


_load_intervention_canon = ma.load_name_map


def _load_indication_map(path: Path) -> dict[str, str]:
    """paper_id -> indication (treatment|prevention|both|exclude), empty if absent.

    Produced by classify_intervention_indication.py. The JSON value is
    {indication, rationale}; we keep just the indication string.
    """
    if not path.exists():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(m, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in m.items():
            ind = v.get("indication") if isinstance(v, dict) else v
            if isinstance(ind, str) and ind:
                out[str(k)] = ind
        return out
    except (ValueError, json.JSONDecodeError):
        return {}


INDICATION_FOCI = ("treatment", "prevention", "all")
LEGACY_LONG_COVID_PREVENTION = "long_covid_prevention_trials.jsonl"


def indication_focus(config: dict | None) -> tuple[str, bool]:
    """(focus, explicitly_configured) from ``topic.indication_focus``.

    'treatment' (the historical behaviour, and the default) routes prevention
    studies to a side feed; 'prevention' routes treatment studies out instead;
    'all' keeps both and drops only 'exclude'."""
    raw = str(((config or {}).get("topic") or {}).get("indication_focus") or "").strip().lower()
    if raw in INDICATION_FOCI:
        return raw, True
    return "treatment", False


def route_by_indication(records: list[dict], indication: dict, focus: str = "treatment"):
    """Split records by the indication map: (feed, side_feed, side_kind, n_excluded).

    'both' studies are evaluated for either indication, so they stay in the
    feed AND appear in the side feed.
    """
    norm = {norm_paper_id(k): v for k, v in indication.items()}
    side_kind = {"treatment": "prevention", "prevention": "treatment"}.get(focus)
    feed, side, n_excluded = [], [], 0
    for r in records:
        ind = indication.get(str(r.get("paper_id", ""))) or norm.get(norm_paper_id(r.get("paper_id")))
        if ind == "exclude":
            n_excluded += 1
            continue
        if side_kind and ind in (side_kind, "both"):
            side.append(r)
            if ind == side_kind:
                continue
        feed.append(r)
    return feed, side, side_kind, n_excluded


def _load_abstract_only(manifest_path: Path) -> set[str]:
    """Normalized DOIs staged from an abstract (no full text), from the staging manifest."""
    if not manifest_path.exists():
        return set()
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {norm_paper_id(doi) for doi, m in man.items() if m.get("source") == "abstract_only"}


def _apply_arm_splits(records: list[dict], splits_path: Path) -> list[dict]:
    """Expand multi-spray trials into one record per spray arm (arm-splitting).

    ``trial_arm_splits.json`` maps doi -> [{suffix, agent_canonical, spray_type,
    arm_label, keep_comparisons}]. For each matching record we emit one clone per
    arm: paper_id = "<doi>#<suffix>", the arm's agent/spray_type/arm_label set, and
    every outcome's ``between_group_effects`` filtered to that arm's comparison(s)
    (outcomes left with no effects are dropped). The original record is removed, so
    the study is screened/counted once but contributes a point to each ingredient.
    """
    if not splits_path.exists():
        return records
    splits = json.loads(splits_path.read_text(encoding="utf-8"))
    splits = {k.strip().lower(): v for k, v in splits.items()}
    if not splits:
        return records
    out: list[dict] = []
    n_split = 0
    for r in records:
        doi = (r.get("paper_id") or "").strip().lower()
        arms = splits.get(doi)
        if not arms:
            out.append(r)
            continue
        for arm in arms:
            keep = set(arm.get("keep_comparisons") or [])
            clone = copy.deepcopy(r)
            clone["paper_id"] = f"{r['paper_id']}#{arm['suffix']}"
            clone["agent_canonical"] = arm["agent_canonical"]
            clone["spray_type"] = arm.get("spray_type") or clone.get("spray_type")
            clone["arm_label"] = arm.get("arm_label", "")
            kept_outcomes = []
            for o in clone.get("outcomes") or []:
                effs = [e for e in (o.get("between_group_effects") or [])
                        if not keep or e.get("comparison") in keep]
                if effs or not (o.get("between_group_effects")):
                    o["between_group_effects"] = effs
                    kept_outcomes.append(o)
            clone["outcomes"] = kept_outcomes
            # Per-arm N: the kept comparison(s) name specific arm_ids (e.g.
            # "arm2_vs_arm4"); set this arm record's total N to the sum of those
            # arms' counts so the table/forest show the per-arm comparison size,
            # not the full multi-arm trial total.
            arm_ids = {int(m) for c in keep for m in re.findall(r"arm(\d+)", c)}
            if arm_ids:
                ss = clone.get("sample_sizes") or {}
                per_arm = ss.get("per_arm") or []
                vals = [a.get("n_randomized") or a.get("n_enrolled")
                        or a.get("n_analyzed_primary")
                        for a in per_arm if a.get("arm_id") in arm_ids]
                vals = [v for v in vals if isinstance(v, (int, float))]
                if vals:
                    total = sum(vals)
                    if ss.get("n_randomized_total") is not None:
                        ss["n_randomized_total"] = total
                    if ss.get("n_enrolled_total") is not None:
                        ss["n_enrolled_total"] = total
                    clone["sample_sizes"] = ss
            out.append(clone)
            n_split += 1
    if n_split:
        print(f"arm-split: expanded {len(splits)} multi-spray trial(s) into {n_split} arm records",
              file=sys.stderr)
    return out


def _attach_related_publications(records: list[dict], path: Path) -> list[dict]:
    """Attach a "see also" list of secondary publications to each kept record.

    ``related_publications.json`` maps a canonical (kept) paper_id -> a list of
    {doi, relation, label} for follow-up analyses or third-party commentaries of
    the same trial. Those secondary DOIs are collapsed out of the feed by
    ``duplicate_dois.json``; this re-surfaces them as labelled links next to the
    primary's reference. Runs before arm-splitting so split clones inherit it.
    """
    if not path.exists():
        return records
    try:
        rel = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return records
    rel = {str(k).strip().lower(): v for k, v in rel.items()
           if not str(k).startswith("_")}
    if not rel:
        return records
    n = 0
    for r in records:
        pid = (r.get("paper_id") or "").strip().lower()
        pubs = rel.get(pid)
        if pubs:
            r["related_publications"] = pubs
            n += 1
    if n:
        print(f"related publications: attached to {n} record(s)", file=sys.stderr)
    return records


def _first_author_surname(authors: str) -> str:
    if not authors:
        return ""
    first = re.split(r"[;,]| and ", authors.strip())[0].strip()
    # "Smith J" -> "Smith"; "J Smith" -> "Smith"
    parts = first.split()
    if not parts:
        return first
    return parts[0] if len(parts[0]) > 2 else parts[-1]


# Failed/empty-record and duplicate-map handling live in utils so the PRISMA
# funnel (ber_core/prisma.py) counts "Included in synthesis" by exactly these
# rules. They were separate implementations, and the funnel's number could
# disagree with the trial count on the same dashboard.
_is_failed = is_unpublishable_record
_load_duplicate_dois = load_duplicate_dois


# ── Merge ────────────────────────────────────────────────────────────────────


def _load_delivery_exclusions(path: "Path | None") -> set:
    """Set of normalized DOIs for trials excluded because the intervention is not
    nasal-delivered (inhaled / oral / gargle / capsule). These are dropped from
    the dashboard feed + meta-analysis; the screening PRISMA counts them as the
    'non-nasal delivery' exclusion.

    Delegates to ``utils.load_exclusion_dois`` so this and pool_meta_analysis.py
    read the file identically. They previously did not: this function required an
    ``{"exclusions": ...}`` wrapper while the meta-analysis also accepted a bare
    mapping, so a bare file silently excluded papers from the pooled estimates
    but not from the published feed.
    """
    return load_exclusion_dois(path)


def _load_organism(path: "Path") -> dict:
    """``{norm_paper_id: organism_verdict}`` from the pass-1 screen.

    Pass 1 stores it as ``screening.organism = {verdict, reasoning}``; only the
    verdict ("human" | "mouse" | "other_animal" | "other") reaches the dashboard.
    A missing file, a record with no organism block, or a pre-migration review
    all yield nothing for that DOI — the caller leaves the field None so the UI
    shows "—" rather than inventing a value.
    """
    if not path or not path.exists():
        return {}
    out: dict = {}
    for rec in read_jsonl(path, on_error="warn"):
        organism = ((rec.get("screening") or {}).get("organism") or {})
        verdict = organism.get("verdict") if isinstance(organism, dict) else organism
        if verdict:
            out[norm_paper_id(rec.get("paper_id"))] = str(verdict)
    return out


def _union_records(pass2: list[dict], observational: list[dict],
                   dup_map: dict | None = None, excluded: set[str] | None = None) -> tuple[list[dict], dict]:
    """Union the published (pass-2) feed with observational extractions.

    Pass-2 records are authoritative (already enriched/summarized). Observational
    records are appended only when their paper_id is not already present and the
    extraction did not fail. Returns (records, stats).

    *dup_map* (duplicate DOI -> canonical DOI) collapses duplicate publications of
    the same study: a record whose normalized paper_id is a known duplicate is
    dropped when its canonical counterpart appears anywhere in the feed.
    """
    from ber_core.identity import paper_join_key
    dup_map = {norm_paper_id(k): norm_paper_id(v) for k, v in (dup_map or {}).items()}
    excluded = excluded or set()

    def _norm(pid: str) -> str:
        # The shared join key, with any `#arm` suffix KEPT: this key dedupes
        # records, and two arms of one paper are two records, not a duplicate.
        base, sep, arm = (pid or "").partition("#")
        return paper_join_key(base) + sep + arm.strip().lower()

    # Canonical DOIs actually present in the feed — only then is dropping a
    # duplicate safe (otherwise the preprint is the only copy and is kept).
    present = {norm_paper_id(r.get("paper_id")) for r in (pass2 + observational)
               if not _is_failed(r) and norm_paper_id(r.get("paper_id")) not in excluded}
    canon_present = {c for c in dup_map.values() if c in present}

    out: list[dict] = []
    seen: set[str] = set()
    dropped_failed = 0
    dropped_dup = 0
    dropped_excluded = 0

    def _consume(records: list[dict]) -> int:
        nonlocal dropped_failed, dropped_dup, dropped_excluded
        added = 0
        for r in records:
            pid = r.get("paper_id")
            norm = _norm(pid)
            # collapse a known duplicate onto its canonical (kept) counterpart
            base_id = norm_paper_id(pid)
            if base_id in excluded:
                dropped_excluded += 1
                continue
            if base_id in dup_map and dup_map[base_id] in canon_present:
                dropped_dup += 1
                continue
            if _is_failed(r):
                dropped_failed += 1
                continue
            if pid in seen or norm in seen:
                continue
            seen.add(pid)
            seen.add(norm)
            out.append(r)
            added += 1
        return added

    n_pass2 = _consume(pass2)
    added_obs = _consume(observational)

    return out, {
        "n_pass2": n_pass2,
        "added_observational": added_obs,
        "dropped_failed": dropped_failed,
        "dropped_duplicates": dropped_dup,
        "dropped_excluded": dropped_excluded,
    }


# ── Enrichment ───────────────────────────────────────────────────────────────

# Biblio fields backfilled onto each record (in priority order of source).
_BIBLIO_KEYS = ("title", "authors", "journal", "year", "volume", "issue",
                "pages", "url")


def _load_local_metadata(screening_csv: "Path | None",
                         enriched_csv: "Path | None") -> dict[str, dict]:
    """doi -> biblio dict, assembled from the already-enriched local CSVs.

    Step 9 (screening enrich) already fetched title/authors/journal/etc. into
    trial_screening.csv (paper_* columns) and classifications_enriched.csv, so the
    export can backfill from disk instead of re-hitting the metadata APIs. Keyed by
    lowercased canonical DOI. trial_screening.csv wins; classifications_enriched.csv
    fills remaining gaps.
    """
    out: dict[str, dict] = {}

    def _put(doi: str, m: dict):
        doi = (doi or "").strip().lower()
        if not doi:
            return
        cur = out.setdefault(doi, {})
        for k, v in m.items():
            v = (v or "").strip() if isinstance(v, str) else v
            if v and not cur.get(k):  # first writer wins (screening loaded first)
                cur[k] = v

    if screening_csv and screening_csv.exists():
        with open(screening_csv, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                _put(row.get("doi"), {
                    "title": row.get("paper_title"), "authors": row.get("paper_authors"),
                    "journal": row.get("paper_journal"), "year": row.get("paper_year"),
                    "volume": row.get("paper_volume"), "issue": row.get("paper_issue"),
                    "pages": row.get("paper_pages"), "url": row.get("paper_url"),
                })
    if enriched_csv and enriched_csv.exists():
        with open(enriched_csv, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                _put(row.get("doi"), {
                    "title": row.get("title"), "authors": row.get("authors"),
                    "journal": row.get("journal"), "year": row.get("year"),
                    "volume": row.get("volume"), "issue": row.get("issue"),
                    "url": row.get("url"),
                })
    return out


def _derive_interventions(record: dict) -> str:
    arms = ((record.get("study_design") or {}).get("arms") or [])
    names = []
    for a in arms:
        if (a.get("type") or "").lower() in ("comparator", "control", "placebo"):
            continue
        nm = a.get("intervention_name")
        if nm and nm not in names:
            names.append(nm)
    return ", ".join(names)


def _enrich(records, spray_types, fetch_meta, local_meta=None):
    """Mutates records in place: spray_type join, is_rct, interventions, biblio.

    Biblio backfill is local-cache-first: missing title/authors/journal/etc. are
    filled from *local_meta* (the already-enriched CSVs, no network); only records
    still lacking a title afterwards fall through to the network *fetch_meta*.

    Returns {doi: {first_author, author_count, journal, year}} for all records.
    """
    from ber_core.utils import filename_to_doi
    local_meta = local_meta or {}
    doi_meta = {}
    n_local = n_network = 0
    iterator = (tqdm(records, desc="biblio backfill", unit="rec")
                if tqdm else records)
    for r in iterator:
        paper_id = r.get("paper_id") or ""
        doi = filename_to_doi(paper_id) if "--" in paper_id else paper_id
        # spray-type join (both sides normalized by norm_paper_id)
        st = spray_types.get(norm_paper_id(paper_id))
        if st:
            r["spray_type"] = st
            for a in ((r.get("study_design") or {}).get("arms") or []):
                if not a.get("intervention_category"):
                    a["intervention_category"] = st
        # derived fields
        # The one randomization definition (ber_core.scientific). The old
        # substring test `"rct" in design_type` called "randomized controlled
        # trial" non-randomized and could not see allocation evidence.
        r["is_rct"] = is_randomized(r)
        if not r.get("interventions"):
            r["interventions"] = _derive_interventions(r)
        # bibliographic backfill — local cache first (no network)
        lm = local_meta.get((doi or "").strip().lower())
        if lm:
            filled = False
            for k in _BIBLIO_KEYS:
                if not r.get(k) and lm.get(k):
                    r[k] = lm[k]
                    filled = True
            if filled:
                n_local += 1
        # only hit the network for what the cache could not supply
        if fetch_meta and not r.get("title"):
            n_network += 1
            try:
                m = fetch_meta(doi) or {}
                for k in ("title", "authors", "journal", "year",
                          "volume", "issue", "pages"):
                    if not r.get(k) and m.get(k):
                        r[k] = m[k]
            except Exception as ex:
                print(f"  WARN metadata {doi}: {ex}", file=sys.stderr)
        authors = r.get("authors", "") or ""
        doi_meta[doi] = {
            # Full author string, not just the surname: the dashboard's hover
            # cards and table render a real citation line. It was computed here
            # and then thrown away, which is why rows had no author.
            "authors": authors,
            "first_author": _first_author_surname(authors),
            "author_count": len([a for a in re.split(r"[;]| and ", authors) if a.strip()]) or None,
            "journal": r.get("journal", ""),
            "year": r.get("year"),
        }
    print(f"Biblio backfill: {n_local} from local cache, {n_network} via network",
          file=sys.stderr)
    return doi_meta


# ── Contract validation & deploy diff ────────────────────────────────────────

def _canonicalize_countries(records: list[dict]) -> int:
    """Normalize `study_design.countries` to canonical names, in place.

    Collapses synonym spellings of one country (see constants.COUNTRY_SYNONYMS)
    and de-duplicates the resulting list per record while preserving order, so
    a record listing both "US" and "United States" yields a single "United
    States". Returns the number of names that were changed (for logging).
    """
    changed = 0
    for r in records:
        sd = r.get("study_design")
        if not isinstance(sd, dict):
            continue
        raw = sd.get("countries")
        if not isinstance(raw, list):
            continue
        out: list[str] = []
        seen: set[str] = set()
        for c in raw:
            canon = canonicalize_country(c) if isinstance(c, str) else c
            if isinstance(c, str) and canon != c:
                changed += 1
            if isinstance(canon, str):
                if canon in seen:
                    continue
                seen.add(canon)
            out.append(canon)
        sd["countries"] = out
    return changed


def _load_verdicts(path: Path) -> dict[str, str]:
    """base DOI -> curated verdict from trial_verdicts.csv (empty dict if absent).

    Keyed on the base DOI (paper_id before any '#arm' suffix) so a multi-arm
    record still finds its paper's verdict.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = (row.get("paper_id") or row.get("doi") or "").strip()
            v = (row.get("verdict") or "").strip()
            if pid and v:
                out[pid.split("#")[0]] = v
    return out


def _years_from_screening(path: Path) -> dict[str, str]:
    """base DOI -> paper_year from the screening CSV.

    A third year source behind the record and doi_metadata.json. Measured on
    nattokinase: the record carries a year for 6 of 30 papers and
    doi_metadata.json for the same 6, while screening has 50 across the corpus —
    so without this the first review we preview renders a nearly empty year chart.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip()
            yr = (row.get("paper_year") or "").strip()
            if doi and yr:
                out.setdefault(doi.split("#")[0], yr)
    return out


def _load_deployed(out_dir: Path, slug: str) -> list[dict] | None:
    """Read the currently-deployed feed (standardized name, then legacy)."""
    for name in ("trial_extractions.jsonl", f"{slug}_trial_extractions.jsonl"):
        p = out_dir / name
        if p.exists():
            try:
                return _read_jsonl(p)
            except Exception as ex:
                print(f"  WARN could not read deployed {p}: {ex}", file=sys.stderr)
                return None
    return None


def _diff_vs_deployed(new_records: list[dict], deployed: list[dict]) -> dict:
    new_ids = {r.get("paper_id") for r in new_records}
    old_ids = {r.get("paper_id") for r in deployed}
    removed = old_ids - new_ids
    added = new_ids - old_ids
    # Canonicalize the deployed feed's country names too before metricizing, so
    # the one-time synonym merge (e.g. "USA"+"United States" -> 1 country) isn't
    # mistaken for information loss by the n_countries guard. A real drop in
    # distinct countries still trips it. Mutates a throwaway view of `deployed`.
    _canonicalize_countries(deployed)
    old_m = _chart_metrics(deployed)
    new_m = _chart_metrics(new_records)
    shrunk = {k: (old_m[k], new_m[k]) for k in old_m
              if isinstance(old_m[k], (int, float)) and new_m[k] < old_m[k]}
    return {
        "removed_ids": sorted(removed),
        "added_ids": sorted(added),
        "old_metrics": old_m,
        "new_metrics": new_m,
        "shrunk_metrics": shrunk,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def _resolve_meta_options(config, args) -> "ma.MetaOptions":
    """Build MetaOptions from the config `meta_analysis:` block, with CLI overrides.

    CLI wins over config wins over the library defaults (DL random-effects, Wald
    CIs — the legacy pooling). ``store_true`` flags can only turn a feature *on*;
    to keep one off, omit both the flag and the config key."""
    cfg = (config or {}).get("meta_analysis", {}) or {}

    def pick(cli_val, key, default):
        # "config" is the sentinel the GUI form passes to mean "defer to config".
        if cli_val is not None and cli_val != "config":
            return cli_val
        return cfg.get(key, default)

    opts = ma.MetaOptions(
        model=pick(args.ma_model, "model", "random"),
        tau2_method=pick(args.ma_tau2, "tau2_method", "DL"),
        ci_method=pick(args.ma_ci, "ci_method", "wald"),
        level=pick(args.ma_level, "level", 0.95),
        min_trials=pick(args.min_trials, "min_trials", 2),
        prediction_interval=bool(args.ma_prediction_interval or cfg.get("prediction_interval", False)),
        publication_bias=bool(args.ma_publication_bias or cfg.get("publication_bias", False)),
        leave_one_out=bool(args.ma_leave_one_out or cfg.get("leave_one_out", False)),
        subgroup_by=pick(args.ma_subgroup_by, "subgroup_by", None),
        regress_on=pick(getattr(args, "ma_regress_on", None), "regress_on", None),
    )
    return opts.normalized()


def main(args):
    # Config mode (default): derive per-project defaults from the topic slug, with
    # any path overridable. Standalone mode: no readable config, drive purely off
    # explicit --pass2 / --slug (used for ad-hoc parity checks).
    config = None
    try:
        config = load_config(args.config)
    except Exception:
        config = None

    if config:
        slug = args.slug or slugify(config.get("topic", {}).get("name", "default"))
        project = PROJECTS_DIR / slug
        pass2_path = (Path(args.pass2) if args.pass2
                      else project / "data" / "trial_extractions_pass2.jsonl")
        obs_path = (Path(args.observational) if args.observational
                    else project / "data" / "observational_extractions.jsonl")
        spray_csv = (Path(args.spray_types) if args.spray_types
                     else project / "data" / "spray_types.csv")
        mech_path = (Path(args.mechanism) if args.mechanism
                     else project / "data" / "mechanism_of_action.json")
        screening_csv = (Path(args.screening) if args.screening
                         else project / "data" / "trial_screening.csv")
    else:
        if not args.pass2:
            sys.exit("ERROR: no config found and no --pass2 given.")
        pass2_path = Path(args.pass2)
        slug = args.slug or slugify(pass2_path.stem)
        obs_path = Path(args.observational) if args.observational else None
        spray_csv = Path(args.spray_types) if args.spray_types else None
        mech_path = Path(args.mechanism) if args.mechanism else None
        screening_csv = Path(args.screening) if args.screening else None

    empty_rct = pass2_path.parent.parent / "eligible_rct"
    has_empty_rct = empty_rct.is_dir() and not any(empty_rct.iterdir())
    if not pass2_path.exists() and not (obs_path and obs_path.exists() and not args.no_observational) and not has_empty_rct:
        sys.exit(f"ERROR: extraction JSONL not found: {pass2_path}")

    # Coarse phase progress so the studio's live bar advances instead of sitting
    # on "starting…". Emitted as "Progress: i/N" lines (parsed by the studio
    # runner's _LOGPROG_RE). The FIRST is emitted up front so the total is known
    # immediately — the same fix applied to 8/9_extract_pass*.py.
    _BUILD_PHASES = (
        "Loading records", "Merging feed", "Backfilling biblio",
        "Routing + contract check", "Meta-analysis", "Writing bundle",
    )
    _phase_n = {"i": 0}

    def _phase(label: str) -> None:
        _phase_n["i"] += 1
        log_progress(_phase_n["i"], len(_BUILD_PHASES), extra=label)

    _phase(_BUILD_PHASES[0])  # up front, so the bar shows a total immediately

    from ber_core.bundles import build_snapshot, source_fingerprint, INPUT_MANIFEST
    config = config or {}
    input_snapshot = source_fingerprint(pass2_path.parent, config)
    pass2_records = _read_jsonl(pass2_path) if pass2_path.exists() else []
    print(f"Loaded {len(pass2_records)} pass-2 records from {pass2_path}",
          file=sys.stderr)

    obs_records: list[dict] = []
    if not args.no_observational and obs_path and obs_path.exists():
        obs_records = _read_jsonl(obs_path)
        print(f"Loaded {len(obs_records)} observational records from {obs_path}",
              file=sys.stderr)
    elif args.no_observational:
        print("Observational merge disabled (--no-observational): pass-2 only.",
              file=sys.stderr)

    # Duplicate-publication map (preprint -> published, etc.), sourced from the
    # project data dir alongside the feed so a study published twice counts once.
    dup_dir = (spray_csv.parent if spray_csv else pass2_path.parent)
    dup_map = _load_duplicate_dois(dup_dir / "duplicate_dois.json")
    if dup_map:
        print(f"duplicate_dois.json: {len(dup_map)} duplicate-publication mappings",
              file=sys.stderr)

    _phase("Merging feed")
    # Applied ONCE, inside _union_records:
    #  * delivery_exclusions.json - trials whose intervention is not
    #    nasal-delivered; surfaced as the 'non-nasal delivery' PRISMA exclusion
    #    rather than in the results dashboard / meta-analysis;
    #  * user_exclusions.json - papers a human excluded in the studio's evidence
    #    table. Same shared loader stage_eligible and pool_meta_analysis use, so
    #    the staged set, the pooled estimates and this feed agree. This is much
    #    narrower than re-applying the pass-1 screening flag (which dropped 27 of
    #    one review's 30 papers): only what a human explicitly excluded.
    excluded = (load_exclusion_dois(pass2_path.parent / "user_exclusions.json")
                | _load_delivery_exclusions(dup_dir / "delivery_exclusions.json"))
    records, merge_stats = _union_records(pass2_records, obs_records, dup_map, excluded)

    # Join the organism verdict from the PASS-1 screen. It is not in the pass-2
    # schema, and adding it there would mean re-extracting every paper for a
    # display column — so it is joined by DOI here instead, the same way the rest
    # of the feed merges on-disk tiers. Papers screened before the organism
    # migration simply have no verdict; they stay None and render "—".
    _organism = _load_organism(pass2_path.parent / "trial_extractions.jsonl")
    if _organism:
        n_tagged = 0
        for r in records:
            verdict = _organism.get(norm_paper_id(r.get("paper_id")))
            if verdict:
                r["organism"] = verdict
                n_tagged += 1
        print(f"organism: joined {n_tagged}/{len(records)} record(s) from pass-1",
              file=sys.stderr)

    print(f"Merged feed: {len(records)} records "
          f"({merge_stats['n_pass2']} pass-2 + "
          f"{merge_stats['added_observational']} observational; "
          f"{merge_stats['dropped_failed']} failed extractions dropped; "
          f"{merge_stats['dropped_duplicates']} duplicate publications collapsed; "
          f"{merge_stats['dropped_excluded']} delivery/manual exclusions)",
          file=sys.stderr)

    spray_types = _load_spray_types(spray_csv) if spray_csv else {}
    if spray_types:
        print(f"spray_types.csv: {len(spray_types)} DOIs mapped", file=sys.stderr)

    # Canonical-ingredient map (doi -> compound) and the abstract-only set, both
    # sourced from the project data dir alongside spray_types.csv. When present,
    # forest plots group by ingredient rather than the broader spray_type.
    data_dir = spray_csv.parent if spray_csv else pass2_path.parent
    ingredient_map = (
        _load_ingredient_map(data_dir / "spray_types.csv",
                             data_dir / "agent_canonical_map.json")
        if data_dir else {})
    abstract_only = (_load_abstract_only(data_dir / "clinical_staging_manifest.json")
                     if data_dir else set())
    # Per-arm intervention canonicalization (raw name -> canonical), produced by
    # canonicalize_interventions.py. Used by the long-covid dashboard to group
    # the many free-text variants of an intervention; no-op when the map is absent.
    intervention_canon = (_load_intervention_canon(data_dir / "intervention_canonical_map.json")
                          if data_dir else {})
    # Per-canonical category overrides (categorize_interventions.py) that move
    # interventions out of the catch-all "other" bucket; keyed by canonical name.
    intervention_cat = (_load_intervention_canon(data_dir / "intervention_category_map.json")
                        if data_dir else {})
    # Indication classification (classify_intervention_indication.py): paper_id ->
    # treatment | prevention | both | exclude. Keeps the feed on the review's
    # indication focus (see route_by_indication).
    indication = (_load_indication_map(data_dir / "intervention_indication_map.json")
                  if data_dir else {})
    if ingredient_map:
        print(f"ingredient map: {len(ingredient_map)} DOIs -> canonical compound",
              file=sys.stderr)
    if intervention_canon:
        print(f"intervention canonical map: {len(intervention_canon)} names",
              file=sys.stderr)
    if intervention_cat:
        print(f"intervention category overrides: {len(intervention_cat)} names",
              file=sys.stderr)
    if abstract_only:
        print(f"abstract-only trials: {len(abstract_only)}", file=sys.stderr)

    fetch_meta = None
    if not args.no_metadata_fetch:
        from ber_core.metadata_fetcher import fetch_metadata_from_doi
        fetch_meta = fetch_metadata_from_doi

    # Local biblio cache (the already-enriched CSVs). Backfilling from disk avoids
    # re-fetching title/authors/journal/etc. for every title-less record.
    local_meta = _load_local_metadata(
        screening_csv,
        (data_dir / "classifications_enriched.csv") if data_dir else None)
    if local_meta:
        print(f"local metadata cache: {len(local_meta)} DOIs", file=sys.stderr)

    _phase("Backfilling biblio")
    doi_meta = _enrich(records, spray_types, fetch_meta, local_meta)

    # Attach canonical ingredient + abstract-only flag for the results dashboard.
    for r in records:
        doi = norm_paper_id(r.get("paper_id"))
        if doi in ingredient_map:
            r["agent_canonical"] = ingredient_map[doi]
        if doi in abstract_only:
            r["source_is_abstract_only"] = True

    # Canonical intervention names are the POOLING identity
    # (scientific.treatment_identity), so they must be on the arms before the
    # meta-analysis below; category overrides move arms out of "other".
    ma.attach_canonical_interventions(records, intervention_canon, intervention_cat)

    _phase("Routing + contract check")
    # Arm-split multi-spray trials (one record per spray arm) so each arm
    # contributes to its own ingredient group. Done after agent_canonical so the
    # per-arm override sticks. Screening/PRISMA still counts the study once.
    records = _attach_related_publications(
        records, data_dir / "related_publications.json")
    records = _apply_arm_splits(records, data_dir / "trial_arm_splits.json")

    # ── Indication routing (classify_intervention_indication.py): keep the feed on
    # the review's focus. Studies of the other indication go to a side feed;
    # off-topic ("exclude") records are dropped; unclassified records are KEPT
    # (never silently drop a real study). Gated on the map existing.
    side_records: list[dict] = []
    side_kind = None
    if indication:
        focus, explicit = indication_focus(config)
        records, side_records, side_kind, n_excl = route_by_indication(records, indication, focus)
        print(f"indication routing (focus={focus}): {len(records)} in feed, "
              f"{len(side_records)} {side_kind or 'side-feed'}, {n_excl} excluded",
              file=sys.stderr)
        if side_records and not explicit:
            print(f"  NOTE: topic.indication_focus is not set, so the historical default "
                  f"'treatment' applied and {len(side_records)} {side_kind} stud(ies) were "
                  f"routed out of the main feed. Set topic.indication_focus to 'all' or "
                  f"'prevention' if this review's question includes prevention.",
                  file=sys.stderr)

    # Canonicalize country names so every feed carries one spelling per country.
    # Prevents the "Trials by country" map from splitting/overwriting one country
    # across synonyms (e.g. "USA"/"US"/"United States of America" -> "United
    # States"). Applies to both the main feed and the indication side feed.
    n_country_fixes = _canonicalize_countries(records) + _canonicalize_countries(side_records)
    if n_country_fixes:
        print(f"country canonicalization: normalized {n_country_fixes} country name(s)",
              file=sys.stderr)

    # Contract validation
    contract = _validate_contract(records)
    if contract["missing_keys"]:
        print(f"  CONTRACT: records missing required keys: {contract['missing_keys']}",
              file=sys.stderr)
    if contract["unknown_rob"]:
        print(f"  CONTRACT: out-of-vocab risk_of_bias values (dashboard won't bucket): "
              f"{contract['unknown_rob']}", file=sys.stderr)
    if contract["unknown_arm_type"]:
        print(f"  CONTRACT: out-of-vocab arm.type values: {contract['unknown_arm_type']}",
              file=sys.stderr)

    # Meta-analysis grouping unit. Prefer the canonical ingredient (compound)
    # when an ingredient map is available; otherwise fall back to spray_type
    # (record value, then first arm's intervention_category).
    def get_spray(r):
        if r.get("spray_type"):
            return r["spray_type"]
        arms = (r.get("study_design") or {}).get("arms") or []
        for a in arms:
            if a.get("intervention_category"):
                return a["intervention_category"]
        return "other"

    _phase("Meta-analysis")
    # Meta-analysis (forest-plot pooling) is opt-in (off by default). When
    # disabled we skip the computation entirely and never touch
    # meta_analysis.json, so routine re-runs don't disturb a previously-deployed
    # file and new topics simply have no meta-analysis surface on the website.
    if args.meta_analysis:
        ma_opts = _resolve_meta_options(config, args)
        if ingredient_map:
            meta = ma.build_meta_analysis(
                records, lambda r: r.get("agent_canonical"),
                ma_opts, group_field="ingredient", policy=(config or {}).get("meta_analysis"))
        else:
            meta = ma.build_meta_analysis(records, get_spray, ma_opts,
                                          group_field="spray_type", policy=(config or {}).get("meta_analysis"))
        pooled_groups = [g for g in meta["groups"] if g.get("pooled")]
        print(f"Meta-analysis: {len(meta['groups'])} groups, "
              f"{len(pooled_groups)} pooled (>= {ma_opts.min_trials} trials); "
              f"model={ma_opts.model} tau2={ma_opts.tau2_method} ci={ma_opts.ci_method}",
              file=sys.stderr)
    else:
        meta = None
        pooled_groups = []
        print("Meta-analysis: skipped (default; pass --meta-analysis to generate)",
              file=sys.stderr)

    # Where the bundle is written. `--out-dir` targets an arbitrary directory —
    # normally the REVIEW'S OWN WORKSPACE, so a pipeline run builds something
    # previewable without touching the public site. `--website-dir` remains the
    # publish target and is just sugar for the conventional path under it.
    #
    # This split exists because building and PUBLISHING are different acts: the
    # export step used to write straight into the shared public repo as part of
    # a normal run, so the studio's default "Run pipeline" button published.
    out_dir = (Path(args.out_dir) if args.out_dir
               else Path(args.website_dir) / "data" / "birds_eye_reviews" / slug if args.website_dir
               else pass2_path.parent.parent / "dashboard")

    # Diff against the currently-deployed feed (the loss guardrail).
    deployed = _load_deployed(out_dir, slug)
    if deployed is not None:
        diff = _diff_vs_deployed(records, deployed)
        print(f"\n=== Diff vs deployed ({len(deployed)} records) ===", file=sys.stderr)
        print(f"  added:   {len(diff['added_ids'])} records", file=sys.stderr)
        print(f"  removed: {len(diff['removed_ids'])} records", file=sys.stderr)
        for k in sorted(diff["new_metrics"]):
            old, new = diff["old_metrics"][k], diff["new_metrics"][k]
            flag = "  <-- SHRANK" if k in diff["shrunk_metrics"] else ""
            if old != new:
                print(f"    {k}: {old} -> {new}{flag}", file=sys.stderr)
        if diff["removed_ids"] or diff["shrunk_metrics"]:
            print("\n  !! INFORMATION LOSS DETECTED !!", file=sys.stderr)
            if diff["removed_ids"]:
                print(f"  {len(diff['removed_ids'])} previously-published papers would "
                      f"disappear, e.g. {diff['removed_ids'][:5]}", file=sys.stderr)
            if diff["shrunk_metrics"]:
                print(f"  shrinking metrics: {diff['shrunk_metrics']}", file=sys.stderr)
            if not args.allow_shrink:
                sys.exit("ABORTED: refusing to publish a feed that loses information. "
                         "Re-run with --allow-shrink only if this is intentional.")
            print("  --allow-shrink set: proceeding despite loss.", file=sys.stderr)
    else:
        print("No deployed feed found to diff against (first export).", file=sys.stderr)

    if args.dry_run:
        unit_key = "ingredient" if ingredient_map else "spray_type"
        for g in pooled_groups[:20]:
            p = g["pooled"]
            print(f"  [{g.get(unit_key)} / {g['outcome_domain']} / "
                  f"{g['effect_measure']}] n={g['n_trials']} "
                  f"pooled={p['effect']} ({p['ci_low']}-{p['ci_high']}) I2={p['i2']}%",
                  file=sys.stderr)
        print("Dry run; no files written.", file=sys.stderr)
        return

    _phase("Writing bundle")
    destination = out_dir
    with build_snapshot(destination) as out_dir:

        with open(out_dir / "trial_extractions.jsonl", "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        # Studies routed out of the main feed by indication (prevention studies of
        # a treatment review, or vice versa): <kind>_trials.jsonl. Refreshed each
        # export when the indication map exists.
        if side_kind:
            side_text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in side_records)
            names = [f"{side_kind}_trials.jsonl"]
            # The public long-COVID prevention page reads the historical name.
            if slug == "long_covid" and side_kind == "prevention":
                names.append(LEGACY_LONG_COVID_PREVENTION)
            for name in names:
                (out_dir / name).write_text(side_text, encoding="utf-8")
                print(f"  {name}  ({len(side_records)} records)", file=sys.stderr)
        with open(out_dir / "doi_metadata.json", "w", encoding="utf-8") as f:
            json.dump(doi_meta, f, ensure_ascii=False, indent=2)
        # Publication enrichment is optional and offline at export time.
        if data_dir:
            for name in ("publication_metadata.json", "publication_audit.json"):
                source = data_dir / name
                if source.exists():
                    (out_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        if args.meta_analysis:
            with open(out_dir / "meta_analysis.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        if screening_csv and screening_csv.exists():
            (out_dir / "trial_screening.csv").write_text(
                screening_csv.read_text(encoding="utf-8"), encoding="utf-8")
        # Screening-flow funnel (PRISMA) — exported with EVERY bundle so the
        # dashboard's "Screening flow" panel always has its data (it used to be
        # permanently skipped with "needs prisma.json"). Derived from the same
        # ber_core.prisma helpers the studio's live funnel endpoint uses. The bundle
        # scopes its terminal count to the routed feed; other extracted reports
        # leave through an explicit branch. Best-effort: a failed funnel
        # derivation must not sink the whole export.
        if config:
            try:
                from ber_core.prisma import derive_counts, to_visualize_json
                from ber_core.projects import Project as _PrismaProject
                _counts = derive_counts(_PrismaProject(
                    slug=slug, config_path=Path(args.config), config=config,
                    project_dir=project), final_records=records)
                _diagram = to_visualize_json(_counts)
                _diagram["counts"] = _counts
                (out_dir / "prisma.json").write_text(
                    json.dumps(_diagram, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                print(f"  prisma.json skipped: {e}", file=sys.stderr)
        if spray_csv and spray_csv.exists():
            (out_dir / "spray_types.csv").write_text(
                spray_csv.read_text(encoding="utf-8"), encoding="utf-8")
        if mech_path and mech_path.exists():
            (out_dir / "mechanism_of_action.json").write_text(
                mech_path.read_text(encoding="utf-8"), encoding="utf-8")
        # Trial verdicts (favors_treatment/control/no_difference/mixed/inconclusive) —
        # the dashboard's result-direction segments + Result column. Produced by
        # classify_trial_verdicts.py; copied through when present so every topic gets it.
        data_dir2 = spray_csv.parent if spray_csv else pass2_path.parent
        verdicts_csv = data_dir2 / "trial_verdicts.csv"
        if verdicts_csv.exists():
            (out_dir / "trial_verdicts.csv").write_text(
                verdicts_csv.read_text(encoding="utf-8"), encoding="utf-8")

        # Stamp the update time into the bundle so the dashboard can show "last
        # updated" reliably (file mtimes are reset by git checkout / Vercel build).
        now = datetime.datetime.now(datetime.timezone.utc)
        last_updated = {
            "last_updated": now.strftime("%Y-%m-%d"),
            "last_updated_display": f"{now.strftime('%B')} {now.day}, {now.year}",
            "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_records": len(records),
        }
        with open(out_dir / "last_updated.json", "w", encoding="utf-8") as f:
            json.dump(last_updated, f, ensure_ascii=False, indent=2)

        # ── The canonical projection + precomputed aggregates ────────────────────
        # Everything the dashboard displays is derived ONCE, here, so the studio
        # preview and the generated website page read the same numbers instead of
        # each computing their own. See dashboard_summary.__doc__.
        csv_verdicts = _load_verdicts(out_dir / "trial_verdicts.csv")
        # No screened_out set is passed: `records` is already the eligibility
        # decision (see _union_records + the indication routing above).
        # Re-applying the screening flag here drops 27 of nattokinase's 30 papers.
        outcome_types = ((config.get("dashboard") or {}).get("outcome_types")
                         or ds.derive_outcome_types(records))
        # Key EVERYTHING through norm_paper_id. Three sources feed this map with
        # three different conventions — _load_local_metadata lowercases, while
        # _enrich and _years_from_screening keep raw case — so without normalizing
        # one paper could occupy two entries and the year backfill (which exists to
        # fix a near-empty year chart) would miss any DOI whose screening-CSV case
        # differed from the record's. build_rows looks up by norm_paper_id.
        row_meta: dict[str, dict] = {}
        for k, v in doi_meta.items():
            row_meta[norm_paper_id(k)] = dict(v)
        for doi, yr in _years_from_screening(out_dir / "trial_screening.csv").items():
            row_meta.setdefault(norm_paper_id(doi), {}).setdefault("year", yr)
        rows, inclusion = ds.build_rows(
            records, csv_verdicts=csv_verdicts,
            outcome_types=outcome_types, metadata=row_meta)

        rows_text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        (out_dir / "dashboard_rows.jsonl").write_text(rows_text, encoding="utf-8")

        summary = ds.build_summary(
            records, slug=slug, rows=rows, inclusion=inclusion,
            outcome_types=outcome_types,
            feasibility=ma_lib.assess_poolability(records, policy=(config or {}).get("meta_analysis"), min_trials=((config or {}).get("meta_analysis") or {}).get("min_trials", 2)),
            rows_bytes=len(rows_text.encode("utf-8")),
            feed_bytes=(out_dir / "trial_extractions.jsonl").stat().st_size,
            generated_at=last_updated["generated_at_utc"])
        with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print(f"\n=== Done === wrote to {out_dir}", file=sys.stderr)
        print(f"  last_updated.json        ({last_updated['last_updated_display']})",
              file=sys.stderr)
        print(f"  trial_extractions.jsonl  ({len(records)} records)", file=sys.stderr)
        print(f"  doi_metadata.json        ({len(doi_meta)} DOIs)", file=sys.stderr)
        if args.meta_analysis:
            print(f"  meta_analysis.json       ({len(pooled_groups)} pooled groups)", file=sys.stderr)
        else:
            print("  meta_analysis.json       (disabled; omitted from this snapshot)", file=sys.stderr)
        if screening_csv and screening_csv.exists():
            print("  trial_screening.csv      (copied through)", file=sys.stderr)
        if args.out_dir:
            # A local build. Saying "commit & push to deploy" here would be wrong
            # AND dangerous: nothing was written to the website repo.
            print("\nNext: preview this bundle in the studio. Publishing to the "
                  "public site is a separate, explicit step.", file=sys.stderr)
        else:
            print("\nNext: review the diff in the website repo, then commit & push "
                  "to deploy (Vercel auto-deploys).", file=sys.stderr)

        if source_fingerprint(pass2_path.parent, config) != input_snapshot:
            raise RuntimeError("Review inputs changed during the build; retry with stable inputs")
        (out_dir / INPUT_MANIFEST).write_text(json.dumps(input_snapshot, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True,
                   help="Topic config YAML (e.g. configs/config_my_topic.yaml)")
    p.add_argument("--slug", default=None, help="Override project/output slug")
    p.add_argument("--pass2", default=None,
                   help="RCT/pass-2 extraction JSONL "
                        "(default: projects/{slug}/data/trial_extractions_pass2.jsonl)")
    p.add_argument("--observational", default=None,
                   help="Observational extraction JSONL "
                        "(default: projects/{slug}/data/observational_extractions.jsonl)")
    p.add_argument("--no-observational", action="store_true",
                   help="Do not merge observational records (reproduce pass-2 feed exactly)")
    p.add_argument("--screening", default=None, help="Override trial_screening.csv path")
    p.add_argument("--spray-types", default=None, help="Override spray_types.csv path")
    p.add_argument("--mechanism", default=None, help="Override mechanism_of_action.json path")
    p.add_argument("--out-dir", default=None,
                   help="Write the bundle here instead of under --website-dir. "
                        "Used to build into a review's workspace for local "
                        "preview; publishing is a separate, explicit action.")
    p.add_argument("--website-dir", default=None,
                   help="Explicit legacy destination; default builds PROJECT/dashboard locally")
    p.add_argument("--meta-analysis", action="store_true",
                   help="Generate forest-plot pooling (meta_analysis.json); off by "
                        "default. When omitted, the new snapshot contains no meta-analysis.")
    p.add_argument("--min-trials", type=int, default=None,
                   help="Min trials in a (group,outcome,measure) group to pool "
                        "(default: config meta_analysis.min_trials, else 2)")
    # Meta-analysis methodology (override the config `meta_analysis:` block).
    # "config" means defer to the config block (the sentinel the GUI form uses).
    p.add_argument("--ma-model", choices=("config", "random", "fixed"), default=None,
                   help="Pooling model (default: config, else random)")
    p.add_argument("--ma-tau2", choices=("config", "DL", "PM", "REML"), default=None,
                   help="Between-study variance estimator (default: config, else DL)")
    p.add_argument("--ma-ci", choices=("config", "wald", "hksj", "modified_hksj"), default=None,
                   help="CI method: wald (z) or Knapp-Hartung (default: config, else wald)")
    p.add_argument("--ma-level", type=float, default=None,
                   help="Confidence level, e.g. 0.95 (default: config, else 0.95)")
    p.add_argument("--ma-prediction-interval", action="store_true",
                   help="Add prediction intervals to each pooled group")
    p.add_argument("--ma-publication-bias", action="store_true",
                   help="Add Egger's test, funnel coords, and trim-and-fill per group")
    p.add_argument("--ma-leave-one-out", action="store_true",
                   help="Add leave-one-out sensitivity per pooled group")
    p.add_argument("--ma-subgroup-by", default=None,
                   help="Dotted record field to run subgroup analysis on, e.g. "
                        "risk_of_bias.overall_judgment")
    p.add_argument("--ma-regress-on", default=None,
                   help="Dotted record field holding a CONTINUOUS trial-level "
                        "moderator to meta-regress the effect on, e.g. "
                        "derived.mean_serum_li_mmol_L. Also emits the raw "
                        "scatter points for plotting.")
    p.add_argument("--no-metadata-fetch", action="store_true",
                   help="Skip network metadata backfill (use fields already in JSONL)")
    p.add_argument("--allow-shrink", action="store_true",
                   help="Permit publishing a feed that drops papers/metrics (override guardrail)")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute, validate, and diff; write nothing")
    main(p.parse_args())
