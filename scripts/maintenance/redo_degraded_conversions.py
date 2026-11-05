#!/usr/bin/env python3
"""Re-convert + re-extract papers whose text was converted WITHOUT tables.

WHY
---
When docling can't load, pdf4llm falls back to `grobid-only` text, which
contains no tables at all (GROBID's tables land in an object the folder
renderer never writes). Pass-2 then extracts from table-free text and silently
misses every outcome reported only in a table — the record looks complete and
nothing raises. `docling_guard.py` now stops that at the source; this script
repairs corpora converted before the guard existed.

WHAT IT DOES (per paper, mirroring the manual fix validated on
10.3233/CH-211147, which recovered 2 co-primary outcomes and a wrong duration):

  1. find papers whose folder's provenance.json shows a table-free mode AND that
     already have a pass-2 extraction record (only those cost money to redo, and
     only those have a wrong record on disk);
  2. re-convert their PDFs in a SCRATCH paper store (a temp dir laid out like
     the real one, so convert_pdfs.py sees exactly those PDFs), VERIFYING
     each result is table-bearing before touching the workspace — a re-run
     under a still-broken docling would otherwise overwrite one degraded
     conversion with another;
  3. copy ONLY the conversion outputs (body.md, provenance.json, tables.md) back
     into the paper's existing folder — the PDF, publisher XML, supplementary
     files and references.json are left exactly as they are — and into any
     eligible_*/<stem> bucket entry that is a real copy rather than a symlink;
  4. drop the stale records from the extraction jsonl (pass-2 resumability skips
     paper_ids already present, and there is no --redo flag), keeping a .bak;
  5. re-run pass 2 over just those papers.

Dry run unless --execute.

Usage:
    python scripts/maintenance/redo_degraded_conversions.py --config configs/config_x.yaml
    python scripts/maintenance/redo_degraded_conversions.py --config configs/config_x.yaml --execute
    BER_PROJECTS_DIR=<ws>/projects python scripts/maintenance/redo_degraded_conversions.py \
        --config <ws>/config.yaml --execute        # a studio review workspace
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for ber_core

from ber_core import paper_store
from ber_core.conversion import TABLE_BEARING_MODES, read_conversion_quality
from ber_core.storage import atomic_write_json
from ber_core.utils import drop_jsonl_records, load_config, norm_paper_id, read_jsonl, slugify

BASE_DIR = Path(__file__).resolve().parents[2]  # repo root
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

# Rough sonnet cost per pass-2 paper, for the dry-run estimate only.
COST_PER_PAPER = 0.70

EXTRACTION_FILES = {
    "rct": "trial_extractions_pass2.jsonl",
    "observational": "observational_extractions.jsonl",
}

#: The files a conversion writes that this repair replaces. Everything else in
#: the paper's folder (PDF, publisher XML, supplementary/, references.json, the
#: fetch provenance) belongs to other steps and is never touched.
CONVERSION_OUTPUTS = ("body.md", "provenance.json", "tables.md", "document.json", "tables.json", "alternatives.json")

#: The bucket the scratch store files every paper under.
_SCRATCH_BUCKET = "redo"


def _mode_of(paper_dir: Path) -> str | None:
    return read_conversion_quality(paper_dir)["mode"]


def _extracted_ids(data_dir: Path) -> dict[str, set[str]]:
    """design-type -> normalized paper ids with an ok extraction record."""
    out: dict[str, set[str]] = {}
    for design, fname in EXTRACTION_FILES.items():
        path = data_dir / fname
        if not path.exists():
            continue
        out[design] = {norm_paper_id(rec.get("paper_id", ""))
                       for rec in read_jsonl(path, on_error="warn")
                       if rec.get("_status") in (None, "ok")}
    return out


def find_degraded(project_dir: Path, root: Path) -> dict[str, list[tuple[str, str]]]:
    """design-type -> [(stem, mode)] for extracted papers converted table-free."""
    index = paper_store.build_index(project_dir, root=root)
    if not index:
        return {}
    by_design = _extracted_ids(project_dir / "data")
    work: dict[str, list[tuple[str, str]]] = {}
    for key, paper_dir in sorted(index.items()):
        mode = _mode_of(paper_dir)
        if not read_conversion_quality(paper_dir)["requires_review"]:
            continue
        for design, ids in by_design.items():
            if key in ids:
                work.setdefault(design, []).append((paper_dir.name, mode or "unknown"))
    return work


def _find_pdf(project_dir: Path, root: Path, stem: str) -> Path | None:
    """The paper's PDF, wherever it currently sits.

    Normally in the paper's own folder in the store. The legacy locations are
    still checked so this repair tool works on an un-migrated project.
    """
    found = paper_store.paper_dir(project_dir, norm_paper_id(stem), root=root)
    if found is not None:
        cand = found / f"{stem}.pdf"
        if cand.exists():
            return cand
        pdfs = sorted(found.glob("*.pdf"))
        if pdfs:
            return pdfs[0]
    for cand in (project_dir / "converted" / stem / f"{stem}.pdf",
                 project_dir / "pdfs" / f"{stem}.pdf"):
        if cand.exists():
            return cand
    for bucket in project_dir.glob("eligible_*"):
        cand = bucket / stem / f"{stem}.pdf"
        if cand.exists():
            return cand
    return None


def _convert_command(config_path: str, scratch: Path, workers: int) -> list[str]:
    """convert_pdfs.py pointed at a scratch store holding only these PDFs."""
    return [sys.executable, str(BASE_DIR / "steps" / "convert_pdfs.py"),
            "--config", config_path, "--papers-dir", str(scratch),
            "--no-auto-migrate", "--no-resume", "--workers", str(workers)]


def reconvert(project_dir: Path, root: Path, stems: list[str], config_path: str,
              workers: int, scratch: Path | None = None) -> dict[str, Path]:
    """Re-convert *stems* in a scratch store; {stem: folder} for table-bearing ones.

    The scratch store mirrors the real layout (``<bucket>/<stem>/<stem>.pdf``)
    because convert_pdfs.py converts a STORE in place: it used to be handed
    ``--input-dir <staging> --output-dir <out>``, both of which it aliases to
    one tree, so it never saw the staged PDFs and "succeeded" doing nothing.

    A conversion that degrades AGAIN is dropped here, so a still-broken docling
    can never overwrite good-faith old output.
    """
    scratch = scratch or Path(tempfile.mkdtemp(prefix="redo_convert_"))
    n = 0
    for stem in stems:
        pdf = _find_pdf(project_dir, root, stem)
        if pdf is None:
            print(f"  ! no PDF found for {stem} — skipping")
            continue
        folder = scratch / _SCRATCH_BUCKET / stem
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, folder / f"{stem}.pdf")
        n += 1
    if not n:
        return {}

    cmd = _convert_command(config_path, scratch, workers)
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        # The docling guard fires here when docling is broken — that is the
        # whole point: stop, rather than rewrite the corpus with more bad text.
        print("\nConversion step failed (see above). Nothing was modified.")
        sys.exit(proc.returncode)

    good: dict[str, Path] = {}
    for stem in stems:
        new_dir = scratch / _SCRATCH_BUCKET / stem
        if not (new_dir / "body.md").is_file():
            continue
        mode = _mode_of(new_dir)
        if mode in TABLE_BEARING_MODES and not read_conversion_quality(new_dir)["requires_review"]:
            good[stem] = new_dir
        else:
            print(f"  ! {stem} re-converted as {mode!r} (still table-free) — leaving as is")
    return good


def _replace_file(src: Path, dst: Path) -> None:
    """Copy *src* over *dst* atomically (tmp file in the same dir, then rename)."""
    tmp = dst.with_name(f".{dst.name}.redo.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def install(project_dir: Path, root: Path, stem: str, new_dir: Path) -> list[Path]:
    """Copy one paper's new conversion outputs into its existing folder(s).

    Only :data:`CONVERSION_OUTPUTS` are written — this used to ``rmtree`` the
    whole folder and copy the scratch output over it, destroying the publisher
    XML, supplementary files, fetch provenance and references.json.

    ``eligible_*/<stem>`` entries are usually SYMLINKS to the store folder (so
    updating the folder updates them); a real directory there is an older
    run's COPY and is updated the same way. Returns the folders written.
    """
    # Validate before replacing any evidence, including a body from a caller
    # other than reconvert(). The previous provenance may itself be corrupt.
    updated = json.loads((new_dir / "provenance.json").read_text())
    if not isinstance(updated, dict) or not (new_dir / "body.md").is_file():
        raise ValueError("Replacement conversion needs a body and object provenance")
    quality = read_conversion_quality(new_dir)
    if quality["mode"] not in TABLE_BEARING_MODES or quality["requires_review"]:
        raise ValueError("Replacement conversion still requires review")
    with paper_store.store_lock(root, exclusive=True, timeout=30) as acquired:
        if not acquired:
            raise RuntimeError("Paper store is busy; conversion was not installed")
        return _install_locked(project_dir, root, stem, new_dir, updated)


def _install_locked(project_dir, root, stem, new_dir, updated):
    home = paper_store.paper_dir(project_dir, norm_paper_id(stem), root=root)
    targets = [home if home is not None else project_dir / "converted" / stem]
    targets += [b / stem for b in sorted(project_dir.glob("eligible_*"))
                if (b / stem).is_dir() and not (b / stem).is_symlink()]
    written = []
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        history = target / "conversion_history"
        history.mkdir(exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix="before-repair-", dir=history))
        for name in CONVERSION_OUTPUTS:
            if (target / name).is_file():
                shutil.copy2(target / name, backup / name)
        merged = dict(updated)
        try:
            old = json.loads((target / "provenance.json").read_text())
            if not isinstance(old, dict):
                old = {}
        except (OSError, ValueError):
            old = {}
        for key, value in old.items():
            if key.startswith("structured_") or key == "supplementary_files":
                merged[key] = value
        merged["previous_conversion_archive"] = str(backup.relative_to(target))
        try:
            for name in CONVERSION_OUTPUTS:
                if name == "provenance.json":
                    continue
                src = new_dir / name
                if src.is_file():
                    _replace_file(src, target / name)
                else:
                    # Retained in the archive, but no longer evidence for the
                    # new body (especially old OCR alternatives/table sidecars).
                    (target / name).unlink(missing_ok=True)
            atomic_write_json(target / "provenance.json", merged)
        except Exception:
            for name in CONVERSION_OUTPUTS:
                if (backup / name).is_file():
                    _replace_file(backup / name, target / name)
                else:
                    (target / name).unlink(missing_ok=True)
            raise
        (target / "conversion_failed.marker").unlink(missing_ok=True)
        written.append(target)
        print(f"    installed -> {target}")
    return written


def drop_records(data_dir: Path, design: str, stems: list[str]) -> int:
    """Remove those papers' records so pass 2 will redo them (it skips ids that
    are already present). Keeps a timestamped .bak."""
    path = data_dir / EXTRACTION_FILES[design]
    if not path.exists():
        return 0
    wanted = {norm_paper_id(s) for s in stems}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".{stamp}.bak")
    shutil.copy2(path, backup)
    dropped = drop_jsonl_records(
        path, lambda rec: norm_paper_id(rec.get("paper_id", "")) in wanted)
    if not dropped:
        backup.unlink()
    return dropped


def reextract(project_dir: Path, root: Path, design: str, stems: list[str],
              config_path: str, workers: int) -> None:
    """Run pass 2 over just these papers via a staging dir of their folders."""
    staging = Path(tempfile.mkdtemp(prefix="redo_extract_"))
    bucket = project_dir / f"eligible_{design}"
    for stem in stems:
        src = bucket / stem
        if not src.is_dir():
            src = (paper_store.paper_dir(project_dir, norm_paper_id(stem), root=root)
                   or project_dir / "converted" / stem)
        if src.is_dir():
            shutil.copytree(src, staging / stem)
    cmd = [sys.executable, str(BASE_DIR / "steps" / "extract_pass2.py"),
           "--config", config_path, "--design-type", design,
           "--input", str(staging), "--workers", str(workers)]
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    subprocess.run(cmd, check=False)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="The review's YAML config")
    ap.add_argument("--dry-run", action="store_true",
                    help="List the worklist and cost, change nothing. This is "
                         "the default; the flag only makes it explicit.")
    ap.add_argument("--execute", action="store_true",
                    help="Actually re-convert and re-extract")
    ap.add_argument("--convert-workers", type=int, default=2)
    ap.add_argument("--extract-workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap papers per design type (0 = all)")
    args = ap.parse_args(argv)
    if args.dry_run and args.execute:
        ap.error("--dry-run and --execute are mutually exclusive")

    cfg = load_config(args.config)
    slug = slugify((cfg.get("topic") or {}).get("name", "default"))
    project_dir = PROJECTS_DIR / slug
    # dirs.base_pdf_dir: without it the store was resolved as
    # <project>/papers, found empty, and the tool reported "nothing to do".
    root = paper_store.papers_root(project_dir, config=cfg)
    print(f"Project: {slug} -> {project_dir}")
    print(f"Store:   {root}")

    work = find_degraded(project_dir, root)
    if not work:
        print("\nNo extracted papers are sitting on table-free conversions. "
              "Nothing to do.")
        return

    total = 0
    for design, items in work.items():
        if args.limit:
            work[design] = items = items[:args.limit]
        total += len(items)
        print(f"\n{design}: {len(items)} paper(s) extracted from table-free text")
        for stem, mode in items[:10]:
            print(f"    {stem}   ({mode})")
        if len(items) > 10:
            print(f"    … and {len(items) - 10} more")

    print(f"\nTotal: {total} paper(s)  ~${total * COST_PER_PAPER:,.0f} to re-extract")

    if not args.execute:
        print("\nDry run — nothing changed. Re-run with --execute to repair.")
        return

    for design, items in work.items():
        stems = [s for s, _ in items]
        print(f"\n=== {design}: re-converting {len(stems)} paper(s) ===")
        good = reconvert(project_dir, root, stems, args.config, args.convert_workers)
        if not good:
            print("  nothing re-converted cleanly — skipping this design type")
            continue
        print(f"\n  {len(good)} conversion(s) now carry tables; installing…")
        for stem, new_dir in good.items():
            install(project_dir, root, stem, new_dir)
        n = drop_records(project_dir / "data", design, list(good))
        print(f"\n  dropped {n} stale extraction record(s) (backup written)")
        reextract(project_dir, root, design, list(good), args.config,
                  args.extract_workers)

    print("\nDone. Re-run without --execute to confirm the worklist is empty.")


if __name__ == "__main__":
    main()
