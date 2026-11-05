"""The per-paper artifact store: ``projects/<slug>/papers/<Type>/<stem>/``.

ONE folder per paper holding everything the pipeline knows about it — the PDF,
publisher XML, supplementary manifests, the converted ``body.md`` and its
``provenance.json`` — filed under the article type the abstract screen assigned:

    papers/
      Clinical_Trial/
        10.1000--jama.2021.1234/
          10.1000--jama.2021.1234.pdf
          10.1000--jama.2021.1234.xml
          body.md
          provenance.json
      _unclassified/
        10.5555--not-screened-yet/
      _incoming/                     <- download staging, filed by refile()

This replaces the old split between a flat ``pdfs/`` dump and per-paper
``converted/<stem>/`` folders, where one paper's artifacts lived in two places
depending on how far the pipeline had got.

Two invariants callers must respect
-----------------------------------

1. **The type folder is never a lookup key.** ``article_type`` is mutable — a
   re-run of ``classify.py`` can change it, and papers with no abstract carry
   no type at all. Locate a paper with :func:`paper_dir` / :func:`build_index`
   (which scan by stem, type-agnostically); construct ``papers/<type>/<stem>``
   only to PLACE a paper, via :func:`ensure_paper_dir`.

2. **Legacy layouts still resolve.** :func:`build_index` falls back to
   ``converted/<stem>/`` when a project has no ``papers/`` tree, so projects that
   have not been migrated yet keep working. :func:`legacy_flat_artifacts` covers
   the flat ``pdfs/`` dump for the same reason.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ber_core.utils import (
    PaperRef,
    decode_artifact_stem,
    discover_papers,
    doi_to_filename,
    norm_doi,
    strip_artifact_suffix,
)


#: Bucket for papers whose article type is unknown (not screened yet, or the
#: screen produced no usable type). They still get a folder — a paper must
#: never be unfilable — and refile() moves them out once a type lands.
UNCLASSIFIED = "_unclassified"

#: Download staging. ``download_pdfs.py`` points fetchpdf here (it can only
#: nest one level deep), then refile() files each ``<stem>/`` into its type
#: folder. Run-level reports (missing_pdfs.html, failed_dois.csv) stay here.
INCOMING = "_incoming"

#: Folder names under papers/ that are NOT article types.
RESERVED_DIRS = frozenset({UNCLASSIFIED, INCOMING})

#: Article-type values that carry no real type. ``no_abstract`` is what
#: classify.py writes for a paper it could not read an abstract for, and
#: ``parse_error`` what it writes when the answer was unreadable — neither is a
#: bucket (long_covid grew a literal ``parse_error/`` folder before this).
_EMPTY_TYPES = frozenset({"", "no_abstract", "unknown", "none", "n/a", "parse_error"})

#: Per-paper JSON written afresh by every fetch attempt. A re-fetch colliding
#: with an older copy keeps the NEWER one instead of piling up ``.dup`` files
#: (long_covid had 5,060 ``*.provenance.json.dup*``).
_REPLACEABLE_SUFFIXES = (".provenance.json", ".manifest.json")

#: Lock file guarding folder MOVES (refile) against concurrent writers.
LOCK_NAME = ".store.lock"

#: Identifier prefixes a paper folder name may start with. Mirrors
#: ``utils.discover_papers``' default — DOIs and PMIDs both flow downstream.
VALID_PREFIXES = ("10.", "pmid_")


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def type_folder(article_type: str | None, valid_types=None) -> str:
    """Article type -> filesystem-safe folder name.

    ``"Case Report/Series"`` -> ``"Case_Report_Series"``. Blank, missing,
    ``no_abstract`` and ``parse_error`` types route to :data:`UNCLASSIFIED`
    rather than to a folder literally named after a non-type — as does any
    value outside *valid_types* (the config's ``article_types``) when given, so
    a model's invented label can never mint a new bucket.
    """
    raw = (article_type or "").strip()
    if raw.lower() in _EMPTY_TYPES:
        return UNCLASSIFIED
    if valid_types and raw not in {str(t).strip() for t in valid_types}:
        return UNCLASSIFIED
    # The long-standing type-folder transform. A leading "_" is
    # reserved for the store's own buckets, so an article type can never
    # collide with (or masquerade as) _incoming/_unclassified.
    safe = raw.replace("/", "_").replace(" ", "_").strip(".")
    if not safe or safe.startswith("_"):
        return UNCLASSIFIED
    return safe


def is_paper_stem(name: str) -> bool:
    """True when *name* looks like a paper folder (a DOI/PMID stem)."""
    return name.startswith(VALID_PREFIXES)


def stem_for(doi_or_stem: str) -> str:
    """THE folder name for a paper the store creates.

    Every place that names a NEW folder goes through here. Existing folders keep
    their names (the PDF and XML inside are named after them); lookups never
    depend on the name because every index keys on ``decode_artifact_stem``.
    """
    return doi_to_filename(norm_doi(doi_or_stem))


# --------------------------------------------------------------------------
# Locating
# --------------------------------------------------------------------------

def papers_root(project_dir: Path, override: str | Path | None = None,
                config: dict | None = None) -> Path:
    """The store root for a project. Does not create it.

    Precedence mirrors what the pipeline has always used for the PDF directory:
    an explicit *override* (a ``--papers-dir`` flag), then the config's
    ``dirs.base_pdf_dir`` (which the studio lets a user point anywhere), then
    ``<project_dir>/papers``.
    """
    if override is not None and str(override).strip():
        return Path(str(override).strip())
    if config:
        configured = str((config.get("dirs") or {}).get("base_pdf_dir") or "").strip()
        if configured:
            return Path(configured).expanduser()
    from ber_core.storage import paper_root
    return paper_root(project_dir)


def _root(project_dir: Path, root, config) -> Path:
    """Every entry point resolves its store the same way: root > config > record."""
    return papers_root(project_dir, root, config=config)


def remember_root(project_dir: Path, config: dict | None) -> None:
    """Record a config's ``dirs.base_pdf_dir`` in ``<project>/.paper-store.json``.

    The studio writes that record before every step; the CLI now does too, so a
    caller that only knows the project directory (the server's evidence build,
    ``relink_buckets`` from an old call site) resolves the same store instead
    of an empty ``<project>/papers``. Skipped inside a managed run, where the
    server owns the record (it resolves per-owner directories the config
    cannot express).
    """
    if os.environ.get("BER_MANAGED_RUN") == "1" or not config:
        return
    configured = str((config.get("dirs") or {}).get("base_pdf_dir") or "").strip()
    if not configured:
        return
    resolved = str(Path(configured).expanduser().resolve())
    record = Path(project_dir) / ".paper-store.json"
    try:
        if json.loads(record.read_text(encoding="utf-8")).get("root") == resolved:
            return
    except (OSError, ValueError, AttributeError):
        pass
    from ber_core.storage import atomic_text
    atomic_text(record, json.dumps({"root": resolved}))


@contextlib.contextmanager
def store_lock(root: Path, *, exclusive: bool, timeout: float | None = None):
    """Coordinate the store's writers.

    Writers that put files INTO paper folders (download, convert) hold a SHARED
    lock; :func:`refile`, which MOVES folders, needs it EXCLUSIVE — moving a
    folder fetchpdf or pdf4llm is writing into loses or misplaces that output.
    The studio runs those on a separate queue, so they genuinely overlap.

    Yields True when the lock was taken, False when *timeout* expired (the
    caller decides; refile skips and retries next run). No-op where ``fcntl``
    is unavailable.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield True
        return
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / LOCK_NAME, "a", encoding="utf-8") as handle:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.5)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _richness(folder: Path) -> int:
    """How much usable full text a paper folder holds (for collision choice)."""
    score = 0
    if (folder / "body.md").exists():
        score += 4
    if (folder / f"{folder.name}.xml").exists() or any(folder.glob("*.xml")):
        score += 2
    if any(folder.glob("*.pdf")):
        score += 1
    return score


def build_index(project_dir: Path, legacy: bool = True,
                root: Path | None = None, *, config: dict | None = None,
                warn: bool = True) -> dict[str, Path]:
    """Map normalized DOI -> the paper's folder, in ONE scan.

    Keys go through :func:`utils.decode_artifact_stem`, so a lookup is a plain
    ``norm_doi(doi)`` regardless of which encoder wrote the directory name.

    ``_incoming/`` is download STAGING, not a home: it is skipped. When one DOI
    has folders in two type buckets (a refile interrupted mid-move, or a folder
    created under a second name), the one holding usable text wins —
    ``body.md`` over XML over a bare PDF — and the collision is reported;
    iteration order used to decide, so the evidence row could point at an empty
    folder while the converted one sat next door.

    With *legacy* true (the default) a project that has no ``papers/`` tree —
    or has one that is missing a paper — also resolves through the old
    ``converted/<stem>/`` folders, so an un-migrated project still works.
    """
    index: dict[str, Path] = {}
    collisions = 0

    root = _root(project_dir, root, config)
    if root.is_dir():
        for bucket in sorted(root.iterdir()):
            if not bucket.is_dir() or bucket.name == INCOMING:
                continue
            for child in bucket.iterdir():
                if not (child.is_dir() and is_paper_stem(child.name)):
                    continue
                key = decode_artifact_stem(child.name)
                previous = index.get(key)
                if previous is not None:
                    collisions += 1
                    if _richness(child) <= _richness(previous):
                        continue
                index[key] = child
    if collisions and warn:
        print(f"  WARN: {collisions} paper(s) have folders in more than one type "
              f"bucket under {root}; using the one with full text. A refile "
              f"(steps/classify.py quick, or scripts/maintenance/migrate_to_paper_store.py) merges them.",
              file=sys.stderr, flush=True)

    if legacy:
        # Old layout: converted/<stem>/. Registered only for stems the new tree
        # does not already own, so a half-migrated project prefers papers/.
        converted = Path(project_dir) / "converted"
        if converted.is_dir():
            for child in converted.iterdir():
                if not child.is_dir() or not is_paper_stem(child.name):
                    continue
                index.setdefault(decode_artifact_stem(child.name), child)

    return index


def paper_dir(project_dir: Path, doi_or_stem: str,
              index: dict[str, Path] | None = None,
              root: Path | None = None, *, config: dict | None = None) -> Path | None:
    """The existing folder for one paper, or None if it has none yet.

    Pass a prebuilt *index* when resolving many papers — otherwise this rescans
    the tree per call.
    """
    key = norm_doi(doi_or_stem)
    if not key:
        return None
    idx = index if index is not None else build_index(project_dir, root=root,
                                                      config=config)
    return idx.get(key)


def ensure_paper_dir(project_dir: Path, doi_or_stem: str,
                     article_type: str | None = None,
                     index: dict[str, Path] | None = None,
                     root: Path | None = None, *, config: dict | None = None) -> Path:
    """The paper's folder, created under its type bucket if it doesn't exist.

    An EXISTING folder is returned as-is even when *article_type* disagrees with
    the bucket it sits in — moving papers between buckets is :func:`refile`'s
    job, and doing it here would make every incidental read a mutation.
    """
    found = paper_dir(project_dir, doi_or_stem, index=index, root=root, config=config)
    if found is not None:
        return found
    dest = _root(project_dir, root, config) / type_folder(article_type) / stem_for(doi_or_stem)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def incoming_dir(project_dir: Path, root: Path | None = None, *,
                 config: dict | None = None) -> Path:
    """The download staging directory, created."""
    dest = _root(project_dir, root, config) / INCOMING
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def iter_papers(project_dir: Path, root: Path | None = None, *,
                config: dict | None = None) -> list[PaperRef]:
    """Every paper in the store as a layout-agnostic :class:`utils.PaperRef`.

    Delegates to :func:`utils.discover_papers`, whose two-level pass understands
    ``papers/<Type>/<stem>/``. Falls back to the legacy ``converted/`` dir when
    the store does not exist yet.
    """
    resolved = _root(project_dir, root, config)
    if resolved.is_dir():
        return discover_papers(resolved)
    return discover_papers(Path(project_dir) / "converted")


def legacy_flat_artifacts(pdfs_dir: Path) -> dict[str, list[Path]]:
    """Normalized DOI -> artifact files in a FLAT legacy ``pdfs/`` dump.

    Only used by the migration and by the downloader's already-have scan; the
    new store never writes flat files. Run-level reports (``missing_pdfs.html``,
    ``failed_dois.csv``) have no paper stem and are skipped.
    """
    out: dict[str, list[Path]] = {}
    if not Path(pdfs_dir).is_dir():
        return out
    for path in Path(pdfs_dir).iterdir():
        if not path.is_file():
            continue
        stem = strip_artifact_suffix(path.name)
        if not is_paper_stem(stem):
            continue
        out.setdefault(decode_artifact_stem(stem), []).append(path)
    return out


# --------------------------------------------------------------------------
# Filing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Move:
    """One planned or performed relocation."""
    stem: str
    src: Path
    dst: Path
    reason: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.stem}: {self.src.parent.name}/ -> {self.dst.parent.name}/ ({self.reason})"


def _move_artifact(src: Path, dst: Path) -> None:
    """Keep same-device hardlinks; copy only across filesystem boundaries."""
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def _same_content(left: Path, right: Path) -> bool:
    """Compare bytes without caching stat signatures or loading whole PDFs."""
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                chunk = a.read(1024 * 1024)
                if chunk != b.read(1024 * 1024):
                    return False
                if not chunk:
                    return True
    except OSError:
        return False  # Unable to prove equality: preserve both artifacts.


def _is_replaceable(path: Path) -> bool:
    return path.name.endswith(_REPLACEABLE_SUFFIXES)


def _record_time(path: Path) -> float:
    """When a fetch record was written: its ``completed_at``, else its mtime."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = data.get("completed_at") or data.get("started_at")
        if stamp:
            from datetime import datetime
            return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def merge_artifact(src: Path, dst: Path) -> list[Path]:
    """Move one artifact, preserving different collisions and nested content."""
    if src.absolute() == dst.absolute():
        return []
    if not dst.exists() and not dst.is_symlink():
        _move_artifact(src, dst)
        return []
    # Never follow colliding symlinks into another paper's artifacts.
    if not src.is_symlink() and not dst.is_symlink():
        if src.is_dir() and dst.is_dir():
            return merge_into(src, dst)
        if src.is_file() and dst.is_file() and _same_content(src, dst):
            src.unlink()
            return []
        if src.is_file() and dst.is_file() and _is_replaceable(dst):
            # A per-attempt fetch record: the newer attempt supersedes the
            # older; neither is data anyone would want two copies of.
            if _record_time(src) > _record_time(dst):
                _move_artifact(src, dst)
            else:
                src.unlink()
            return []
    spare = _free_name(dst.parent, f"{dst.name}.dup")
    _move_artifact(src, spare)
    return [spare]


def merge_into(src_dir: Path, dst_dir: Path) -> list[Path]:
    """Move every file from *src_dir* into *dst_dir*, then drop *src_dir*.

    Used when a paper folder already exists at the destination — a re-download
    into ``_incoming/`` for a paper we already have, say. Existing destination
    files WIN: the store's contents are the pipeline's resumable state, and
    silently replacing a converted ``body.md`` with a fresh empty folder's
    version would destroy work.

    On a name collision the loser is discarded ONLY when it is byte-identical
    to the file already filed — a true duplicate, where deleting
    costs nothing. A collision between DIFFERENT content is kept as
    ``<name>.dup``: two distinct PDFs for one DOI is a real thing (a
    re-download that resolved to another copy), and destroying one silently
    loses data no one asked us to throw away.

    Returns the paths kept as ``.dup`` so callers can report them.
    """
    if src_dir.resolve() == dst_dir.resolve():
        return []
    dst_dir.mkdir(parents=True, exist_ok=True)
    kept_duplicates: list[Path] = []
    for item in list(src_dir.iterdir()):
        kept_duplicates.extend(merge_artifact(item, dst_dir / item.name))
    try:
        src_dir.rmdir()
    except OSError:
        # Non-empty (a name collision we could not resolve) — leave it for a
        # human rather than deleting recursively.
        pass
    return kept_duplicates


def _free_name(directory: Path, name: str) -> Path:
    """``directory/name``, suffixed with a counter until it does not exist."""
    candidate = directory / name
    n = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{name}{n}"
        n += 1
    return candidate


def _place(src_dir: Path, dst_dir: Path) -> None:
    """Relocate a paper folder, merging when the destination already exists."""
    if src_dir.resolve() == dst_dir.resolve():
        return
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    if dst_dir.exists():
        merge_into(src_dir, dst_dir)
    else:
        # rename() within a filesystem preserves hardlinks and is atomic; the
        # shutil fallback covers a cross-device store.
        _move_artifact(src_dir, dst_dir)


def refile(project_dir: Path, types: dict[str, str],
           execute: bool = False, root: Path | None = None, *,
           config: dict | None = None, valid_types=None,
           lock_timeout: float | None = 60.0) -> list[Move]:
    """File every paper into the bucket its CURRENT article type names.

    *types* maps DOI (any form — keys are normalized here) to ``article_type``,
    as read from ``classifications.csv``. Papers in :data:`INCOMING` are filed;
    papers already filed are moved when their type changed; papers with no entry
    in *types* land in :data:`UNCLASSIFIED`, as do types outside *valid_types*
    (default: the config's ``article_types``).

    A paper whose DOI already has a folder elsewhere — under another name, or
    the same name in another bucket — is MERGED into that folder rather than
    filed beside it, so one DOI never ends up with two homes.

    Executing takes the store lock exclusively (see :func:`store_lock`); when a
    download or conversion holds it past *lock_timeout* seconds, nothing moves
    and an empty list is returned — refile is idempotent and runs again at the
    end of the next screen or download.

    Idempotent — a second call with the same inputs returns an empty list. Set
    *execute* to perform the moves; the default only plans them.
    """
    root = _root(project_dir, root, config)
    if not root.is_dir():
        return []
    if valid_types is None and config:
        valid_types = config.get("article_types") or None

    if not execute:
        return _plan_refile(root, types, valid_types)
    with store_lock(root, exclusive=True, timeout=lock_timeout) as locked:
        if not locked:
            print(f"  WARN: the paper store at {root} is busy (a download or "
                  f"conversion is writing into it); refile skipped, it runs "
                  f"again next time.", file=sys.stderr, flush=True)
            return []
        moves = _plan_refile(root, types, valid_types)
        for mv in moves:
            _place(mv.src, mv.dst)
        _prune_empty_buckets(root)
    return moves


def _plan_refile(root: Path, types: dict[str, str], valid_types) -> list[Move]:
    """Plan the moves that give every DOI exactly one folder, in its bucket."""
    wanted = {norm_doi(k): v for k, v in (types or {}).items() if norm_doi(k)}
    moves: list[Move] = []

    # Every filed folder, grouped by paper. Usually one per key; more when a
    # refile was interrupted or a folder was created under a second name.
    folders: dict[str, list[Path]] = {}
    for bucket in sorted(root.iterdir()):
        if not bucket.is_dir() or bucket.name == INCOMING:
            continue
        for child in sorted(bucket.iterdir()):
            if child.is_dir() and is_paper_stem(child.name):
                folders.setdefault(decode_artifact_stem(child.name), []).append(child)

    homes: dict[str, Path] = {}
    for key, group in folders.items():
        richest = max(group, key=_richness)  # max() keeps the first on ties
        if key in wanted:
            target = type_folder(wanted[key], valid_types)
        else:
            # No classification row: the paper keeps its bucket (the CSV is
            # often a subset of the corpus, and demoting those papers to
            # _unclassified on every refile would thrash the tree) — unless
            # that bucket is not a type at all, like a stray ``parse_error/``.
            bucket = richest.parent.name
            target = UNCLASSIFIED if bucket.lower() in _EMPTY_TYPES else bucket
        in_target = [f for f in group if f.parent.name == target]
        home = (max(in_target, key=_richness) if in_target
                else root / target / richest.name)
        homes[key] = home
        # The richest folder moves first so the others merge INTO it.
        for folder in sorted(group, key=lambda f: f != richest):
            if folder == home:
                continue
            moved = folder.parent.name != target
            moves.append(Move(folder.name, folder, home,
                              "type changed" if moved else "merged duplicate folder"))

    staging = root / INCOMING
    if staging.is_dir():
        for child in sorted(staging.iterdir()):
            if not child.is_dir() or not is_paper_stem(child.name):
                continue
            key = decode_artifact_stem(child.name)
            home = homes.get(key)
            if home is None:
                target = (type_folder(wanted[key], valid_types) if key in wanted
                          else UNCLASSIFIED)
                home = root / target / child.name
                homes[key] = home
            moves.append(Move(child.name, child, home, "filed from staging"))

    return moves


def _prune_empty_buckets(root: Path) -> None:
    """Remove type folders left empty by a refile. Keeps the tree readable."""
    for bucket in root.iterdir():
        if not bucket.is_dir() or bucket.name in RESERVED_DIRS:
            continue
        try:
            bucket.rmdir()  # only succeeds when empty
        except OSError:
            pass


def relink_buckets(project_dir: Path, index: dict[str, Path] | None = None, *,
                   config: dict | None = None, root: Path | None = None) -> int:
    """Re-point ``eligible_*/<stem>`` symlinks at each paper's current folder.

    The design buckets are flat directories of symlinks into the store, so a
    refile (or the migration) leaves them dangling. Returns the number of links
    rewritten. Only SYMLINKS are touched — a real directory in a bucket is a
    user's own file.

    Pass *config* (or *root*): resolving the store without it ignored
    ``dirs.base_pdf_dir``, found nothing, and left every link dangling — and
    pass 2 then silently dropped those papers.
    """
    project_dir = Path(project_dir)
    idx = index if index is not None else build_index(project_dir, root=root,
                                                      config=config)
    fixed = 0
    for bucket_dir in sorted(project_dir.glob("eligible_*")):
        if not bucket_dir.is_dir():
            continue
        for link in sorted(bucket_dir.iterdir()):
            if not link.is_symlink():
                continue
            dest = idx.get(decode_artifact_stem(link.name))
            if dest is None:
                continue
            rel = os.path.relpath(dest, bucket_dir)
            if os.readlink(link) == rel:
                continue
            link.unlink()
            link.symlink_to(rel, target_is_directory=True)
            fixed += 1
    return fixed


def bucket_link_target(paper_folder: Path, bucket_dir: Path) -> Path:
    """The relative symlink target for *paper_folder* from inside *bucket_dir*.

    Relative so the links survive the project directory being moved — the
    property the old hardcoded ``../converted/<stem>`` had, now that the depth
    varies with the type folder.
    """
    return Path(os.path.relpath(paper_folder, bucket_dir))


def discover_project_papers(project_dir, *, root=None, config=None):
    """Merge modern and legacy layouts, preferring usable text over PDF stubs."""
    found = {}
    for directory in (_root(project_dir, root, config), Path(project_dir) / "converted"):
        for ref in discover_papers(directory):
            key = norm_doi(ref.paper_id)
            previous = found.get(key)
            if previous is None or (not (previous.md_path or previous.xml_path) and (ref.md_path or ref.xml_path)):
                found[key] = ref
    return list(found.values())
