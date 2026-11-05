"""Explicit per-review paths, with a non-secret record of the acquired store."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile


def atomic_text(path, text):
    """Write *text* to *path* so readers see the old file or the new one, never half.

    The temp file is fsynced before the rename (a crash after ``replace`` must
    not leave an empty file) and given the replaced file's permissions — a
    NamedTemporaryFile is 0600, which would otherwise make every rewritten
    file owner-only.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8",
                                     delete=False, prefix=f".{path.name}.") as f:
        temporary = Path(f.name)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path, data, *, indent=2):
    """JSON counterpart of :func:`atomic_text`."""
    atomic_text(path, json.dumps(data, indent=indent, ensure_ascii=False))


def paper_root(project_dir, configured=None):
    if configured and str(configured).strip():
        return Path(configured)
    path = Path(project_dir) / ".paper-store.json"
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["root"]
            if Path(value).is_absolute():
                return Path(value)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return Path(project_dir) / "papers"


@dataclass(frozen=True)
class WorkspaceContext:
    project_dir: Path
    config_path: Path
    config: dict

    @property
    def papers(self):
        return paper_root(self.project_dir, (self.config.get("dirs") or {}).get("base_pdf_dir"))

    @property
    def data(self):
        return self.project_dir / "data"

    @property
    def bundle(self):
        return self.project_dir / "dashboard"

    def remember_store(self, root):
        atomic_text(self.project_dir / ".paper-store.json", json.dumps({"root": str(Path(root).resolve())}))
