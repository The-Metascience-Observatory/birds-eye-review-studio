"""Pure, directly-testable step/pipeline execution.

This is the deterministic heart of the job engine. ``execute_step``,
``execute_pipeline`` and the background runners are plain async functions taking
``(session, job, review, ...)`` so unit tests drive them without redis or a
worker; they all share one step loop, :func:`_run_steps`. Step resolution goes
through :mod:`pipeline_bridge` (so tests can monkeypatch ``find_step`` /
``canonical_steps_for``). Each step is a subprocess ``[python, -u, script,
*args]`` run with ``cwd=REPO_ROOT`` and BER_* env carrying the review workspace;
its stdout+stderr stream into the job's log file.
"""
from __future__ import annotations

from .._time import utcnow

import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..config import settings
from ..db.models import Job, Review
from . import capability, llm_prefs, pdf_dir, pipeline_bridge, redact

REPO_ROOT = Path(settings.REPO_ROOT)

# --- Progress parsing (tqdm + "[i/N]") -------------------------------------
# tqdm renders like:  " 45%|████▌     | 225/500 [00:30<00:37,  7.42it/s]"
_TQDM_RE = re.compile(
    r"(?P<done>\d+)/(?P<total>\d+)\s*\["
    r"(?P<elapsed>[\d:]+)<(?P<eta>[\d:?]+),\s*"
    r"(?P<rate>[\d.]+)(?P<unit>s|it)/(?P<per>it|s)\]"
)
# Custom "[123/500]" counters several pipeline scripts print.
_FRAC_RE = re.compile(r"\[(?P<done>\d+)/(?P<total>\d+)\]")
# utils.log_progress lines (classify etc.). The ETA is seconds-aware —
#   "ETA: 45s" / "ETA: 12m30s" / "ETA: 1h02m" / "ETA: ?" (no rate yet) —
# and legacy logs still carry the old XhYYm-only form; parse all of them.
_LOGPROG_RE = re.compile(
    r"Progress:\s*(?P<done>\d+)/(?P<total>\d+)"
    r"(?:.*?ETA:\s*(?:\?|(?:(?P<eh>\d+)h)?(?:(?P<em>\d+)m)?(?:(?P<es>\d+)s)?))?"
)


def _clock_to_s(s: str) -> int | None:
    if not s or "?" in s:
        return None
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]


def parse_progress(text: str) -> dict | None:
    """Extract {done,total,elapsed_s?,eta_s?,rate?} from a tqdm or [i/N] line."""
    m = _TQDM_RE.search(text)
    if m:
        rate = float(m["rate"])
        if m["unit"] == "s" and m["per"] == "it" and rate:
            rate = 1.0 / rate  # s/it -> it/s
        return {
            "done": int(m["done"]),
            "total": int(m["total"]),
            "elapsed_s": _clock_to_s(m["elapsed"]),
            "eta_s": _clock_to_s(m["eta"]),
            "rate": round(rate, 3),
        }
    m = _LOGPROG_RE.search(text)
    if m:
        out: dict = {"done": int(m["done"]), "total": int(m["total"])}
        # eta_s only when at least one h/m/s component matched — "ETA: ?" (no
        # rate yet) and a missing ETA both leave the key out, so the UI can
        # distinguish "unknown" from a real 0.
        if any(m[g] is not None for g in ("eh", "em", "es")):
            out["eta_s"] = (
                int(m["eh"] or 0) * 3600
                + int(m["em"] or 0) * 60
                + int(m["es"] or 0)
            )
        return out
    m = _FRAC_RE.search(text)
    if m:
        return {"done": int(m["done"]), "total": int(m["total"])}
    return None


def sidecar_path(workspace: Path, job_id: str) -> Path:
    return workspace / "_jobs" / f"{job_id}.progress.json"


def cancel_flag_path(workspace: Path, job_id: str) -> Path:
    """The file whose existence tells a running step "the user cancelled".

    Written by the cancel endpoint (a different PROCESS from the arq worker, so
    a DB flag alone can't interrupt the worker's blocking subprocess read — but
    a file can be polled from a watcher thread). One per job id, like the log
    and progress sidecar."""
    return workspace / "_jobs" / f"{job_id}.cancel"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """SIGTERM the step's whole process group (the script + its claude CLI
    children — killing just the parent would orphan in-flight LLM calls that
    keep billing), escalating to SIGKILL after a grace period."""
    import signal

    pgid = proc.pid  # every step is spawned with start_new_session=True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _write_sidecar(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _clear_sidecar(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def pipeline_python() -> str:
    """Interpreter for running pipeline SCRIPTS (needs the pipeline's deps, which
    the lean server venv lacks). Configurable via settings.PIPELINE_PYTHON; the
    default is the base interpreter this venv was created from (has the deps)."""
    if settings.PIPELINE_PYTHON:
        return settings.PIPELINE_PYTHON
    candidate = Path(sys.base_prefix) / "bin" / "python3"
    if candidate.exists() and str(candidate) != sys.executable:
        return str(candidate)
    return sys.executable


def build_step_env(
    workspace: Path, provider: str, creds: dict[str, str] | None = None
) -> dict[str, str]:
    """Multi-tenant subprocess env: target THIS review's workspace.

    Scripts write to ``<workspace>/projects/<slug>/…`` and route LLM calls
    through ``BER_LLM_PROVIDER``.  ``BER_PROMPTS_DIR`` is set only when the
    workspace ships its own prompts dir.

    CREDENTIALS: this starts from a copy of the server's whole environment —
    which ``dev.sh`` has loaded the repo-root ``.env.local`` into — so it must
    CLEAR every credential variable before setting the ones this provider needs.
    Claude Code resolves its credential by a fixed precedence, and an
    ``ANTHROPIC_API_KEY`` inherited from that file would outrank a user's own
    ``CLAUDE_CODE_OAUTH_TOKEN`` and silently bill the operator instead. Clearing
    also stops one tenant's credential reaching the next tenant's step. The
    desired end state comes from ``llm_prefs.credential_env`` so this and the
    in-process context manager cannot disagree.
    """
    env = dict(os.environ)
    env["BER_MANAGED_RUN"] = "1"
    env["BER_PROJECTS_DIR"] = str(workspace / "projects")
    env["BER_LLM_PROVIDER"] = provider or llm_prefs.DEFAULT_PROVIDER
    for var, value in llm_prefs.credential_env(creds).items():
        if value is None:
            env.pop(var, None)
        else:
            env[var] = value
    # Local SciSciNet tier (optional): the step scripts read these to serve
    # metadata/abstracts from disk instead of the APIs. Absent => pure API path.
    sciscinet_dir = settings.SCISCINET_DIR or os.environ.get("BER_SCISCINET_DIR", "")
    if sciscinet_dir:
        env["BER_SCISCINET_DIR"] = str(sciscinet_dir)
        if settings.SCISCINET_WRITE or os.environ.get("BER_SCISCINET_WRITE"):
            env["BER_SCISCINET_WRITE"] = "1"
    prompts = workspace / "prompts"
    if prompts.is_dir():
        env["BER_PROMPTS_DIR"] = str(prompts)
    return env


from ber_core.execution import verify_produces  # noqa: E402


#: Scripts whose ``--workers`` fan-out is memory/CPU-bound enough to need a
#: machine-aware ceiling, mapped to the capability function that supplies it.
#: Each worker in these scripts spawns its own `claude` CLI subprocess (or loads
#: an ML extraction stack), so an unbounded count is what took down a 16 GB box.
_WORKER_CAPS = {
    "extract_pass2.py": capability.extraction_workers,
    "extract_pass1.py": capability.extraction_workers,
    "classify.py": capability.extraction_workers,
}


def cap_step_args(step) -> list[str]:
    """Bound a step's ``--workers`` to what this machine can actually take.

    Applied here — at dispatch — rather than in the stored config on purpose:
    the config is the user's stated preference and must stay stable across
    reads, while free memory on a shared host moves constantly. A step that
    asks for more workers than the box can support is lowered; one that asks
    for fewer is left alone (never raise a deliberate choice).

    Scripts read `extraction_workers` from config when no flag is passed, so an
    explicit ``--workers`` is appended when absent — it takes precedence.
    """
    args = list(step.args)
    cap_fn = _WORKER_CAPS.get(Path(step.script).name)
    if cap_fn is None:
        return args
    cap = cap_fn()
    if "--workers" in args:
        i = args.index("--workers")
        if i + 1 < len(args):
            try:
                if int(args[i + 1]) > cap:
                    args[i + 1] = str(cap)
            except ValueError:
                pass  # not a number we understand — leave it as the caller wrote it
        return args
    return args + ["--workers", str(cap)]


def _log_scrubber(workspace: Path, env: dict[str, str]):
    """A function making one log line safe for the review OWNER to read.

    Secrets always go (see ``redact``). Absolute server paths are replaced with
    placeholders on a PUBLIC_MODE instance, where the reader is not the
    operator; locally the real paths are what a developer debugging wants.
    """
    secrets = redact.secret_values(env)
    paths: dict[str, str] = {}
    if settings.PUBLIC_MODE:
        paths = {
            str(workspace): "<workspace>",
            str(settings.DATA_ROOT): "<data>",
            str(REPO_ROOT): "<repo>",
        }

    def scrub(text: str) -> str:
        return redact.scrub_paths(redact.redact(text, secrets), paths)

    return scrub


def run_subprocess(
    step,
    workspace: Path,
    provider: str,
    log_path: Path,
    *,
    creds: dict[str, str] | None = None,
    side_path: Path | None = None,
    num: int | None = None,
    label: str | None = None,
    cancel_path: Path | None = None,
) -> tuple[int, str | None, float, dict | None]:
    """Run one step, teeing stdout+stderr into ``log_path`` and (when
    ``side_path`` is given) writing live tqdm/[i/N] progress to a sidecar JSON.

    Every line is passed through a secret scrubber before it is written: the
    log is readable by the review's owner (``/jobs/{id}/log`` and the SSE
    stream), and scripts echo request URLs carrying data-source API keys.

    ``cancel_path`` (when given) is polled by a watcher thread; the moment the
    file exists, the step's whole process group is killed. This is what makes
    the UI's Stop button actually stop a RUNNING step — the job row alone can't,
    while this synchronous helper reads the child stream. The async wrapper
    runs it in a thread and forwards worker cancellation to the same flag.

    Returns ``(returncode, verify_error, duration_s, last_progress)``. The stream
    is read raw so tqdm's ``\\r`` redraws are parsed for progress but not spammed
    into the log; real ``\\n`` lines are logged verbatim (minus secrets).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = build_step_env(workspace, provider, creds)
    scrub = _log_scrubber(workspace, env)
    from ber_core.pipeline import script_path
    cmd = [pipeline_python(), "-u", script_path(step.script), *cap_step_args(step)]
    script_name = Path(step.script).name
    started = time.monotonic()
    last_progress: dict | None = None
    last_side = 0.0
    last_prog_log = 0.0
    finished = threading.Event()

    def emit(log, text: str) -> None:
        log.write((scrub(text) + "\n").encode())
        log.flush()

    with open(log_path, "ab") as log:
        emit(log, f"\n=== step {step.id}: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            # Own process group, so a cancel can kill the script AND its LLM
            # CLI children in one killpg instead of orphaning them mid-call.
            start_new_session=True,
        )
        if cancel_path is not None:

            def _watch_cancel() -> None:
                while not finished.is_set():
                    if cancel_path.exists():
                        try:
                            log.write(b"\n=== cancelled by user; stopping step\n")
                            log.flush()
                        except ValueError:
                            pass  # log already closed (step just finished)
                        _kill_process_tree(proc)
                        return
                    time.sleep(1.0)

            threading.Thread(target=_watch_cancel, daemon=True).start()
        buf = b""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                # Split on whichever comes first: \n (real line) or \r (redraw).
                i_n, i_r = buf.find(b"\n"), buf.find(b"\r")
                cands = [i for i in (i_n, i_r) if i >= 0]
                if not cands:
                    break
                idx = min(cands)
                is_newline = idx == i_n
                seg, buf = buf[:idx], buf[idx + 1:]
                text = seg.decode("utf-8", "replace")
                prog = parse_progress(text)
                now = time.monotonic()
                if prog:
                    last_progress = prog
                    if side_path and now - last_side > 0.7:
                        elapsed = prog.get("elapsed_s")
                        _write_sidecar(side_path, {
                            "num": num, "step_id": step.id, "script": script_name,
                            "label": label, "done": prog.get("done"),
                            "total": prog.get("total"),
                            "elapsed_s": elapsed if elapsed is not None else int(now - started),
                            "eta_s": prog.get("eta_s"), "rate": prog.get("rate"),
                        })
                        last_side = now
                    # Keep the terminal readable: log a progress snapshot ~every 2s.
                    if now - last_prog_log > 2.0:
                        emit(log, text)
                        last_prog_log = now
                elif is_newline:
                    emit(log, text)
                # else: a \r redraw that isn't progress — drop (transient).
        if buf:
            emit(log, buf.decode("utf-8", "replace"))
        rc = proc.wait()
        finished.set()

    duration = time.monotonic() - started
    verr = verify_produces(step) if rc == 0 else None
    return rc, verr, duration, last_progress


def _step_record(step, duration_s: float, last: dict | None, ok: bool) -> dict:
    """A finished step's timing/progress row for ``job.progress['steps']``."""
    return {
        "num": getattr(step, "num", None),
        "step_id": step.id,
        "script": Path(step.script).name,
        "label": step.label,
        "duration_s": round(duration_s, 1),
        "item_total": (last or {}).get("total"),
        "item_done": (last or {}).get("done"),
        "state": "done" if ok else "failed",
    }


_TERMINAL = frozenset({"cancelled", "succeeded", "failed"})


async def _finish_job(session, job: Job, *, error: str | None = None) -> None:
    """Commit the job's terminal state WITHOUT clobbering an intervening cancel.

    ``cancel_job`` commits ``state="cancelled"`` from the REQUEST session while
    this worker session (``expire_on_commit=False``) still holds ``"running"``
    in memory — so the runner used to overwrite the cancel with ``"failed"``
    when the killed subprocess exited -15. Re-read the row first: a cancelled
    job keeps its cancelled state (and the cancel's ended_at/error).
    """
    await session.refresh(job, ["state", "ended_at", "error"])
    if job.state in _TERMINAL:
        await session.commit()  # still flush progress/exit_code updates
        return
    if error is not None:
        job.state = "failed"
        job.error = error
    else:
        job.state = "succeeded"
    job.ended_at = utcnow()
    await session.commit()


async def _prepare_store(session, review, workspace):
    from ber_core.storage import WorkspaceContext
    from ..db.models import ReviewConfig
    from . import workspace as ws
    cfg = await session.get(ReviewConfig, review.id)
    if cfg is not None:
        ws.create_review_workspace(review.id, cfg.config_json)
    proj = pipeline_bridge.build_project(workspace)
    owner_dir = await llm_prefs.owner_pdf_dir(session, review)
    root = pdf_dir.resolve_for_workspace(workspace, proj.project_dir, owner_dir)
    WorkspaceContext(proj.project_dir, proj.config_path, proj.config).remember_store(root)


def _step_error(step, rc: int, verr: str | None, workspace: Path) -> str:
    """The ``Job.error`` for a failed step. Verification messages name the
    expected output files by absolute path, which is the server's layout —
    shown relative to the workspace instead."""
    if rc != 0:
        return f"step {step.id} exited {rc}"
    return redact.scrub_paths(verr or "", {str(workspace): "<workspace>"})


async def _run_steps(
    session,
    job: Job,
    review: Review,
    steps,
    *,
    tolerate_verify: bool = False,
    before_step=None,
) -> None:
    """THE step loop every job kind shares.

    Resolves the owner's provider + credential, marks the job running, runs
    each step as a subprocess into the job's log (live progress to the
    sidecar the ``/jobs/{id}`` endpoint overlays), records per-step timing,
    and stops at the first failure.

    ``tolerate_verify``: the background fetch/screen treat ONLY a non-zero exit
    as failure — an empty store or an empty eligible bucket is a legitimate
    "nothing new", which ``verify_produces`` would otherwise report as missing
    output. ``before_step(step)`` runs right before each step (used to
    materialize data a previous step in the same job just produced).
    """
    workspace = Path(review.workspace_path)
    provider, creds = await llm_prefs.resolve_for_run(session, review)
    log_path = workspace / "_jobs" / f"{job.id}.log"
    side = sidecar_path(workspace, job.id)
    total = len(steps)
    # Accumulate finished-step records here. IMPORTANT: only ever store COPIES
    # (list(hist)) into job.progress — storing the live list by reference and then
    # mutating it corrupts SQLAlchemy's JSON change detection (a later assignment
    # deep-equals the mutated committed value, so the UPDATE is silently skipped).
    hist: list[dict] = []
    job.state = "running"
    job.started_at = utcnow()
    job.log_path = str(log_path)
    job.progress = {
        "current": 0, "total": total,
        "step_id": None, "step_label": None, "steps": list(hist),
    }
    await session.commit()

    for idx, step in enumerate(steps, start=1):
        if before_step is not None:
            before_step(step)
        job.progress = {
            "current": idx, "total": total,
            "step_id": step.id, "step_label": step.label, "steps": list(hist),
        }
        await session.commit()

        rc, verr, dur, last = await run_subprocess_async(
            step, workspace, provider, log_path, creds=creds,
            side_path=side, num=getattr(step, "num", None), label=step.label,
            cancel_path=cancel_flag_path(workspace, job.id),
        )
        _clear_sidecar(side)
        if tolerate_verify:
            verr = None
        ok = rc == 0 and not verr
        hist.append(_step_record(step, dur, last, ok))
        job.exit_code = rc
        job.progress = {
            "current": idx, "total": total,
            "step_id": step.id, "step_label": step.label, "steps": list(hist),
        }
        if not ok:
            await _finish_job(session, job, error=_step_error(step, rc, verr, workspace))
            return
        await session.commit()

    await _finish_job(session, job)


async def execute_step(
    session, job: Job, review: Review, step_id: str
) -> None:
    """Resolve + run a single step, updating the ``Job`` row in place."""
    if job.state in _TERMINAL:  # cancelled while queued — don't run at all
        return
    if step_id in {"9_pass2_rct", "9_pass2_observational"}:
        await execute_pipeline(session, job, review, only_ids=[step_id])
        return
    workspace = Path(review.workspace_path)
    await _prepare_store(session, review, workspace)

    step = pipeline_bridge.find_step(workspace, step_id)
    if step is None:
        job.state = "failed"
        job.error = f"unknown step {step_id!r}"
        job.ended_at = utcnow()
        await session.commit()
        return
    await _run_steps(session, job, review, [step])


async def execute_pipeline(
    session,
    job: Job,
    review: Review,
    *,
    from_num: int | None = None,
    to_num: int | None = None,
    only: list[int] | None = None,
    only_ids: list[str] | None = None,
    since: str | None = None,
    fresh: bool = False,
) -> None:
    """Run a range of canonical steps into a single job log.

    Updates ``job.progress`` per step and stops on the first nonzero exit or
    verify error, marking the job failed; otherwise the job succeeds.
    """
    # Lazy import: keep step selection monkeypatchable via pipeline_bridge.
    from ber_core.pipeline import select_step_ids, select_steps

    if job.state in _TERMINAL:  # cancelled while queued — don't run at all
        return
    workspace = Path(review.workspace_path)
    await _prepare_store(session, review, workspace)

    all_steps = pipeline_bridge.canonical_steps_for(
        workspace, since=since, fresh=fresh
    )
    # select_step_ids has no empty-list footgun: an empty selection selects
    # NOTHING, which is what a caller that resolved zero steps actually means.
    # `select_steps` treats an empty `only` as "no filter" and runs everything.
    if only_ids is not None:
        steps = select_step_ids(all_steps, only_ids)
    else:
        steps = select_steps(all_steps, from_num=from_num, to_num=to_num, only=only)
    if any(s.id in {"9_pass2_rct", "9_pass2_observational"} for s in steps):
        from ber_core.pipeline import execution_steps
        steps = execution_steps(pipeline_bridge.build_project(workspace), steps,
                                only_dois_path=str(workspace / "_jobs" / f"{job.id}.only_dois.csv"),
                                convert_options=_conversion_options())

    # Before building the dashboard bundle, materialize the review's enriched
    # metadata (which the studio holds in the `paper_metadata` DB table) to the
    # local CSV the export step reads. Otherwise export_dashboard finds no
    # on-disk biblio cache and re-fetches every title over the network — a slow
    # serial grind that made the build look stuck. Best-effort: a dump failure
    # must never block the build (it just falls back to --no-metadata-fetch, so
    # the export script's own "Biblio backfill: N from local cache" line reports
    # the outcome in the job log either way).
    if any(s.id == "13_build_bundle" for s in steps):
        try:
            from . import paper_metadata as _pm

            await _pm.dump_to_disk(session, review.id, workspace)
        except Exception:  # noqa: BLE001
            pass

    def before_step(step) -> None:
        if step.id == "5_download_pdfs":
            # Screening may have run earlier in this same pipeline. Materialize
            # the current verdicts here, not when the step list was constructed.
            _write_screened_in_dois(workspace, job.id)

    await _run_steps(session, job, review, steps, before_step=before_step)


# --- Background full-text fetch (download + convert), off the canonical range --

def _conversion_options() -> list[str]:
    """Use the same resource limits for background and extraction conversion."""
    options = ["--profile", "content", "--workers", str(capability.convert_workers())]
    if capability.critical_memory():
        options += ["--mode", "fast", "--no-marker-fallback"]
    return options


def _fulltext_steps(workspace: Path, only_dois_path: str | None = None,
                    user_pdf_dir: str | None = None):
    from ber_core import pipeline
    project = pipeline_bridge.build_project(workspace)
    root = pdf_dir.resolve_for_workspace(workspace, project.project_dir, user_pdf_dir)
    return pipeline.acquisition_steps(project, papers_dir=root,
                                      only_dois_path=only_dois_path,
                                      convert_options=_conversion_options())


def _write_screened_in_dois(workspace: Path, job_id: str) -> str:
    """Write the review's screened-in DOIs to a CSV for `--only-dois`, returning
    its path. An empty set still writes a header-only allowlist.

    "Screened in" is the authoritative `build_evidence` verdict — `screen_state`
    in included/pending, which folds in `user_inclusions.json`/`user_exclusions.json`
    and the eligibility screen — so the download population matches exactly what the
    dock, PRISMA, and the eligibility screen agree on (not the raw `is_relevant`
    column, which `--relevant-only` would use). We deliberately do NOT drop
    already-fetched (`has_fulltext`) DOIs: download_pdfs is resumable and skips
    existing PDFs, so keeping them is a cheap no-op and keeps the download target
    equal to the dock's screened-in count."""
    from . import evidence as _evidence

    rows = _evidence.build_evidence(workspace).get("rows", [])
    dois = [
        r["doi"] for r in rows
        if r.get("doi") and r.get("screen_state") in ("included", "pending")
    ]
    dest = workspace / "_jobs" / f"{job_id}.only_dois.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # `doi` header + one DOI per line — the form download_pdfs's reader expects
    # (it skips a `doi`/blank first cell and norm_doi-normalizes the rest).
    import csv
    with dest.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["doi"])
        writer.writerows([doi] for doi in dois)
    return str(dest)


async def run_fulltext_fetch(session, job: Job, review: Review) -> None:
    """Download + convert full text for a review, as a background job.

    Both scripts are resumable and pre-filter what already exists, so a re-run
    only fetches what's new — safe to fire repeatedly as screening decisions
    land. A download that finds nothing new is a success, not a failure (an
    empty store is legitimate), so only a non-zero exit fails the job.

    The download is restricted to the review's SCREENED-IN set (via `--only-dois`)
    — we only fetch full text for papers the user actually screened in, not every
    accepted-article-type paper in the corpus.
    """
    if job.state in _TERMINAL:  # cancelled while queued — don't run at all
        return
    workspace = Path(review.workspace_path)
    await _prepare_store(session, review, workspace)
    only_dois_path = _write_screened_in_dois(workspace, job.id)
    user_pdf_dir = await llm_prefs.owner_pdf_dir(session, review)
    steps = _fulltext_steps(workspace, only_dois_path=only_dois_path,
                            user_pdf_dir=user_pdf_dir)
    await _run_steps(session, job, review, steps, tolerate_verify=True)


# --- Background full-text eligibility screen (pass 1 + stage), off-canonical ---

def _fulltext_screen_steps(workspace: Path):
    from ber_core import pipeline
    return pipeline.eligibility_steps(pipeline_bridge.build_project(workspace))


async def run_fulltext_screen(session, job: Job, review: Review) -> None:
    """Run the full-text eligibility screen (pass 1) + staging, as a background job.

    Only a non-zero exit is a failure (stage_eligible's empty buckets are
    legitimate). Both scripts are resumable, so firing this repeatedly as full
    texts arrive only screens what is new."""
    if job.state in _TERMINAL:  # cancelled while queued — don't run at all
        return
    workspace = Path(review.workspace_path)
    await _prepare_store(session, review, workspace)
    await _run_steps(session, job, review, _fulltext_screen_steps(workspace),
                     tolerate_verify=True)


# --- Review-document synthesis (off-canonical, user-initiated) ----------------

def _synthesis_steps(workspace: Path, params: dict):
    """The single off-canonical script the synthesis job runs.

    Options come from Job.params (the enqueue-time snapshot), not the config —
    the job record must say what it actually ran with. --force because the job
    only exists when the user explicitly asked to (re)generate.
    """
    from ber_core import pipeline  # lazy; keeps module import light

    cfg = str(workspace / "config.yaml")
    slug = pipeline_bridge.project_slug(workspace)
    doc = workspace / "projects" / slug / "data" / "review_document.md"
    args = [
        "--config", cfg,
        "--depth", str(params.get("depth") or "standard"),
        "--audience", str(params.get("audience") or "clinician"),
        "--model", str(params.get("model") or "auto"),
        "--force",
    ]
    sections = params.get("sections")
    if sections:
        args += ["--sections", ",".join(str(s) for s in sections)]
    return [
        pipeline.Step("synthesize_review", 17, "Synthesize review",
                      "synthesize_review.py", args, produces=[str(doc)]),
    ]


async def run_synthesis(session, job: Job, review: Review) -> None:
    """Generate the narrative review document, as a background job (one
    off-canonical script; it prints ``Progress: i/N`` per LLM call)."""
    if job.state in _TERMINAL:  # cancelled while queued — don't run at all
        return
    workspace = Path(review.workspace_path)
    await _prepare_store(session, review, workspace)
    await _run_steps(session, job, review,
                     _synthesis_steps(workspace, job.params or {}))


# --- Workspace exclusivity ------------------------------------------------------

class WorkspaceBusy(Exception):
    """Another job holds this review's workspace lock right now."""


@contextmanager
def workspace_lock(workspace: Path):
    """Hold the review's artifact-writer lock for a whole job — or fail FAST.

    Jobs of different kinds may be queued for one review at once (the
    background kinds are exempt from the one-job guard), and two of them
    writing the same CSVs/JSONL would corrupt each other's resume logic. The
    lock serializes them.

    It used to be taken per step with a blocking poll, INSIDE the single-slot
    LLM worker: a long PDF fetch on one review then parked the LLM slot for
    the whole fetch, stalling every other user's LLM work behind a job that was
    not even theirs. Now it is non-blocking: if it is held, raise
    :class:`WorkspaceBusy` immediately and the worker re-queues the job with a
    delay instead of occupying the slot (see ``worker.py``).
    """
    import fcntl

    lock = workspace / "_jobs" / ".execution.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceBusy(str(workspace)) from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


async def run_subprocess_async(step, workspace, provider, log_path, **kwargs):
    """Run :func:`run_subprocess` in a thread, keeping the worker's event loop
    (heartbeats, cancellation) live, and forward a worker cancellation to the
    step's cancel flag so the subprocess tree is actually killed."""
    import asyncio

    task = asyncio.create_task(asyncio.to_thread(
        run_subprocess, step, workspace, provider, log_path, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel = kwargs.get("cancel_path")
        if cancel:
            cancel.parent.mkdir(parents=True, exist_ok=True)
            cancel.touch()
        await asyncio.gather(task, return_exceptions=True)
        raise
