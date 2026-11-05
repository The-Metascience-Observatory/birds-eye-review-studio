"""Migrate a project from the legacy split layout to the per-paper store.

BEFORE — one paper's artifacts spread across two trees, which one depending on
how far the pipeline got:

    projects/<slug>/pdfs/10.1000--a.pdf           (+ .xml, _supplementary_info.json, ...)
    projects/<slug>/converted/10.1000--a/body.md  (+ the PDF once convert moved it)
    projects/<slug>/eligible_rct/10.1000--a       -> ../converted/10.1000--a

AFTER — one folder per paper, filed under its screened article type:

    projects/<slug>/papers/Clinical_Trial/10.1000--a/
        10.1000--a.pdf  10.1000--a.xml  body.md  provenance.json
    projects/<slug>/eligible_rct/10.1000--a  -> ../papers/Clinical_Trial/10.1000--a

Dry-run by default: it prints the plan and touches nothing. ``--execute``
performs it. Idempotent — a second run finds nothing to do.

Every relocation is a ``rename()`` within the project, so **hardlinks survive**
(corpora seeded by hardlinking PDFs in from other projects keep their links, and
the migration costs no extra disk).

Usage:
    python scripts/maintenance/migrate_to_paper_store.py --config configs/config_my_topic.yaml
    python scripts/maintenance/migrate_to_paper_store.py --config <ws>/config.yaml --execute
    python scripts/maintenance/migrate_to_paper_store.py --project-dir projects/<slug> --execute
    python scripts/maintenance/migrate_to_paper_store.py --config <cfg> --execute --remove-legacy
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ber_core import paper_store
from ber_core.utils import decode_artifact_stem, doi_to_filename, load_config, project_paths

YELLOW, GREEN, RED, RESET = "\033[93m", "\033[92m", "\033[91m", "\033[0m"

#: Files in a legacy ``pdfs/`` dir that belong to the run, not to any paper.
#: They move to ``papers/_incoming/`` so the reports stay reachable.
RUN_LEVEL_NAMES = ("missing_pdfs.html", "failed_dois.csv", "fetch_report.html")


@dataclass
class PaperPlan:
    """Everything that must end up in one paper's folder."""
    stem: str
    key: str
    dest: Path
    src_dir: Path | None = None          # legacy converted/<stem>/ to relocate
    files: list[Path] = field(default_factory=list)   # flat pdfs/ artifacts

    @property
    def n_artifacts(self) -> int:
        n = len(self.files)
        if self.src_dir is not None and self.src_dir.is_dir():
            n += sum(1 for _ in self.src_dir.iterdir())
        return n


@dataclass
class MigrationPlan:
    project_dir: Path
    #: The store root, resolved ONCE from the config (``dirs.base_pdf_dir``) —
    #: this used to call ``papers_root(project_dir)`` with no config, so the
    #: migration wrote to ``projects/<slug>/papers`` while download_pdfs.py
    #: wrote to the configured directory.
    root: Path | None = None
    papers: list[PaperPlan] = field(default_factory=list)
    run_level: list[Path] = field(default_factory=list)
    already_migrated: int = 0
    types: Counter = field(default_factory=Counter)
    untyped_stems: list[str] = field(default_factory=list)
    #: Files in a legacy dir whose name carries no recognizable paper identity,
    #: so they cannot be filed. Reported, never moved and never deleted.
    unidentified: list[Path] = field(default_factory=list)
    #: Colliding files kept as `<name>.dup` because their content differed.
    duplicates: list[Path] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.papers and not self.run_level


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def load_types(data_dir: Path) -> dict[str, str]:
    """Normalized DOI -> article_type, from ``classifications.csv``.

    Missing file or column is not an error: every paper simply files under
    ``_unclassified`` and a later refile sorts them once screening runs.
    """
    csv_path = Path(data_dir) / "classifications.csv"
    if not csv_path.exists():
        return {}
    out: dict[str, str] = {}
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip()
            if doi:
                out[decode_artifact_stem(doi)] = (row.get("article_type") or "").strip()
    return out


def plan_migration(project_dir: Path, types: dict[str, str] | None = None, *,
                   config: dict | None = None, root: Path | None = None) -> MigrationPlan:
    """Work out every move without performing any of them."""
    project_dir = Path(project_dir)
    types = types if types is not None else load_types(project_dir / "data")
    root = paper_store.papers_root(project_dir, root, config=config)
    plan = MigrationPlan(project_dir=project_dir, root=root)
    valid_types = (config or {}).get("article_types") or None

    existing = paper_store.build_index(project_dir, legacy=False, root=root, warn=False)
    plan.already_migrated = len(existing)

    plans: dict[str, PaperPlan] = {}

    def plan_for(key: str, stem: str) -> PaperPlan:
        if key not in plans:
            article_type = types.get(key)
            if article_type is None:
                plan.untyped_stems.append(stem)
            bucket = paper_store.type_folder(article_type, valid_types)
            plan.types[bucket] += 1
            plans[key] = PaperPlan(stem=stem, key=key,
                                   dest=existing.get(key, root / bucket / stem))
        return plans[key]

    # --- Legacy converted/<stem>/ folders ---
    converted = project_dir / "converted"
    if converted.is_dir():
        for child in sorted(converted.iterdir()):
            if not child.is_dir() or not paper_store.is_paper_stem(child.name):
                continue
            key = decode_artifact_stem(child.name)
            plan_for(key, child.name).src_dir = child

    # --- Legacy flat pdfs/ artifacts ---
    pdfs_dir = project_dir / "pdfs"
    for key, files in paper_store.legacy_flat_artifacts(pdfs_dir).items():
        if key in existing:
            # The paper is already filed; its stray flat artifacts still need
            # to join it, so target the folder it actually occupies.
            pp = plans.get(key)
            if pp is None:
                pp = PaperPlan(stem=doi_to_filename(key), key=key, dest=existing[key])
                plans[key] = pp
            pp.files.extend(files)
            continue
        plan_for(key, _stem_of(files)).files.extend(files)

    # --- Run-level reports ---
    if pdfs_dir.is_dir():
        for name in RUN_LEVEL_NAMES:
            p = pdfs_dir / name
            if p.is_file():
                plan.run_level.append(p)

    # --- Anything we cannot identify ---
    # Names that are neither a DOI nor a `pmid_` stem: bare numeric PMIDs in
    # older corpora, hand-added grey-literature entries like
    # `grey:miller-2023-thai-prevention`. They have no derivable paper identity,
    # so they are left exactly where they are and REPORTED — silently leaving
    # them behind would let "migration complete" hide artifacts the store never
    # picked up. Anything still linked from an eligible_* bucket keeps working.
    if pdfs_dir.is_dir():
        run_level = {p.name for p in plan.run_level}
        for path in sorted(pdfs_dir.iterdir()):
            if not path.is_file() or path.name in run_level:
                continue
            if not paper_store.is_paper_stem(_safe_stem(path.name)):
                plan.unidentified.append(path)

    if converted.is_dir():
        for child in sorted(converted.iterdir()):
            if child.name.startswith("."):
                continue  # the pdf4llm checkpoint, relocated by apply_migration
            if child.is_dir() and not paper_store.is_paper_stem(child.name):
                plan.unidentified.append(child)

    plan.papers = [plans[k] for k in sorted(plans)]
    return plan


def _stem_of(files: list[Path]) -> str:
    """The directory-name stem for a group of flat artifacts.

    Uses the artifact filenames rather than a re-encoded DOI: fetchpdf and this
    repo escape DOIs differently, and re-encoding would name the folder
    something the files inside it don't match.
    """
    from ber_core.utils import strip_artifact_suffix
    return strip_artifact_suffix(sorted(f.name for f in files)[0])


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def apply_migration(plan: MigrationPlan) -> int:
    """Perform the planned moves. Returns the number of papers relocated.

    Collisions that are byte-identical are dropped; collisions with DIFFERENT
    content are kept as ``<name>.dup`` and recorded in ``plan.duplicates``. Two
    distinct PDFs for one DOI is a real state (a re-download that resolved to
    another copy), and a migration must not be the thing that decides one of
    them was worthless.
    """
    root = plan.root or paper_store.papers_root(plan.project_dir)
    root.mkdir(parents=True, exist_ok=True)

    moved = 0
    for pp in plan.papers:
        if pp.src_dir is not None and pp.src_dir.is_dir():
            # Whole-directory rename: one syscall, hardlinks preserved.
            pp.dest.parent.mkdir(parents=True, exist_ok=True)
            plan.duplicates += paper_store.merge_artifact(pp.src_dir, pp.dest)
        else:
            pp.dest.mkdir(parents=True, exist_ok=True)

        for src in pp.files:
            if not src.is_file():
                continue
            plan.duplicates += paper_store.merge_artifact(src, pp.dest / src.name)
        moved += 1

    if plan.run_level:
        staging = paper_store.incoming_dir(plan.project_dir, root=root)
        for src in plan.run_level:
            plan.duplicates += paper_store.merge_artifact(src, staging / src.name)

    # The converter's checkpoint tracked absolute paths under converted/; it
    # belongs with the store now (its stem-based fallback still resolves).
    legacy_ckpt = plan.project_dir / "converted" / ".pdf4llm_checkpoint.json"
    if legacy_ckpt.is_file():
        target = root / ".pdf4llm_checkpoint.json"
        plan.duplicates += paper_store.merge_artifact(legacy_ckpt, target)

    return moved


def verify_migration(plan: MigrationPlan) -> list[str]:
    """Post-migration checks. Returns a list of problems (empty when clean)."""
    problems: list[str] = []
    index = paper_store.build_index(plan.project_dir, legacy=False,
                                    root=plan.root, warn=False)

    for pp in plan.papers:
        found = index.get(pp.key)
        if found is None:
            problems.append(f"{pp.stem}: no folder in the store after migration")
            continue
        if not any(found.iterdir()):
            problems.append(f"{pp.stem}: folder is empty")

    for legacy in ("pdfs", "converted"):
        d = plan.project_dir / legacy
        if not d.is_dir():
            continue
        stragglers = [p for p in d.iterdir()
                      if paper_store.is_paper_stem(
                          p.name if p.is_dir() else _safe_stem(p.name))]
        if stragglers:
            problems.append(
                f"{legacy}/: {len(stragglers)} paper artifact(s) left behind "
                f"(e.g. {stragglers[0].name})")

    return problems


def _safe_stem(name: str) -> str:
    from ber_core.utils import strip_artifact_suffix
    return strip_artifact_suffix(name)


def remove_legacy_dirs(project_dir: Path) -> list[str]:
    """Delete ``pdfs/`` and ``converted/`` when empty. Returns what was removed."""
    removed = []
    for name in ("pdfs", "converted"):
        d = Path(project_dir) / name
        if not d.is_dir():
            continue
        if any(d.iterdir()):
            continue
        d.rmdir()
        removed.append(name)
    return removed


# --------------------------------------------------------------------------
# The one-call entry point other scripts use
# --------------------------------------------------------------------------

def needs_migration(project_dir: Path) -> bool:
    """True when this project still has paper artifacts in the legacy layout.

    A project with a store can still need migrating — a half-finished run, or a
    fetch that wrote flat files before the store existed — so this asks about
    legacy artifacts, not about whether ``papers/`` exists.
    """
    project_dir = Path(project_dir)
    converted = project_dir / "converted"
    if converted.is_dir() and any(
            c.is_dir() and paper_store.is_paper_stem(c.name)
            for c in converted.iterdir()):
        return True
    return bool(paper_store.legacy_flat_artifacts(project_dir / "pdfs"))


def migrate_in_place(project_dir: Path, quiet: bool = False, *,
                     config: dict | None = None, root: Path | None = None) -> MigrationPlan:
    """Plan + apply + relink, for callers that auto-migrate on first touch."""
    plan = plan_migration(project_dir, config=config, root=root)
    if plan.is_empty:
        return plan
    if not quiet:
        print(f"{YELLOW}Migrating {len(plan.papers)} paper(s) into the "
              f"papers/ store at {plan.root}...{RESET}", file=sys.stderr)
    apply_migration(plan)
    paper_store.relink_buckets(project_dir, root=plan.root)
    return plan


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _describe_unidentified(plan: MigrationPlan) -> None:
    """Report files the store will never pick up.

    Printed whether or not there is anything to migrate: a run that says
    "nothing to do" while artifacts sit unreachable in pdfs/ is exactly the
    silent gap this migration is supposed to close.
    """
    if not plan.unidentified:
        return
    print(f"\n{YELLOW}{len(plan.unidentified)} entr(ies) carry no recognizable "
          f"paper identity (not a DOI or pmid_ stem), so they cannot be filed. "
          f"They are LEFT WHERE THEY ARE, untouched:{RESET}")
    by_kind = Counter(
        f"{p.parent.name}/  {'(dir)' if p.is_dir() else '*' + (p.suffix or '')}"
        for p in plan.unidentified)
    for kind, n in by_kind.most_common(6):
        print(f"    {n:5d}  {kind}")
    print(f"    e.g. {plan.unidentified[0].parent.name}/{plan.unidentified[0].name}")


def describe(plan: MigrationPlan) -> None:
    print(f"Project:  {plan.project_dir}")
    print(f"Store:    {plan.root or paper_store.papers_root(plan.project_dir)}")
    if plan.already_migrated:
        print(f"Already in the store: {plan.already_migrated} paper(s)")

    if plan.is_empty:
        print(f"\n{GREEN}Nothing to migrate — the layout is already current.{RESET}")
        _describe_unidentified(plan)
        return

    total_files = sum(pp.n_artifacts for pp in plan.papers)
    print(f"\nTo migrate: {len(plan.papers)} paper(s), {total_files} artifact file(s)")
    if plan.run_level:
        print(f"            {len(plan.run_level)} run-level report(s) -> "
              f"{paper_store.INCOMING}/")

    print("\nDestination buckets:")
    for bucket, n in plan.types.most_common():
        print(f"  {n:6d}  {bucket}")
    if plan.untyped_stems:
        print(f"\n  {len(plan.untyped_stems)} paper(s) have no classifications row "
              f"-> {paper_store.UNCLASSIFIED}/")

    _describe_unidentified(plan)

    print("\nExamples:")
    for pp in plan.papers[:5]:
        print(f"  {pp.stem}")
        print(f"    -> {pp.dest.relative_to(plan.project_dir)}  "
              f"({pp.n_artifacts} file(s))")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a project to the papers/<Type>/<stem>/ store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=None,
                        help="Path to the project's YAML config (drives the slug)")
    parser.add_argument("--project-dir", default=None,
                        help="Project directory to migrate, instead of --config")
    parser.add_argument("--execute", action="store_true",
                        help="Perform the migration (default: dry run)")
    parser.add_argument("--remove-legacy", action="store_true",
                        help="Delete the emptied pdfs/ and converted/ dirs afterwards")
    args = parser.parse_args()

    config = load_config(args.config) if args.config else None
    if args.project_dir:
        project_dir = Path(args.project_dir)
    elif config is not None:
        project_dir = project_paths(config)["project_dir"]
    else:
        print("ERROR: pass --config or --project-dir", file=sys.stderr)
        return 2

    if not project_dir.is_dir():
        print(f"ERROR: project directory not found: {project_dir}", file=sys.stderr)
        return 1

    if config is not None:
        paper_store.remember_root(project_dir, config)
    plan = plan_migration(project_dir, config=config)
    describe(plan)

    if plan.is_empty:
        # An already-migrated project is exactly when --remove-legacy is useful,
        # so it must not be skipped by the early return.
        if args.remove_legacy and args.execute:
            _drop_legacy(project_dir)
        elif args.remove_legacy:
            print(f"\n{YELLOW}--remove-legacy needs --execute.{RESET}")
        return 0

    if not args.execute:
        print(f"\n{YELLOW}DRY RUN — nothing was moved. Re-run with --execute.{RESET}")
        return 0

    print("\nMigrating...")
    moved = apply_migration(plan)
    relinked = paper_store.relink_buckets(project_dir, root=plan.root)
    print(f"  {moved} paper(s) relocated")
    print(f"  {relinked} eligible_*/ symlink(s) re-pointed")
    if plan.duplicates:
        print(f"  {YELLOW}{len(plan.duplicates)} colliding file(s) had DIFFERENT "
              f"content and were kept alongside as *.dup — review them:{RESET}")
        for p in plan.duplicates[:5]:
            print(f"    {p.parent.name}/{p.name}")
        if len(plan.duplicates) > 5:
            print(f"    ... and {len(plan.duplicates) - 5} more")
    if plan.unidentified:
        print(f"  {YELLOW}{len(plan.unidentified)} unidentifiable file(s) left "
              f"in pdfs/ (see above){RESET}")

    problems = verify_migration(plan)
    if problems:
        print(f"\n{RED}Verification found {len(problems)} problem(s):{RESET}")
        for p in problems[:20]:
            print(f"  - {p}")
        return 1

    print(f"\n{GREEN}Verified: every migrated paper resolves in the store.{RESET}")

    if args.remove_legacy:
        _drop_legacy(project_dir)

    return 0


def _drop_legacy(project_dir: Path) -> None:
    """Remove the emptied legacy dirs, saying why when one has to stay."""
    removed = remove_legacy_dirs(project_dir)
    if removed:
        print(f"\nRemoved empty legacy dir(s): {', '.join(removed)}")
    for name in ("pdfs", "converted"):
        d = Path(project_dir) / name
        if d.is_dir() and any(d.iterdir()):
            n = sum(1 for _ in d.iterdir())
            print(f"{YELLOW}Kept {name}/ — {n} entr(ies) still in it "
                  f"(see the unidentified list above).{RESET}")


if __name__ == "__main__":
    sys.exit(main())
