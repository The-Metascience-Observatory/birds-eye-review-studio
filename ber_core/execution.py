"""Output contracts shared by synchronous CLI and Studio jobs."""
from pathlib import Path

#: Filesystem timestamp granularity slack (coarse mtimes on some filesystems).
_MTIME_SLACK_S = 2.0


def input_bucket_empty(step):
    args = list(step.args)
    if "--input" not in args:
        return False
    index = args.index("--input") + 1
    if index >= len(args):
        return False
    bucket = Path(args[index])
    return bucket.name.startswith("eligible_") and bucket.is_dir() and not any(bucket.iterdir())


def stale_outputs(step, started_at: float) -> list[Path]:
    """Declared outputs that exist but were NOT written since *started_at*.

    *started_at* is a ``time.time()`` taken just before the step ran. A
    directory output counts as fresh when it or any direct child changed.
    """
    stale = []
    for spec in step.produces:
        path = Path(spec.rstrip("/"))
        if not path.exists():
            continue
        newest = path.stat().st_mtime
        if spec.endswith("/") and path.is_dir():
            for child in path.iterdir():
                try:
                    newest = max(newest, child.stat().st_mtime)
                except OSError:
                    continue
        if newest + _MTIME_SLACK_S < started_at:
            stale.append(path)
    return stale


def freshness_warning(step, started_at: float | None):
    """A warning when a step succeeded without touching any declared output.

    Only a WARNING: every pipeline step is resumable, and a resumed step with
    no new work legitimately leaves its outputs as they were. But "exited 0 and
    wrote nothing" is also what a silently broken step looks like, so it is
    worth a line in the log rather than nothing.
    """
    if started_at is None or not step.produces or input_bucket_empty(step):
        return None
    stale = stale_outputs(step, started_at)
    if stale and len(stale) == len([p for p in step.produces if Path(p.rstrip("/")).exists()]):
        names = ", ".join(p.name for p in stale)
        return (f"no declared output was updated by this run ({names}); fine for a "
                f"resumed step with no new work, otherwise the step did nothing")
    return None


def verify_produces(step, started_at: float | None = None, *, require_fresh: bool = False):
    """Why *step*'s declared outputs are unacceptable, or None when they are fine.

    Existence (and non-emptiness, unless ``allow_empty_outputs``) is always
    checked. With ``require_fresh`` and a *started_at*, an output not rewritten
    since the step started is an error too — for callers that know the step
    must produce new data; the default leaves freshness to
    :func:`freshness_warning`.
    """
    if input_bucket_empty(step):
        return None
    for spec in step.produces:
        path = Path(spec.rstrip("/"))
        if not path.exists() and getattr(step, "optional_outputs", False):
            continue
        if spec.endswith("/"):
            if not path.is_dir() or (not getattr(step, "allow_empty_outputs", False) and not any(path.iterdir())):
                return f"expected non-empty directory {path}"
        elif not path.exists():
            return f"expected output {path} (missing)"
        elif path.stat().st_size == 0 and not getattr(step, "allow_empty_outputs", False):
            return f"output {path} is empty"
    if require_fresh and started_at is not None:
        stale = stale_outputs(step, started_at)
        if stale:
            return f"output {stale[0]} was not updated by this run"
    return None
