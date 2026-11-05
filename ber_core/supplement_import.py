"""Copy explicitly identified legacy supplements into the canonical paper store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from ber_core import paper_store
from ber_core.identity import join_key
from ber_core.storage import atomic_write_json


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def import_supplements(project: Path, config: dict, entries: list[dict], *, execute=False) -> list[dict]:
    """Validate the entire explicit manifest before copying any files.

    Each entry requires paper_id, source and sha256. Files are copied, never
    moved, and differing destination contents are never overwritten. The
    manifest is the source-to-paper association; filename guessing is avoided.
    """
    root = paper_store.papers_root(project, config=config)
    if not root.is_dir():
        raise ValueError("Paper store does not exist")
    with paper_store.store_lock(root, exclusive=True, timeout=30) as acquired:
        if not acquired:
            raise TimeoutError("Paper store is busy")
        index = paper_store.build_index(project, root=root, legacy=False)
        plan = []
        destinations = {}
        for entry in entries:
            key = join_key(entry["paper_id"])
            if not key or key not in index:
                raise ValueError(f"Paper is not in the canonical store: {entry['paper_id']}")
            source = Path(entry["source"]).expanduser().resolve(strict=True)
            expected = entry.get("sha256")
            if not source.is_file() or not expected or file_hash(source) != expected:
                raise ValueError(f"Supplement hash does not match: {source.name}")
            name = entry.get("filename", source.name)
            if not name or Path(name).name != name or name in {".", "..", "import_provenance.json"}:
                raise ValueError("Supplement filename must be a plain filename")
            folder = index[key]
            supplement_dir = folder / "supplementary"
            if supplement_dir.is_symlink():
                raise ValueError("Supplement destination must be inside its paper folder")
            dest = supplement_dir / name
            if dest.is_symlink():
                raise ValueError("Supplement destination cannot be a symlink")
            if dest in destinations and destinations[dest] != expected:
                raise ValueError(f"Conflicting manifest entries: {name}")
            destinations[dest] = expected
            if dest.exists() and file_hash(dest) != expected:
                raise ValueError(f"Refusing to replace different supplement: {name}")
            # Refuse corrupt bookkeeping rather than replacing evidence of an
            # earlier import with a fresh empty dictionary.
            for path in (folder / "provenance.json", supplement_dir / "import_provenance.json"):
                if path.exists() and not isinstance(json.loads(path.read_text()), dict):
                    raise ValueError(f"Invalid provenance: {path.name}")
            plan.append({"paper_id": key, "source": str(source), "destination": str(dest),
                         "sha256": expected, "action": "present" if dest.exists() else "copy"})
        if not execute:
            return plan
        for item in plan:
            dest = Path(item["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item["action"] == "copy":
                fd, name = tempfile.mkstemp(dir=dest.parent, prefix=".supplement-")
                os.close(fd)
                temporary = Path(name)
                try:
                    shutil.copyfile(item["source"], temporary)
                    if file_hash(temporary) != item["sha256"]:
                        raise ValueError("Supplement changed during import")
                    os.chmod(temporary, 0o644)
                    os.replace(temporary, dest)
                finally:
                    temporary.unlink(missing_ok=True)
            record = dest.parent / "import_provenance.json"
            imports = json.loads(record.read_text()) if record.exists() else {}
            imports[dest.name] = {"paper_id": item["paper_id"], "sha256": item["sha256"],
                                  "source": item["source"]}
            atomic_write_json(record, imports)
            provenance = dest.parent.parent / "provenance.json"
            data = json.loads(provenance.read_text()) if provenance.exists() else {}
            data["supplementary_files"] = sum(p.is_file() and not p.name.startswith((".", "_"))
                                                and p.name != "import_provenance.json"
                                                for p in dest.parent.rglob("*"))
            atomic_write_json(provenance, data)
        return plan
