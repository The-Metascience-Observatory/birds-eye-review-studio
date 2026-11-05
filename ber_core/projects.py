"""Review projects: paths from a config, and pipeline state derived from disk."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = Path(os.environ.get("BER_PROJECTS_DIR") or (REPO_ROOT / "projects"))


from ber_core.identity import slugify  # noqa: E402  (re-exported)

__all__ = ["REPO_ROOT", "PROJECTS_DIR", "Project", "StepState", "slugify",
           "derive_pipeline_state"]


@dataclass
class Project:
    slug: str
    config_path: Path
    config: dict
    project_dir: Path

    @property
    def topic_name(self) -> str:
        return (self.config.get("topic") or {}).get("name", self.slug)

    @property
    def data_dir(self) -> Path:
        return self.project_dir / "data"

    @property
    def abstracts_dir(self) -> Path:
        return self.project_dir / "abstracts"

    @property
    def progress_dir(self) -> Path:
        return self.project_dir / "progress"

    @property
    def base_pdf_dir(self) -> Path | None:
        d = (self.config.get("dirs") or {}).get("base_pdf_dir")
        return Path(d) if d else None

    @property
    def eligible_trials_dir(self) -> Path | None:
        d = (self.config.get("dirs") or {}).get("eligible_trials")
        return Path(d) if d else None

    @property
    def screening_dirs(self) -> list[Path]:
        ds = (self.config.get("dirs") or {}).get("screening_dirs") or []
        return [Path(d) for d in ds]


# --- Pipeline state derivation -----------------------------------------------

@dataclass
class StepState:
    number: int
    name: str
    script: str
    status: str  # "done" | "partial" | "pending" | "blocked"
    detail: str
    output_paths: list[Path] = field(default_factory=list)
    last_modified: str | None = None
    count: int | None = None


def _csv_count(path: Path) -> int:
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            return max(0, sum(1 for _ in csv.reader(f)) - 1)  # minus header
    except Exception:
        return 0


def _count_names_endingwith(path: Path, suffix: str) -> int:
    """Count directory entries whose name ends with *suffix* (no per-entry stat)."""
    try:
        with os.scandir(path) as it:
            return sum(1 for e in it if e.name.endswith(suffix))
    except OSError:
        return 0


def _jsonl_count(path: Path) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _find_csv(project: Project, names: list[str]) -> Path | None:
    """The first of *names* in the project's data/ (then project) directory.

    Never the repo root: a stray file there used to be reported as THIS
    project's output — one review's CSV leaking into another's state.
    """
    for loc in (project.data_dir, project.project_dir):
        for name in names:
            p = loc / name
            if p.exists():
                return p
    return None


def _find_jsonl(project: Project, names: list[str]) -> Path | None:
    return _find_csv(project, names)  # same logic


def _artifact_state(spec: str, allow_empty: bool) -> tuple[bool, int | None, Path]:
    """(produced?, count, path) for one ``Step.produces`` entry.

    ``<dir>/`` means "a non-empty directory"; a file counts when it exists and,
    unless the step allows empty outputs, is non-empty.
    """
    if spec.endswith("/"):
        path = Path(spec.rstrip("/"))
        try:
            with os.scandir(path) as it:
                n = sum(1 for _ in it)
        except OSError:
            return False, None, path
        if path.name == "abstracts":
            n = _count_names_endingwith(path, "_abstract.md")
        return n > 0, n, path
    path = Path(spec)
    if not path.is_file():
        return False, None, path
    if path.suffix == ".csv":
        n = _csv_count(path)
    elif path.suffix == ".jsonl":
        n = _jsonl_count(path)
    else:
        n = None
    produced = allow_empty or path.stat().st_size > 0
    return produced, n, path


def _detail_for(path: Path, n: int | None) -> str:
    if n is None:
        return path.name
    unit = {".csv": "rows", ".jsonl": "records"}.get(path.suffix, "files")
    return f"{path.name}: {n:,} {unit}"


def derive_pipeline_state(project: Project) -> list[StepState]:
    """One display row per pipeline SCRIPT, in canonical execution order.

    Derived from :func:`pipeline.canonical_steps` and each step's
    ``Step.produces`` — the same contract ``run_pipeline.py`` verifies after a
    step runs — so this can never disagree with what the runner calls success.
    It replaced a hand-maintained list that had drifted: it required a
    ``relevance.csv`` the abstract screen no longer writes (pinning resume at
    step 3 forever), looked for QC outputs under names qc_report never used, reported
    an archived visualize step, judged "exported" by a sibling website repo, and
    fell back to the REPO ROOT for any file it could not find in the project.

    Rows are per script because the server's resume logic and stage details key
    on the script; a script backing several steps (classify quick + screen,
    the two pass-2 designs) is ``done`` only when every one of its steps is.
    Steps that declare no outputs are not shown: the filesystem cannot say
    whether they ran.
    """
    from ber_core.pipeline import canonical_steps
    from ber_core.execution import input_bucket_empty

    cfg = project.config or {}
    include_observational = bool(cfg.get("include_observational", True))
    if (cfg.get("meta_analysis") or {}).get("designs") == "rct_only":
        include_observational = False  # same rule as the server's step list
    steps = canonical_steps(
        project,
        include_observational=include_observational,
        generate_meta_analysis=bool(cfg.get("generate_meta_analysis", False)),
    )

    rows: dict[str, StepState] = {}
    per_script: dict[str, list[str]] = {}
    for step in steps:
        if not step.produces:
            continue
        if input_bucket_empty(step):
            status, details, outputs, count = "done", ["no papers staged for this design"], [], 0
        else:
            results = [_artifact_state(p, step.allow_empty_outputs) for p in step.produces]
            produced = [r for r in results if r[0]]
            if len(produced) == len(results):
                status = "done"
            elif produced:
                status = "partial"
            else:
                status = "pending"
            outputs = [r[2] for r in results]
            details = [_detail_for(r[2], r[1]) for r in produced] or [
                f"{', '.join(r[2].name for r in results)} not yet generated"]
            count = next((r[1] for r in produced if r[1] is not None), None)
        per_script.setdefault(step.script, []).append(status)
        row = rows.get(step.script)
        if row is None:
            rows[step.script] = StepState(
                number=step.num, name=step.label, script=step.script,
                status=status, detail=" | ".join(details),
                output_paths=list(outputs),
                last_modified=max((m for m in map(_mtime, outputs) if m), default=None),
                count=count,
            )
        else:
            row.name = f"{row.name} + {step.label}"
            row.detail = " | ".join(x for x in (row.detail, *details) if x)
            row.output_paths.extend(outputs)
            mtimes = [m for m in (row.last_modified, *map(_mtime, outputs)) if m]
            row.last_modified = max(mtimes, default=None)
            if row.count is None:
                row.count = count

    for script, statuses in per_script.items():
        if all(s == "done" for s in statuses):
            rows[script].status = "done"
        elif any(s in ("done", "partial") for s in statuses):
            rows[script].status = "partial"
        else:
            rows[script].status = "pending"
    return list(rows.values())
