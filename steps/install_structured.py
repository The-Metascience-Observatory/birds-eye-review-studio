#!/usr/bin/env python3
"""Tidy structured full text (XML/HTML) and supplements inside the paper store.

download_pdfs.py now writes fetchpdf's artifacts straight into each paper's
own folder, so this step no longer RELOCATES anything between trees. What it
still does, per paper folder:

1. **It renames.** fetchpdf names files with its own DOI encoder (":" -> "~",
   a literal "-" run -> "~2d~"); folders migrated from the old layout are named
   with this repo's ("/" -> "--" and nothing else). ``discover_papers`` looks
   for exactly ``<dir>/<stem>.xml``, so a differently-named XML sitting in the
   folder is invisible to extraction. Matching is by normalized DOI, never by
   reconstructing a stem.

2. **It files supplements** into ``<paper>/supplementary/`` instead of leaving
   a dozen ``_supplementary_info_*`` files loose in the folder — the point of
   the store being human-navigable.

3. **It never writes `extraction_mode`.** That key is read by three separate
   table-quality gates (convert_pdfs, evidence.py, redo_degraded_conversions)
   and any value they don't recognize reads as DEGRADED — which would paint an
   amber "no tables captured" warning on the single best source in the corpus.
   Structured provenance goes in its own `structured_*` keys instead.

Safe to re-run: idempotent, and it reports what it would do unless --execute.

Ordering note: this used to have to run AFTER convert_pdfs, because creating
an XML-only ``converted/<stem>/`` dir made pdf4llm's ``skip_existing`` treat a
later-arriving PDF as already converted and mask it permanently. That hazard is
gone — every paper has a folder from the moment it is fetched, and both
``skip_existing`` and the convert step's own work list key on ``body.md``, which
structured text never produces. The order no longer matters.

Usage:
    python steps/install_structured.py --config configs/config_x.yaml            # dry run
    python steps/install_structured.py --config configs/config_x.yaml --execute
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core import paper_store
from ber_core.utils import (
    RED, YELLOW, RESET,
    decode_artifact_stem, load_config, slugify, strip_artifact_suffix,
)

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

#: Written into a converted/ dir this script created from structured text alone
#: (no PDF was available). Its only job is to let the reconcile phase recognize
#: its own stubs — a real pdf4llm conversion must never be touched.
STRUCTURED_ONLY_MARKER = "structured_only.marker"

#: Where a paper's supplementary files are filed inside its converted/ dir.
SUPPLEMENT_DIRNAME = "supplementary"

_SUPPLEMENT_MANIFEST = "_supplementary_info.json"
_SUPPLEMENT_INFIX = "_supplementary_info_"
_DATA_ARTIFACT_INFIX = "_data_artifacts"


# --- Indexing ---------------------------------------------------------------

def index_structured(pdfs_dir: Path) -> dict[str, dict]:
    """Normalized DOI -> the structured artifacts fetchpdf wrote for it.

    Values: ``{"stem", "xml", "html", "provenance", "manifest", "supplements"}``
    (paths, or None/[] when absent).
    """
    found: dict[str, dict] = {}

    def slot(stem: str) -> dict:
        key = decode_artifact_stem(stem)
        return found.setdefault(key, {
            "stem": stem, "xml": None, "html": None, "provenance": None,
            "manifest": None, "supplements": [],
        })

    if not pdfs_dir.is_dir():
        return found

    for path in sorted(pdfs_dir.iterdir()):
        name = path.name
        if path.is_dir():
            # {stem}_data_artifacts/ — linked deposits (Zenodo/Dryad/OSF).
            if _DATA_ARTIFACT_INFIX in name:
                stem = name.split(_DATA_ARTIFACT_INFIX, 1)[0]
                slot(stem)["supplements"].append(path)
            continue
        if name.endswith(".xml"):
            slot(strip_artifact_suffix(name))["xml"] = path
        elif name.endswith(".fulltext.html"):
            slot(strip_artifact_suffix(name))["html"] = path
        elif name.endswith(".provenance.json"):
            slot(strip_artifact_suffix(name))["provenance"] = path
        elif name.endswith(_SUPPLEMENT_MANIFEST):
            slot(strip_artifact_suffix(name))["manifest"] = path
        elif _SUPPLEMENT_INFIX in name:
            stem = name.split(_SUPPLEMENT_INFIX, 1)[0]
            slot(stem)["supplements"].append(path)

    # A record with nothing but a PDF is not our business.
    return {k: v for k, v in found.items()
            if v["xml"] or v["html"] or v["manifest"] or v["supplements"]}


def index_store(papers_dir: Path) -> list[tuple[Path, dict]]:
    """Every paper folder that holds structured artifacts, with those artifacts.

    Reuses :func:`index_structured` per folder — the artifacts are flat siblings
    INSIDE the folder now, which is the same shape it always understood. An
    already-filed ``supplementary/`` subdir is skipped by that scan, which is
    what makes re-runs idempotent.

    Returns ``(paper_dir, arts)`` pairs rather than a DOI-keyed map: the folder
    is the identity here, and a folder whose files were named by fetchpdf's
    encoder decodes to a key that need not match its own name.
    """
    out: list[tuple[Path, dict]] = []
    if not papers_dir.is_dir():
        return out
    for bucket in sorted(papers_dir.iterdir()):
        if not bucket.is_dir():
            continue
        for paper_dir in sorted(bucket.iterdir()):
            if not paper_dir.is_dir() or not paper_store.is_paper_stem(paper_dir.name):
                continue
            for arts in index_structured(paper_dir).values():
                out.append((paper_dir, arts))
    return out


# --- Reconcile --------------------------------------------------------------

def clear_stale_stubs(papers_dir: Path, execute: bool) -> int:
    """Drop the structured-only marker from migrated folders that have a PDF.

    The marker used to mean "this dir was invented from XML alone, so a
    later-arriving PDF must not be masked by it". In the store a paper's folder
    always exists and the convert step keys on ``body.md``, so nothing can be
    masked and the marker has no job left — but a stale one left behind by a
    migrated project is confusing. Remove it once its paper has a PDF.
    """
    if not papers_dir.is_dir():
        return 0
    cleared = 0
    for bucket in sorted(papers_dir.iterdir()):
        if not bucket.is_dir():
            continue
        for paper_dir in sorted(bucket.iterdir()):
            marker = paper_dir / STRUCTURED_ONLY_MARKER
            if not marker.exists() or not any(paper_dir.glob("*.pdf")):
                continue
            cleared += 1
            print(f"  clear stale structured-only marker: {paper_dir.name}")
            if execute:
                marker.unlink()
    return cleared


# --- Install ----------------------------------------------------------------

def _xml_flavor(xml_path: Path) -> str:
    """'jats' | 'elsevier' | 'generic', by sniffing the head of the file."""
    try:
        head = xml_path.read_text(encoding="utf-8", errors="replace")[:4000].lower()
    except OSError:
        return "generic"
    if "elsevier.com/xml/ja/dtd" in head:
        return "elsevier"
    if "jats" in head or "<article-meta" in head or "pmc-articleset" in head:
        return "jats"
    return "generic"


def _provider_from_sidecar(path: Path | None) -> str | None:
    """fetchpdf's {stem}.provenance.json records which source served the file."""
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in ("source", "provider", "source_name", "tier_source"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def merge_provenance(paper_dir: Path, info: dict, execute: bool) -> None:
    """Add `structured_*` keys to converted/<stem>/provenance.json.

    Deliberately additive and deliberately silent about `extraction_mode`: that
    key belongs to the PDF conversion, and inventing a value for it would make
    every table-quality gate read this paper as degraded.
    """
    path = paper_dir / "provenance.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    data.update(info)
    if not execute:
        return
    from ber_core.storage import atomic_text
    atomic_text(path, json.dumps(data, indent=2) + "\n")


def install_one(paper_dir: Path, arts: dict, execute: bool) -> str:
    """Tidy one paper's structured artifacts in place. Returns an outcome word.

    Outcomes: ``installed`` (something was renamed, filed or recorded),
    ``unchanged``. The folder always exists — the fetch created it — so this
    never invents one.
    """
    stem = paper_dir.name
    wrote = []

    # --- the structured text itself ---
    # A rename, not a copy: the file is already in the right folder, it just may
    # carry fetchpdf's stem where discover_papers wants the folder's own.
    source = None
    fmt = None
    if arts["xml"]:
        source, fmt = "xml", _xml_flavor(arts["xml"])
        dest = paper_dir / f"{stem}.xml"      # renamed: discover_papers is exact
        if not _same_file(arts["xml"], dest):
            wrote.append(dest.name)
            if execute:
                _place_artifact(arts["xml"], dest, history_dir=paper_dir / 'source_history')
    elif arts["html"]:
        source, fmt = "html", "html"
        dest = paper_dir / f"{stem}.fulltext.html"
        if not _same_file(arts["html"], dest):
            wrote.append(dest.name)
            if execute:
                _place_artifact(arts["html"], dest, history_dir=paper_dir / 'source_history')

    # --- supplements ---
    n_supplements = 0
    if arts["manifest"] or arts["supplements"]:
        sup_dir = paper_dir / SUPPLEMENT_DIRNAME
        for src in ([arts["manifest"]] if arts["manifest"] else []) + arts["supplements"]:
            dest = sup_dir / src.name
            if src.is_dir():
                n_supplements += sum(1 for _ in src.rglob("*") if _.is_file())
                if execute and not dest.exists():
                    shutil.copytree(src, dest)
                    wrote.append(f"{SUPPLEMENT_DIRNAME}/{src.name}/")
                continue
            if src.name.endswith(_SUPPLEMENT_MANIFEST):
                pass          # the manifest is bookkeeping, not a supplement
            else:
                n_supplements += 1
            if not _same_file(src, dest):
                wrote.append(f"{SUPPLEMENT_DIRNAME}/{src.name}")
                if execute:
                    sup_dir.mkdir(parents=True, exist_ok=True)
                    _place_artifact(src, dest, history_dir=paper_dir / 'source_history')

    # Re-check provenance even when every file was already in place: a run
    # interrupted between the move and the provenance write would otherwise be
    # reported "unchanged" forever, leaving the studio blind to the XML.
    prov_stale = not _provenance_records_structured(paper_dir)
    if not wrote and not prov_stale:
        return "unchanged"

    has_body = (paper_dir / "body.md").exists()
    merge_provenance(paper_dir, {
        "structured_source": source,
        "structured_format": fmt,
        "structured_file": f"{stem}.xml" if source == "xml" else (
            f"{stem}.fulltext.html" if source == "html" else None),
        "structured_provider": _provider_from_sidecar(arts["provenance"]),
        "supplementary_files": n_supplements,
        "has_pdf_conversion": has_body,
    }, execute)

    return "installed"


def _place_artifact(src: Path, dest: Path, *, history_dir: Path | None = None) -> None:
    """Move *src* to *dest*, both already inside the paper's folder.

    A move, not a copy: leaving the original behind would double the folder's
    size and put two names for the same content side by side — exactly the
    clutter the store exists to remove. Falls back to copy across devices.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and not _same_file(src, dest) and history_dir is not None:
        # Keep old native evidence outside supplementary/, where recursive
        # extraction would otherwise mistake it for another current source.
        with dest.open('rb') as stream:
            old_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        archive = history_dir / old_hash
        archive.mkdir(parents=True, exist_ok=True)
        if not (archive / dest.name).exists():
            shutil.copy2(dest, archive / dest.name)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
        shutil.rmtree(src, ignore_errors=True)
        return
    try:
        os.replace(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _same_file(src: Path, dest: Path) -> bool:
    """Same-length files can contain different numerical results; compare bytes."""
    try:
        if not dest.is_file() or not src.is_file() or dest.stat().st_size != src.stat().st_size:
            return False
        if src.resolve() == dest.resolve():
            return True
        with src.open('rb') as left, dest.open('rb') as right:
            return hashlib.file_digest(left, 'sha256').digest() == hashlib.file_digest(right, 'sha256').digest()
    except OSError:
        return False


def _provenance_records_structured(paper_dir: Path) -> bool:
    path = paper_dir / "provenance.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("structured_source") is not None


# --- main -------------------------------------------------------------------

def main(args) -> int:
    config = load_config(args.config)
    topic_name = config.get("topic", {}).get("name", "default")
    slug = slugify(topic_name)
    project_dir = PROJECTS_DIR / slug

    papers_dir = paper_store.papers_root(project_dir, args.pdfs_dir, config)

    print(f"Project:   {topic_name!r} -> {project_dir}")
    print(f"Papers:    {papers_dir}")
    if not args.execute:
        print(f"{YELLOW}DRY RUN — pass --execute to apply.{RESET}")

    if not papers_dir.is_dir():
        # Nothing fetched yet is a legitimate state for this step, not an error:
        # the background job runs it on every pass, including the first.
        print("\nNo paper store yet — nothing to install.")
        return 0

    cleared = clear_stale_stubs(papers_dir, args.execute)
    if cleared:
        print(f"  {cleared} stale structured-only marker(s) cleared\n")

    entries = index_store(papers_dir)
    if not entries:
        print("\nNo structured artifacts found "
              "(run download_pdfs.py with structured text enabled).")
        return 0

    outcomes = Counter()
    for paper_dir, arts in entries:
        try:
            outcome = install_one(paper_dir, arts, args.execute)
        except OSError as e:
            print(f"  {RED}ERROR {paper_dir.name}: {e}{RESET}", file=sys.stderr)
            outcomes["failed"] += 1
            continue
        outcomes[outcome] += 1
        if outcome != "unchanged" and args.verbose:
            print(f"  {outcome:9s} {paper_dir.name}")

    xml_count = sum(1 for _, v in entries if v["xml"])
    html_count = sum(1 for _, v in entries if v["html"] and not v["xml"])

    print(f"\n=== {'Installed' if args.execute else 'Would install'} ===")
    print(f"  Papers with structured text: {len(entries)} "
          f"({xml_count} xml, {html_count} html-only)")
    for name in ("installed", "unchanged", "failed"):
        if outcomes[name]:
            print(f"  {name:10s} {outcomes[name]}")
    return 1 if outcomes["failed"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tidy fetched XML/HTML and supplements inside the paper store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", required=True,
                        help="Path to the review's YAML config (drives project slug)")
    parser.add_argument("--pdfs-dir", "--papers-dir", dest="pdfs_dir",
                        default=None,
                        help="The paper store (default: the config's "
                             "base_pdf_dir, else projects/{slug}/papers)")
    parser.add_argument("--execute", action="store_true",
                        help="Apply the changes (default: dry run)")
    parser.add_argument("--quiet", dest="verbose", action="store_false",
                        default=True, help="Only print the summary")
    sys.exit(main(parser.parse_args()))
