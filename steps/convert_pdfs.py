#!/usr/bin/env python3
"""Convert downloaded PDFs to structured Markdown + JSON using pdf4llm.

Works IN PLACE inside the paper store: for every paper under
projects/{slug}/papers/<Type>/<stem>/ that has a PDF but no converted text, the
converted output is written into that same folder, beside its PDF:
  abstract.md, body.md, references.json, references.md, tables.md, provenance.json

The PDF never moves — one paper, one folder, whatever stage it has reached.

Project-aware and resumable: skips papers already converted, via both the
checkpoint and the presence of body.md.

Usage (every run names its review config):
    python steps/convert_pdfs.py --config configs/config_x.yaml
    python steps/convert_pdfs.py --config ... --workers 2
    python steps/convert_pdfs.py --config ... --profile content  # default: Docling
    python steps/convert_pdfs.py --config ... --device cuda      # require CUDA
    python steps/convert_pdfs.py --config ... --profile bibliographic  # GROBID + Docling
    python steps/convert_pdfs.py --config ... --mode fast        # explicit legacy text mode
    python steps/convert_pdfs.py --config ... --docling-only     # content profile alias
    python steps/convert_pdfs.py --config ... --no-marker-fallback
    python steps/convert_pdfs.py --config ... --limit 5          # smoke test
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.conversion import TABLE_BEARING_MODES, read_conversion_quality
from ber_core.environment import load_local_env as load_dotenv
from ber_core.storage import atomic_text

from ber_core.docling_guard import docling_importable, require_docling
from ber_core import migrate_to_paper_store
from ber_core import paper_store
from ber_core.utils import PaperRef, load_config, slugify

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(BASE_DIR / ".env.local")

PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (BASE_DIR / "projects"))

FAILED_MARKER = "conversion_failed.marker"


def _clear_failed_markers(papers: list[PaperRef]) -> int:
    """Drop the failure marker from every paper carrying one, so this run
    re-attempts them.

    Without this a marked paper counts as "already attempted" forever — a paper
    whose conversion failed while (say) GROBID was down could never be converted
    again, even after the cause was fixed. Nothing moves: the paper is already
    in its own folder with its PDF, which is all a retry needs.
    """
    n = 0
    for ref in papers:
        marker = ref.dir / FAILED_MARKER
        if marker.exists():
            marker.unlink()
            n += 1
    return n


# Conversion modes that capture table structure — ONE definition, shared with
# the repair tool and the server (see ber_core/conversion.py).
_TABLE_BEARING_MODES = TABLE_BEARING_MODES


def _report_conversion_modes(paper_dirs: dict[str, Path]) -> None:
    """Print how many of this run's conversions lost tables.

    Reads each paper folder's `provenance.json` — the one durable record of
    conversion quality, which nothing else in the system reads.
    """
    from collections import Counter

    modes = Counter()
    degraded: list[str] = []
    for stem in sorted(paper_dirs):
        prov = paper_dirs[stem] / "provenance.json"
        if not prov.exists():
            continue
        mode = read_conversion_quality(paper_dirs[stem])["mode"]
        modes[mode] += 1
        if mode not in _TABLE_BEARING_MODES:
            degraded.append(stem)

    if not modes:
        return
    print("\nConversion modes:")
    for mode, n in modes.most_common():
        flag = "" if mode in _TABLE_BEARING_MODES else "   <-- NO TABLES"
        print(f"  {str(mode):26} {n:5}{flag}")
    if degraded:
        print(f"\n  WARNING: {len(degraded)} conversion(s) captured NO TABLES. "
              f"Extraction from these will silently miss table-only outcomes.")
        print("  Re-convert them once docling is healthy: "
              "python scripts/maintenance/redo_degraded_conversions.py --config <cfg> --execute")
    flagged = [(stem, read_conversion_quality(folder)) for stem, folder in paper_dirs.items()]
    for stem, quality in flagged:
        if quality["requires_review"]:
            print(f"  REVIEW {stem}: {'; '.join(quality['warnings']) or 'conversion requires review'}")


def _convert_html_papers(project_dir: Path, papers_dir: Path,
                         limit: int = 0, *, fix_docling: bool = False,
                         allow_degraded: bool = False) -> tuple[int, int, int]:
    """Convert papers whose ONLY full text is publisher HTML into body.md.

    Writes `body.md` rather than teaching every reader about a fourth path:
    `discover_papers` and `read_best_text` already prefer markdown, so the
    paper becomes visible to both extraction passes with no other change.
    Tables are embedded as HTML to keep row/col spans (html_convert.__doc__).

    Returns ``(attempted, converted, failed)``. Docling is probed FIRST: it does all of
    this work, and without the probe a broken install showed up as one
    "FAILED" line per paper and a green run.
    """
    import json

    index = paper_store.build_index(project_dir, root=papers_dir)
    from ber_core import html_convert
    todo = html_convert.find_html_only_papers(index)
    if limit > 0:
        todo = todo[:limit]
    if not todo:
        return 0, 0, 0
    if allow_degraded and not docling_importable()[0]:
        # There is no table-free HTML path to degrade to: skip, don't fail.
        print(f"\nHTML-only papers: {len(todo)} — SKIPPED, docling is not "
              f"importable (--allow-degraded)")
        return 0, 0, 0
    require_docling(needed=True, fix=fix_docling, allow_degraded=False)

    print(f"\nHTML-only papers to convert: {len(todo)}")
    done = failed = empty = 0
    for i, (doi, folder, html_path) in enumerate(todo, 1):
        try:
            md = html_convert.html_to_markdown(html_path)
        except Exception as e:                      # noqa: BLE001
            print(f"  [{i}/{len(todo)}] FAILED {doi}: {str(e)[:90]}")
            failed += 1
            continue
        if not md:
            # A consent wall or paywall stub. Not an error, but writing it as
            # body.md would falsely mark the paper as having full text.
            print(f"  [{i}/{len(todo)}] no usable text {doi}")
            empty += 1
            continue
        # Atomic: a reader (the evidence build, a concurrent pass) must never
        # see a half-written body.md, which reads as a real but truncated text.
        atomic_text(folder / "body.md", md)
        prov = folder / "provenance.json"
        try:
            data = json.loads(prov.read_text(encoding="utf-8")) if prov.exists() else {}
        except (OSError, ValueError):
            data = {}
        data["extraction_mode"] = html_convert.HTML_EXTRACTION_MODE
        data["html_source"] = html_path.name
        data["tables_captured"] = html_convert.count_tables(md)
        atomic_text(prov, json.dumps(data, indent=2))
        done += 1
        if i % 10 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] converted={done} "
                  f"no-text={empty} failed={failed}")
    print(f"  HTML conversion: {done} converted, {empty} with no usable text, "
          f"{failed} failed")
    return len(todo), done, failed


def _resolve_profile(args):
    """Default BER to content extraction; keep explicit legacy modes working."""
    profile = getattr(args, "profile", None)
    if args.docling_only:
        profile = "content"
    if profile is None:
        profile = "bibliographic" if args.mode is not None else "content"
    args.profile = profile
    if profile == "content":
        args.docling_only = True
        args.mode = "fast"
        args.no_auto_grobid = True
        args.no_parallel = True
    else:
        args.mode = args.mode or "full-grobid"
    return args


def main(args):
    _resolve_profile(args)
    config = load_config(args.config)
    topic_name = config.get("topic", {}).get("name", "default")
    slug = slugify(topic_name)
    project_dir = PROJECTS_DIR / slug

    # --papers-dir > config dirs.base_pdf_dir > projects/{slug}/papers.
    papers_dir = paper_store.papers_root(project_dir, args.papers_dir, config)
    if not args.papers_dir:
        paper_store.remember_root(project_dir, config)

    print(f"Project:  {topic_name!r} -> {project_dir}")
    print(f"Papers:   {papers_dir}")

    # A project still on the legacy pdfs/ + converted/ split has to be brought
    # across before anything here can find its PDFs. The migration is a set of
    # renames within the project — idempotent, hardlink-preserving, nothing
    # deleted — so doing it on first touch beats failing the step and leaving
    # every un-migrated review's background job broken.
    if not args.no_auto_migrate and migrate_to_paper_store.needs_migration(project_dir):
        plan = migrate_to_paper_store.migrate_in_place(project_dir, config=config, root=papers_dir)
        print(f"Migrated {len(plan.papers)} paper(s) into the store "
              f"(run migrate_to_paper_store.py --help to do this explicitly)")

    if not papers_dir.exists():
        print(f"\nERROR: paper store not found: {papers_dir}")
        print("       Run download_pdfs.py first.")
        sys.exit(1)

    # Writers hold the store lock SHARED for the whole run, so a refile (the
    # end of a re-screen) cannot move a folder pdf4llm is writing into.
    with paper_store.store_lock(papers_dir, exclusive=False):
        _convert(args, project_dir, papers_dir)


def _convert(args, project_dir: Path, papers_dir: Path) -> None:
    all_papers = paper_store.iter_papers(project_dir, root=papers_dir)

    # --- Phase 1: publisher HTML -> body.md ---------------------------------
    # Runs BEFORE the PDF work and needs neither GROBID nor a docling PDF
    # pipeline, so a broken GROBID cannot block it. Papers whose only full text
    # is HTML were invisible to extraction entirely (see html_convert.__doc__).
    html_tried, n_html, html_failed = _convert_html_papers(
        project_dir, papers_dir, limit=args.limit,
        fix_docling=args.fix_docling, allow_degraded=args.allow_degraded)
    # A consent wall yields no text without being a failure; only a run where
    # EVERY attempt raised is systemic.
    html_all_failed = bool(html_tried) and html_failed == html_tried
    if args.html_only:
        print(f"\n--html-only: converted {n_html} of {html_tried} HTML paper(s); "
              f"skipping PDF conversion.")
        if html_all_failed:
            sys.exit(1)
        return

    if args.retry_failed:
        n_cleared = _clear_failed_markers(all_papers)
        print(f"--retry-failed: cleared {n_cleared} failure marker(s)")

    # A paper needs converting when it has a PDF but no extracted text yet.
    # Re-derived from disk rather than from a directory listing, so a paper
    # whose XML full text was installed without a PDF is correctly skipped.
    todo = [r for r in all_papers if r.pdf_path is not None and r.md_path is None]
    if not args.retry_failed:
        todo = [r for r in todo if not (r.dir / FAILED_MARKER).exists()]

    if not todo:
        print("\nNo PDFs found to convert.")
        if html_all_failed:
            print("\nERROR: every HTML conversion this run failed.")
            sys.exit(1)
        return

    if args.limit > 0:
        todo = todo[:args.limit]
        print(f"Limiting to {args.limit} PDFs (smoke test)")

    # Docling carries TABLES; without it this run would write table-free text
    # and extraction would silently lose every table-only outcome. Probe once,
    # before any work, and stop the run rather than degrade it. (Every mode here
    # except an explicit non-docling FAST run depends on it.)
    needs_docling = args.docling_only or args.mode in ("full-grobid", "hybrid")
    require_docling(
        needed=needs_docling,
        fix=args.fix_docling,
        allow_degraded=args.allow_degraded,
    )

    print(f"\nPDFs to convert: {len(todo)} (workers={args.workers}, mode={args.mode})\n")

    from pdf4llm import Config, ExtractionMode
    from pdf4llm.pipeline import BatchProcessor

    mode_map = {
        "full-grobid": ExtractionMode.FULL_GROBID,
        "hybrid": ExtractionMode.HYBRID,
        "fast": ExtractionMode.FAST,
    }

    pdf4llm_config = Config(
        mode=mode_map[args.mode],
        workers=args.workers,
        parallel_extraction=not args.no_parallel,
        use_marker_fallback=not args.no_marker_fallback,
        use_pymupdf_fallback=not args.no_pymupdf_fallback,
        auto_start_grobid=not args.no_auto_grobid,
        grobid_docker_mode=args.grobid_mode,
        # Docling is what preserves table structure when GROBID is out of the
        # picture; without this the FAST path degrades to plain text and the
        # safety tables the extraction depends on are lost.
        use_docling=args.docling_only,
        conversion_profile=args.profile,
        docling_table_mode=getattr(args, "table_mode", "accurate"),
        docling_device=getattr(args, "device", "auto"),
        docling_timeout=getattr(args, "document_timeout", 300),
        **({"crossref_enrich_threshold": 0} if args.profile == "content" else {}),
    )

    checkpoint_file = papers_dir / ".pdf4llm_checkpoint.json"
    processor = BatchProcessor(pdf4llm_config, checkpoint_file=checkpoint_file)

    # pdf4llm writes its output to `output_dir/<stem>/`, so converting IN PLACE
    # means calling it once per type bucket with that bucket as the output dir.
    # process_folder() also scans its input_dir RECURSIVELY, and the store is a
    # tree — so stage each batch's PDFs as symlinks in a flat temp dir and hand
    # it exactly those. `move_pdf=True` is harmless here: pdf4llm resolves the
    # symlink and finds the real PDF is already at its destination, which its
    # `src != dst` guard makes a no-op.
    import tempfile
    from collections import defaultdict

    by_bucket: dict[Path, list[PaperRef]] = defaultdict(list)
    for ref in todo:
        by_bucket[ref.dir.parent].append(ref)

    reports = []
    for bucket in sorted(by_bucket):
        refs = by_bucket[bucket]
        if len(by_bucket) > 1:
            print(f"  {bucket.name}: {len(refs)} PDF(s)")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for ref in refs:
                (tmp_path / ref.pdf_path.name).symlink_to(ref.pdf_path.resolve())
            result = processor.process_folder(
                input_dir=tmp_path,
                output_dir=bucket,
                # --retry-failed must also bypass the checkpoint: the failed
                # PDFs were "attempted" last run, and the checkpoint would skip
                # them again. skip_existing still protects successful
                # conversions (their <stem>/ output exists).
                resume=not (args.no_resume or args.retry_failed),
                skip_existing=True,
                move_pdf=True,
            )
        reports.append(result)

    for result in reports:
        print(result.generate_report())

    # pdf4llm's report counts a table-free grobid-only conversion as a SUCCESS
    # (no ProcessingError is raised when GROBID rescues a docling failure), so
    # degradation is invisible there. Read back what was actually written and
    # say it out loud — a partially degraded run must not read as a clean one.
    _report_conversion_modes({r.stem: r.dir for r in todo})

    # --- Mark papers that failed conversion ---------------------------------
    # Nothing moves: each paper already sits in its own folder holding its PDF.
    # A paper this run attempted but which still has no body.md failed every
    # tier. The marker (rather than a quarantine dir) keeps the raw PDF readable
    # via read_best_text's pymupdf fallback, stops the studio's self-healing
    # fetch job re-downloading in a loop, and is what --retry-failed finds.
    failed = [r for r in todo if not (r.dir / "body.md").exists()]
    if failed:
        print(f"\n{len(failed)} PDF(s) FAILED conversion — marking...")
        for ref in failed:
            (ref.dir / FAILED_MARKER).write_text(
                "All conversion tiers failed for this PDF. The folder holds "
                "only the raw PDF (pymupdf-fallback text still works). "
                "Re-attempt with: convert_pdfs.py --retry-failed\n",
                encoding="utf-8")
        print("  Failed stems:")
        for ref in failed:
            print(f"    {ref.stem}")
        print("  Re-attempt later with: convert_pdfs.py --retry-failed")

    print(f"\nConverted output in: {papers_dir}")
    print(f"Checkpoint:          {checkpoint_file}")

    # Every attempted PDF failing is systemic (GROBID down, bad install), not
    # bad luck with a few corrupt files — fail the step so the runner surfaces
    # it instead of reporting a "successful" conversion of nothing.
    if todo and len(failed) == len(todo):
        print(f"\nERROR: all {len(todo)} attempted conversions failed — "
              f"this looks systemic, not per-file.")
        sys.exit(1)
    if html_all_failed:
        print("\nERROR: every HTML conversion this run failed.")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert downloaded PDFs to structured Markdown + JSON via pdf4llm.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", required=True,
                        help="Path to the review's YAML config (drives project slug)")
    parser.add_argument("--papers-dir", "--input-dir", "--output-dir",
                        dest="papers_dir", default=None,
                        help="The paper store to convert in place "
                             "(default: the config's base_pdf_dir, else "
                             "projects/{slug}/papers/). The old --input-dir / "
                             "--output-dir spellings are accepted as aliases: "
                             "input and output are the same tree now")
    parser.add_argument("--no-auto-migrate", action="store_true",
                        help="Don't bring a legacy pdfs/ + converted/ project "
                             "across to the paper store; fail instead")
    parser.add_argument("--workers", type=int, default=2,
                        help="Parallel papers to process (default: 2; each uses ~8 GB RAM)")
    parser.add_argument("--profile", choices=["content", "bibliographic"], default=None,
                        help="Conversion profile (default: content, Docling without GROBID). "
                             "Bibliographic retains optional GROBID enrichment.")
    parser.add_argument("--mode", default=None,
                        choices=["full-grobid", "hybrid", "fast"],
                        help="Explicit legacy extraction mode; selects bibliographic profile "
                             "unless --profile or --docling-only is supplied")
    parser.add_argument("--table-mode", choices=["fast", "accurate"], default="accurate",
                        help="Docling table model configuration (default: accurate)")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                        help="Converter device; cuda requires working GPU access")
    parser.add_argument("--document-timeout", type=int, default=300,
                        help="Docling process deadline in seconds (default: 300)")
    parser.add_argument("--docling-only", action="store_true",
                        help="Use docling only, skip GROBID (no structured references.json)")
    parser.add_argument("--no-parallel", action="store_true",
                        help="Disable parallel GROBID+docling; use classic cascade instead")
    parser.add_argument("--no-marker-fallback", action="store_true",
                        help="Disable marker-pdf fallback tier")
    parser.add_argument("--no-pymupdf-fallback", action="store_true",
                        help="Disable pymupdf4llm last-resort fallback tier")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore checkpoint and re-process all PDFs")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Clear conversion_failed.marker from previously-"
                             "failed papers and re-attempt them, bypassing "
                             "the checkpoint")
    parser.add_argument("--no-auto-grobid", action="store_true",
                        help="Don't auto-start GROBID via Docker (assume already running)")
    parser.add_argument("--grobid-mode", default="crf",
                        choices=["crf", "delft"],
                        help="GROBID Docker image to use when auto-starting: "
                             "'crf' (lightweight, ~500 MB, default) or "
                             "'delft' (highest accuracy, ~8 GB)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap on PDFs to convert this run (0 = all)")
    parser.add_argument("--fix-docling", action="store_true",
                        help="If docling can't be imported, attempt to "
                             "install/repair it without prompting (also via "
                             "BER_FIX_DOCLING=1), then restart the run")
    parser.add_argument("--html-only", action="store_true",
                        help="Convert only publisher HTML to body.md and stop. "
                             "Needs no GROBID and no PDF pipeline, so it works "
                             "when those are unavailable.")
    parser.add_argument("--allow-degraded", action="store_true",
                        help="Convert even when docling is unavailable. The "
                             "output will contain NO TABLES and extraction "
                             "will silently miss table-only outcomes — only "
                             "for deliberate GROBID-only runs")
    args = parser.parse_args()

    main(args)
