"""Versioned bundle inputs, complete snapshots, and content-bound approval."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

INPUT_MANIFEST = "bundle.inputs.json"
INPUTS = (
    "trial_extractions_pass2.jsonl", "observational_extractions.jsonl",
    "trial_extractions.jsonl", "trial_screening.csv", "classifications.csv",
    "classifications_enriched.csv", "relevance.csv", "search_dois.csv", "search_raw.csv",
    "user_inclusions.json", "user_exclusions.json", "duplicate_dois.json",
    "delivery_exclusions.json", "related_publications.json",
    "intervention_canonical_map.json", "intervention_category_map.json",
    "intervention_indication_map.json", "agent_canonical_map.json", "spray_types.csv",
    "clinical_staging_manifest.json", "trial_arm_splits.json",
    "mechanism_of_action.json", "trial_verdicts.csv", "publication_metadata.json",
    "publication_audit.json", "doi_metadata.json",
)


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def source_fingerprint(data_dir, config=None):
    from .scientific import RULES_VERSION
    cfg = dict(config or {})
    layout = cfg.pop("dashboard", None) or {}
    # Presentation-only edits can rebuild the manifest without re-exporting.
    cfg["dashboard_outcome_types"] = layout.get("outcome_types")
    return {"version": 2, "scientific_rules_version": RULES_VERSION,
            "config": hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest(),
            "files": {name: file_digest(Path(data_dir) / name) if (Path(data_dir) / name).is_file() else None
                      for name in INPUTS}}


def tree_digest(directory):
    h = hashlib.sha256()
    if directory is not None and Path(directory).is_dir():
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file():
                h.update(str(path.relative_to(directory)).encode() + b"\0")
                h.update(file_digest(path).encode() + b"\0")
    return h.hexdigest()


def approval_digest(candidate, live, slug):
    return hashlib.sha256((slug + "\0" + tree_digest(candidate) + "\0" + tree_digest(live)).encode()).hexdigest()


@contextmanager
def directory_lock(target):
    """Refuse conflicting writers; never block an async server's event loop."""
    import fcntl
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ("." + target.name + ".lock")).open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another build or publish is using this destination; retry when it finishes.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def replace_directory(staging, target):
    """Install a complete snapshot; restore the previous one if install fails."""
    target, staging = Path(target), Path(staging)
    # Linux exchange is atomic for readers too: the live path never disappears
    # between two renames. Keep the portable rollback path for other platforms.
    if target.exists() and os.name == "posix":
        import ctypes
        import errno
        libc = ctypes.CDLL(None, use_errno=True)
        exchange = getattr(libc, "renameat2", None)
        if exchange is not None:
            result = exchange(-100, os.fsencode(staging), -100, os.fsencode(target), 2)
            if result == 0:
                shutil.rmtree(staging)  # the old snapshot after the exchange
                return
            error = ctypes.get_errno()
            if error not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
                raise OSError(error, os.strerror(error), str(target))
    backup = target.with_name("." + target.name + ".previous." + uuid.uuid4().hex)
    moved = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved = True
        os.replace(staging, target)
    except BaseException:
        if moved and not target.exists():
            os.replace(backup, target)
        raise
    else:
        if moved:
            shutil.rmtree(backup)


@contextmanager
def build_snapshot(target):
    target = Path(target)
    with directory_lock(target):
        staging = Path(tempfile.mkdtemp(prefix="." + target.name + ".building.", dir=target.parent))
        try:
            yield staging
            replace_directory(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
