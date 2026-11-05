#!/usr/bin/env python3
"""Download full text for classified papers, filtered by article type.

Reads projects/{slug}/data/classifications.csv (produced by classify.py),
filters to selected article types (default: Original Research and Clinical
Trial), and uses the fetchpdf package to fetch each paper through its
multi-source fallback chain (Unpaywall, PMC, Crossref, OpenAlex, CORE, etc.).

Three artifact classes, written into the paper's own folder in the store
(projects/{slug}/papers/<Type>/<stem>/):

    {stem}.pdf                        always
    {stem}.xml / .fulltext.html       structured full text -- ON by default
    {stem}_supplementary_info*        supplementary material -- OFF by default

Structured text is the point of the whole chain: publisher XML carries tables as
real markup, so extraction reads it in preference to the lossy PDF->markdown
conversion — and it now lands beside the PDF with no relocation step.

fetchpdf can only nest one level, so a batch downloads into papers/_incoming/
and is filed into type buckets afterwards (see paper_store.refile).

Project-aware: defaults paths under projects/{slug}/. Resumable, per artifact
CLASS: a paper is skipped only when it already has everything THIS run asked
for, so enabling supplementary material later backfills over papers whose PDFs
were downloaded long ago -- without re-downloading those PDFs.

Usage:
    python steps/download_pdfs.py --config configs/config_x.yaml
    python steps/download_pdfs.py --config configs/config_x.yaml --types "Original Research,Clinical Trial,Meta-Analysis"
    python steps/download_pdfs.py --config configs/config_x.yaml --supplementary   # backfills SI
    python steps/download_pdfs.py --config configs/config_x.yaml --no-structured   # PDFs only
    python steps/download_pdfs.py --config configs/config_x.yaml --no-extra-sources
    python steps/download_pdfs.py --config configs/config_x.yaml --limit 5 --workers 2

Extra PDF sources
-----------------
fetchpdf's built-in source chain can be extended by optional, separately
installed plugins. A plugin registers a zero-argument callable under the
``ber.pdf_sources`` entry-point group, e.g. in its pyproject.toml::

    [project.entry-points."ber.pdf_sources"]
    my_source = "my_package.ber_plugin:install"

``install()`` is called once per process, BEFORE ``fetchpdf`` is imported, and
only when extra sources are enabled (``--no-extra-sources`` skips them all). It
installs or enables its sources (e.g. by registering them with fetchpdf) and may
return a short label (a version string) for the run banner. A plugin that
raises is reported and skipped; the run continues with the built-in sources.
"""

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.environment import load_local_env as load_dotenv

# Load this repo's .env.local BEFORE importing fetchpdf. That package
# reads EMAIL at import time and only checks its *own* directory's .env.local,
# so our EMAIL must already be in the environment — otherwise it warns and makes
# no-email API calls (lower Crossref rate limits, Unpaywall disabled). Plugins
# load their own settings after this, and load_dotenv never overwrites, so
# this repo's values win.
BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(BASE_DIR / ".env.local")

from ber_core import migrate_to_paper_store  # noqa: E402
from ber_core import paper_store  # noqa: E402
from ber_core.utils import (  # noqa: E402
    load_config, slugify, norm_doi, decode_artifact_stem, strip_artifact_suffix,
    is_fetchable_article,
    RED, YELLOW, RESET,
)
from ber_core.constants import PDF_ELIGIBLE_TYPES  # noqa: E402

#: Entry-point group optional extra PDF sources register under (see module doc).
PDF_SOURCE_GROUP = "ber.pdf_sources"

# fetchpdf is imported LAZILY, by _import_fetchpdf(), so extra-source plugins
# can install themselves first: fetchpdf binds some settings at import time.
batch_fetch_pdfs = None
doi_to_safe_filename = None


def load_extra_sources(enabled: bool) -> list[str]:
    """Run every ``ber.pdf_sources`` plugin's installer; return banner labels.

    Must run before :func:`_import_fetchpdf`. With *enabled* False nothing is
    loaded at all.
    """
    if not enabled:
        return []
    from importlib.metadata import entry_points
    labels: list[str] = []
    for ep in entry_points(group=PDF_SOURCE_GROUP):
        try:
            label = ep.load()()
        except Exception as exc:  # noqa: BLE001 — a plugin must not sink the run
            print(f"{YELLOW}WARN: extra PDF source {ep.name!r} failed to load "
                  f"({type(exc).__name__}: {exc}); continuing without it{RESET}",
                  file=sys.stderr)
            continue
        labels.append(f"{ep.name} {label}" if isinstance(label, str) and label
                      else ep.name)
    return labels


def _import_fetchpdf() -> None:
    """Bind fetchpdf's entry points (after any plugins installed themselves)."""
    global batch_fetch_pdfs, doi_to_safe_filename
    if batch_fetch_pdfs is None:
        from fetchpdf import batch_fetch_pdfs as _batch
        batch_fetch_pdfs = _batch
    if doi_to_safe_filename is None:
        # fetchpdf's OWN filename encoder. Staging folders must be named with
        # it, not with doi_to_filename: it names the artifact files inside
        # them, and the two encoders diverge on any DOI containing ":"
        # (10.1000/x:y -> 10.1000--x~y).
        from fetchpdf.fetchpdf import doi_to_safe_filename as _enc
        doi_to_safe_filename = _enc

PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

# --- Artifact classes -------------------------------------------------------
# What this script can be asked to obtain for a paper. A paper is "satisfied"
# only when it already has every class the CURRENT run requested — which is what
# lets a later run backfill: turning supplementary material on re-admits every
# paper whose PDF was downloaded months ago, without re-downloading that PDF.
PDF = "pdf"
STRUCTURED = "structured"   # publisher XML (preferred) or full-text HTML
SI = "si"                   # supplementary/supporting information


def _classify_artifact(name: str) -> str | None:
    """The artifact class a filename represents, or None if it is not one."""
    if name.endswith("_supplementary_info.json"):
        return SI
    if name.endswith(".fulltext.html") or name.endswith(".xml"):
        return STRUCTURED
    if name.endswith(".pdf"):
        return PDF
    return None


def seed_staging(staging: Path, dois: list[str], project_dir: Path,
                 papers_dir: Path) -> int:
    """Hardlink each paper's EXISTING artifacts into its staging folder.

    fetchpdf pre-fills an already-satisfied goal from whatever it finds in its
    own output dir — that is what makes a backfill run (turning supplementary
    material on, say) fetch only the missing class instead of re-downloading
    every PDF. Handing it an empty staging dir would silently undo that, so the
    papers already in the store are represented there before the batch starts.

    Hardlinks, so this costs no disk and no copy time; ``refile`` drops the
    staging entry afterwards (the filed copy wins), which just unlinks one of
    the two directory entries.

    Returns the number of papers seeded.
    """
    _import_fetchpdf()
    index = paper_store.build_index(project_dir, root=papers_dir)
    seeded = 0
    for doi in dois:
        existing = index.get(norm_doi(doi))
        if existing is None:
            continue
        dest = staging / doi_to_safe_filename(doi)
        dest.mkdir(parents=True, exist_ok=True)
        linked = False
        for src in existing.iterdir():
            if not src.is_file() or _classify_artifact(src.name) is None:
                continue
            target = dest / src.name
            if target.exists():
                continue
            try:
                os.link(src, target)
                linked = True
            except OSError:
                # Cross-device or a filesystem without hardlinks: fall back to
                # leaving the goal unfilled rather than copying a whole corpus.
                pass
        seeded += 1 if linked else 0
    return seeded


def artifact_index(papers_dir: Path, project_dir: Path) -> dict[str, set[str]]:
    """Normalized DOI -> the artifact classes already on disk for it.

    Scans every paper folder in the store, plus the legacy locations a project
    that has not been migrated still uses (flat ``pdfs/``, ``converted/<stem>/``
    and the ``eligible_*`` buckets) — otherwise a corpus would look unfetched
    and be re-downloaded wholesale.

    Keys go through :func:`decode_artifact_stem`, never through a reconstructed
    stem: fetchpdf names these files with its own encoder and we name the
    directories with ours, and the two diverge on any DOI containing ":" or a
    literal "--".
    """
    index: dict[str, set[str]] = {}

    def mark(stem: str, cls: str) -> None:
        index.setdefault(decode_artifact_stem(stem), set()).add(cls)

    # --- The store: papers/<Type>/<stem>/ (and papers/_incoming/<stem>/) ---
    if papers_dir.is_dir():
        for bucket in papers_dir.iterdir():
            if not bucket.is_dir():
                continue
            for paper in bucket.iterdir():
                if not paper.is_dir() or not paper_store.is_paper_stem(paper.name):
                    continue
                for path in paper.rglob("*"):
                    if not path.is_file():
                        continue
                    cls = _classify_artifact(path.name)
                    if cls:
                        mark(strip_artifact_suffix(path.name), cls)

    # --- Legacy layouts, so an un-migrated project is not re-fetched ---
    legacy_flat = project_dir / "pdfs"
    if legacy_flat.is_dir() and legacy_flat != papers_dir:
        for path in legacy_flat.glob("*"):
            if not path.is_file():
                continue
            cls = _classify_artifact(path.name)
            if cls:
                mark(strip_artifact_suffix(path.name), cls)

    for base in (project_dir / "converted", *project_dir.glob("eligible_*")):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            cls = _classify_artifact(path.name)
            if cls:
                mark(strip_artifact_suffix(path.name), cls)

    return index


def main(args):
    # --- Resolve project-aware paths -------------------------------------
    config = load_config(args.config)
    topic_name = config.get("topic", {}).get("name", "default")
    slug = slugify(topic_name)
    project_dir = PROJECTS_DIR / slug

    classifications_csv = (Path(args.input) if args.input
                           else project_dir / "data" / "classifications.csv")
    # Precedence: --output-dir > config dirs.base_pdf_dir > project default.
    # The config key was previously honored by the old PDF-reorganize script, the
    # dashboard export and the studio's evidence table, but NOT here — so a
    # configured base_pdf_dir downloaded to one place and was read from another.
    papers_dir = paper_store.papers_root(project_dir, args.output_dir, config)
    papers_dir.mkdir(parents=True, exist_ok=True)
    if not args.output_dir:
        paper_store.remember_root(project_dir, config)

    print(f"Project: {topic_name!r} -> {project_dir}")
    print(f"Input:   {classifications_csv}")
    print(f"Output:  {papers_dir}")

    # Bring a legacy pdfs/ + converted/ project across before the already-have
    # scan runs, so previously-fetched papers are recognised rather than
    # re-downloaded. Idempotent and hardlink-preserving; nothing is deleted.
    if not args.no_auto_migrate and migrate_to_paper_store.needs_migration(project_dir):
        plan = migrate_to_paper_store.migrate_in_place(project_dir, config=config, root=papers_dir)
        print(f"Migrated {len(plan.papers)} paper(s) into the store")

    if not classifications_csv.exists():
        print(f"\nERROR: classifications CSV not found at {classifications_csv}")
        print("       Run classify.py first.")
        sys.exit(1)

    # --- Filter rows ------------------------------------------------------
    # --types wins; otherwise the review's config `pdf_types`; otherwise the
    # shared default. Reading from config lets a review target its own subset
    # without the caller having to remember the flag.
    if args.types:
        keep_types = {t.strip() for t in args.types.split(",") if t.strip()}
    else:
        keep_types = {str(t).strip() for t in
                      (config.get("pdf_types") or PDF_ELIGIBLE_TYPES) if str(t).strip()}
    skip_relevance = {"no"}
    if args.relevant_only:
        skip_relevance = {"no", "unknown"}

    # --- What to fetch besides the PDF -----------------------------------
    # Same precedence as `pdf_types`: an explicit flag wins, else the review's
    # config, else the constant. Structured text (publisher XML, else full-text
    # HTML) is ON by default — it is free, keyless for the sources that matter,
    # and it is what the extraction agent reads. Supplementary material is OFF
    # by default: it is slow, can be very large, and nothing reads it yet.
    ft_cfg = config.get("fulltext") or {}
    want_structured = (args.structured if args.structured is not None
                       else bool(ft_cfg.get("structured", True)))
    want_supplementary = (args.supplementary if args.supplementary is not None
                          else bool(ft_cfg.get("supplementary", False)))
    want_refresh_si = (args.refresh_supplementary
                       or bool(ft_cfg.get("refresh_supplementary", False)))
    want_artifacts = (args.data_artifacts
                      or bool(ft_cfg.get("data_artifacts", False)))
    # fetchpdf's CLI implies --pull-supplementary from these two; its Python API
    # does NOT, and silently no-ops instead. Apply the implication ourselves.
    if want_refresh_si or want_artifacts:
        want_supplementary = True

    max_si_mb = (args.max_si_mb if args.max_si_mb is not None
                 else ft_cfg.get("max_supplementary_mb"))

    if want_structured:
        import importlib.util
        if importlib.util.find_spec("lxml") is None:
            # Not fatal: the JATS tiers that carry most of the value (Europe PMC,
            # PMC efetch, bioRxiv/medRxiv) are stdlib-parsed. Only the HTML
            # rungs demote, and they demote silently, so say so here.
            print(f"{YELLOW}WARN: lxml is not installed -- fetchpdf's HTML tiers "
                  f"(unpaywall_html, publisher_html, arxiv_html) will demote. "
                  f"`pip install 'fetchpdf[html]'` to enable them.{RESET}",
                  file=sys.stderr)

    # Optional restriction to a specific DOI set (incremental updates). Normalize
    # to canonical "/" form so the stored "--" convention also matches.
    only_dois = None
    if args.only_dois:
        op = Path(args.only_dois)
        if op.exists():
            only_dois = set()
            with open(op, newline="", encoding="utf-8", errors="replace") as f:
                for r in csv.reader(f):
                    if not r:
                        continue
                    v = r[0].strip()
                    if v.lower() in ("doi", ""):
                        continue
                    only_dois.add(norm_doi(v))
            print(f"--only-dois: restricting to {len(only_dois)} DOIs from {op}")
        else:
            print(f"ERROR: --only-dois file not found ({op}); refusing an unrestricted download",
                  file=sys.stderr)
            sys.exit(2)
        if not only_dois:
            print("No DOIs selected for download.")
            return

    rows = []
    skipped_scope = 0
    skipped_nonarticle = 0
    with open(classifications_csv, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("article_type") not in keep_types:
                continue
            if row.get("is_relevant", "").strip().lower() in skip_relevance:
                continue
            if only_dois is not None and \
                    norm_doi(row.get("doi") or "") not in only_dois:
                skipped_scope += 1
                continue
            # Dataset deposits and supplementary components have no article to
            # fetch. They reach the corpus legitimately (a Zenodo record really
            # does mention the topic) but every one is a guaranteed failure that
            # costs a download slot and lands title-less in the evidence table.
            # Checked AFTER --only-dois so the reported count describes the
            # scope actually requested, not the whole corpus.
            if not is_fetchable_article(row.get("doi") or ""):
                skipped_nonarticle += 1
                continue
            rows.append(row)
    if skipped_nonarticle:
        print(f"Skipped {skipped_nonarticle} non-article DOI(s) "
              f"(dataset deposits / supplementary components)")
    if skipped_scope:
        print(f"  {skipped_scope} rows out of --only-dois scope -- skipped")

    print(f"\nMatching rows: {len(rows)} (types: {sorted(keep_types)}, "
          f"skip is_relevant in {sorted(skip_relevance)})")

    # Zero matches is nearly always a classification/config mismatch, not an
    # empty corpus. Say so here — the caller otherwise only sees the pipeline's
    # generic "expected non-empty directory .../pdfs" verification failure.
    if not rows:
        with open(classifications_csv, "r", newline="", encoding="utf-8") as f:
            present = Counter((r.get("article_type") or "").strip()
                              for r in csv.DictReader(f))
        print("\nERROR: no classified paper matches the PDF download filter.")
        print(f"       Wanted any of: {sorted(keep_types)}")
        print("       Found in classifications.csv:")
        for t, n in present.most_common(10):
            print(f"         {n:6d}  {t or '(blank)'}")
        if not present:
            print("         (file is empty)")
        print("\n       This usually means the `article_types` config key is "
              "missing or\n       disagrees with the download filter, so the "
              "classifier emitted labels\n       nothing downstream accepts. "
              "Fix the config and re-run classification.")
        sys.exit(1)

    # --- Pre-filter against what is already on disk ----------------------
    # Skip a paper only when it has EVERY artifact class this run asked for.
    # Keying on the PDF alone (as this did before structured text and
    # supplements existed) meant a downloaded paper was never handed to
    # fetchpdf again — so enabling either of those could never backfill an
    # existing corpus, and the toggle would appear to do nothing.
    wanted = {PDF}
    if want_structured:
        wanted.add(STRUCTURED)
    if want_supplementary:
        wanted.add(SI)
    have = artifact_index(papers_dir, project_dir)

    todo_canonical = []
    skipped_have = 0
    skipped_no_doi = 0
    backfill = Counter()
    for row in rows:
        stored_doi = (row.get("doi") or "").strip()
        if not stored_doi:
            skipped_no_doi += 1
            continue
        got = have.get(norm_doi(stored_doi), set())
        if wanted <= got:
            skipped_have += 1
            continue
        for cls in sorted(wanted - got):
            backfill[cls] += 1
        # A paper missing ONLY a non-PDF class is still sent to fetchpdf, and
        # that is cheap: --get-xml-or-html pre-fills the already-satisfied `pdf`
        # goal from disk so nothing re-downloads, and the supplementary pass runs
        # on fetchpdf's own skip branch too.
        #
        # bare "--" -> "/" (NOT filename_to_doi: must not strip a DOI that happens
        # to end in .md/.pdf; NOT norm_doi: the fetcher needs original DOI casing).
        todo_canonical.append(stored_doi.replace("--", "/"))

    if skipped_no_doi:
        print(f"  {skipped_no_doi} rows have no DOI -- skipped")
    if skipped_have:
        print(f"  {skipped_have} already have everything requested -- skipped")
    if backfill:
        print("  to fetch: " + ", ".join(
            f"{n} need {cls}" for cls, n in sorted(backfill.items())))

    if args.limit > 0:
        todo_canonical = todo_canonical[:args.limit]

    if not todo_canonical:
        print("\nNothing to download.")
        return

    # --- Batch download via fetch_pdf_from_doi ---------------------------
    # Optional extra sources install themselves BEFORE fetchpdf is imported.
    # Say which source set is live: a missing plugin otherwise shows up only as
    # a lower success rate.
    extra = load_extra_sources(enabled=not args.no_extra_sources)
    _import_fetchpdf()
    extra_state = (f"extra sources: on ({len(extra)}: {', '.join(extra)})" if extra
                   else "extra sources: off")
    print(f"\nFetching {len(todo_canonical)} papers "
          f"(workers={args.workers}, delay={args.delay}, "
          f"structured={want_structured}, "
          f"supplementary={want_supplementary}, {extra_state})\n")

    # fetchpdf nests exactly one level, so it cannot write <Type>/<stem>/
    # itself: download into staging, then file each folder into its type bucket.
    # The SHARED store lock keeps a concurrent refile (end of a screen) from
    # moving folders out from under the batch.
    with paper_store.store_lock(papers_dir, exclusive=False):
        staging = paper_store.incoming_dir(project_dir, root=papers_dir)
        n_seeded = seed_staging(staging, todo_canonical, project_dir, papers_dir)
        if n_seeded:
            print(f"  {n_seeded} paper(s) already have artifacts — linked into "
                  f"staging so only the missing class is fetched\n")
        _fetch(args, todo_canonical, staging, want_structured, want_supplementary,
               want_refresh_si, want_artifacts, max_si_mb)

    _file_and_report(args, project_dir, papers_dir, staging, rows, todo_canonical,
                     want_structured, want_supplementary, config)


def _fetch(args, todo_canonical, staging, want_structured, want_supplementary,
           want_refresh_si, want_artifacts, max_si_mb) -> None:
    batch_fetch_pdfs(
        dois=todo_canonical,
        output_dir=str(staging),
        email=os.getenv("EMAIL"),
        verbose=args.verbose,
        workers=args.workers,
        delay=args.delay,
        create_missing_report=True,
        # --- structured full text, kept ALONGSIDE the PDF ---
        # get_xml_or_html fills two goals per record and pre-fills whichever is
        # already on disk, so this doubles as the backfill path for a corpus
        # whose PDFs were downloaded before structured text existed.
        get_xml_or_html=want_structured,
        want_provenance=want_structured,
        # Deliberately NOT converting to Markdown. The extraction agent reads the
        # XML directly (see utils.parse_xml_body): fetchpdf's JATS->Markdown
        # converter refuses CALS-model tables (<entry namest/nameend>) rather
        # than flatten them, and raw markup keeps them. Same call fetchpdf's own
        # tools/fetch_into_subfolders.py makes for this exact use case.
        to_markdown=False,
        # One folder per record, which refile() then moves into its type bucket.
        # Every artifact class lands inside it, so nothing needs relocating
        # afterwards the way flat siblings did.
        make_subfolder=True,
        target_task="extraction",
        # --- supplementary / supporting information ---
        pull_supplementary=want_supplementary,
        refresh_supplementary=want_refresh_si,
        download_data_artifacts=want_artifacts,
        max_supplementary_bytes=(int(max_si_mb * 1_000_000) if max_si_mb else None),
    )


def _file_and_report(args, project_dir, papers_dir, staging, rows, todo_canonical,
                     want_structured, want_supplementary, config) -> None:
    # --- File the downloads into their type buckets ----------------------
    # Types come from the rows we already read, so a paper is filed by the same
    # classification that selected it for download in the first place.
    types = {(r.get("doi") or "").strip(): (r.get("article_type") or "").strip()
             for r in rows if (r.get("doi") or "").strip()}
    filed = paper_store.refile(project_dir, types, execute=True, root=papers_dir,
                               valid_types=config.get("article_types") or None)
    if filed:
        print(f"\nFiled {len(filed)} paper(s) into their type folders")

    # --- Report ----------------------------------------------------------
    # Re-derive from disk rather than from the result tuples: under
    # get_xml_or_html a record counts as a success when it yielded XML but no
    # PDF, so `r[1]` no longer means "got a PDF".
    requested = {norm_doi(d) for d in todo_canonical}
    after = artifact_index(papers_dir, project_dir)
    got = {cls: sum(1 for k in requested if cls in after.get(k, ()))
           for cls in (PDF, STRUCTURED, SI)}
    total = len(requested)

    print("\n=== Done ===")
    print(f"  PDF:          {got[PDF]}/{total} "
          f"({got[PDF] / max(total, 1) * 100:.1f}%)")
    if want_structured:
        print(f"  Structured:   {got[STRUCTURED]}/{total}  (xml, else full-text html)")
    if want_supplementary:
        print(f"  Supplementary manifests: {got[SI]}/{total}")
    print(f"  Files in:     {papers_dir}")
    if got[PDF] < total:
        print(f"  Missing PDFs: {staging}/missing_pdfs.html")
        if want_structured:
            print("                (under-reports: a paper that got XML but no PDF "
                  "counts as a success there)")
    # Nothing at all for any requested paper is a failed step (dead network,
    # rejected keys, a broken fetchpdf install), not a run with bad luck.
    if total and not got[PDF] and not got[STRUCTURED]:
        print(f"{RED}ERROR: 0 of {total} papers yielded a PDF or structured text.{RESET}",
              file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download PDFs for classified papers, filtered by article type.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", required=True,
                        help="Path to the review's YAML config (drives project slug)")
    parser.add_argument("--input", default=None,
                        help="Path to classifications CSV "
                             "(default: projects/{slug}/data/classifications.csv)")
    parser.add_argument("--output-dir", "--papers-dir", dest="output_dir",
                        default=None,
                        help="The paper store to download into "
                             "(default: the config's base_pdf_dir, else "
                             "projects/{slug}/papers)")
    parser.add_argument("--no-auto-migrate", action="store_true",
                        help="Don't bring a legacy pdfs/ + converted/ project "
                             "across to the paper store first")
    parser.add_argument("--types", default="",
                        help="Comma-separated article types to download. "
                             "Default: the config's `pdf_types`, else "
                             f"'{','.join(PDF_ELIGIBLE_TYPES)}'")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers (default: 4)")
    parser.add_argument("--delay", type=float, default=0.2,
                        help="Per-worker delay between API calls (default: 0.2)")
    parser.add_argument("--no-extra-sources", dest="no_extra_sources",
                        action="store_true",
                        help="Use only fetchpdf's built-in sources; do not load "
                             "plugins registered under the 'ber.pdf_sources' "
                             "entry-point group")
    parser.add_argument("--relevant-only", action="store_true",
                        help="Additionally drop is_relevant=unknown rows "
                             "(default: keep yes and unknown)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap on papers to attempt this run (0 = all)")
    parser.add_argument("--only-dois", default=None,
                        help="Path to a CSV/text file of DOIs (one per line, or a "
                             "'doi' column). Restrict downloads to just these DOIs "
                             "(incremental updates). Missing files fail; empty "
                             "files select no papers.")
    parser.add_argument("--quiet", dest="verbose", action="store_false",
                        default=True,
                        help="Suppress per-paper progress from the package")
    # Tri-state (default=None) so precedence is --flag > config > constant.
    parser.add_argument("--structured", dest="structured", action="store_true",
                        default=None,
                        help="Also fetch publisher XML (else full-text HTML) "
                             "alongside the PDF. ON by default; config "
                             "`fulltext.structured: false` or --no-structured "
                             "turns it off.")
    parser.add_argument("--no-structured", dest="structured", action="store_false",
                        help="PDF only, as before structured text existed.")
    parser.add_argument("--supplementary", dest="supplementary",
                        action="store_true", default=None,
                        help="Also fetch supplementary/supporting information "
                             "files plus a per-paper manifest. OFF by default "
                             "(slow, and files can be very large); config key "
                             "`fulltext.supplementary`.")
    parser.add_argument("--no-supplementary", dest="supplementary",
                        action="store_false",
                        help="Skip supplementary material.")
    parser.add_argument("--refresh-supplementary", action="store_true",
                        help="Re-run the supplementary pass over papers that "
                             "already have a manifest, picking up files "
                             "deposited since. Implies --supplementary.")
    parser.add_argument("--data-artifacts", action="store_true",
                        help="Also download linked data deposits (Zenodo, Dryad, "
                             "OSF, figshare). Implies --supplementary.")
    parser.add_argument("--max-si-mb", type=float, default=None,
                        help="Per-file cap for supplementary downloads in MB "
                             "(default: fetchpdf's own 300 MB)")
    return parser


if __name__ == "__main__":
    main(build_parser().parse_args())
