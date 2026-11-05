"""Docling must load — or the conversion run stops loudly.

WHY THIS EXISTS
---------------
`convert_pdfs.py` converts PDFs with GROBID + docling merged. Docling is what
carries **tables** into `body.md`; GROBID's tables land in an object the folder
renderer never writes. So when docling can't be imported, pdf4llm quietly falls
back to `extraction_mode: "grobid-only"` — text with **zero tables** — and every
downstream step keeps reporting success:

  * pdf4llm marks the record `status="success"` (no ProcessingError raised),
  * `convert_pdfs.py` exits 0,
  * `evidence.py` sets `has_fulltext=True` on directory existence alone,
  * pass-2 extracts from table-free text and silently misses table-only
    outcomes, then never revisits the paper (resumability skips it).

That chain cost us real data: 1,708 grobid-only conversions across reviews
(361/370 in one), including a trial whose two co-primary outcomes existed only
in Table 2 and were therefore absent from the extraction.

The failure is also self-amplifying: pdf4llm caches an unrecoverable docling
import in a process global, so PDF #2 onward skips docling without retrying.
One broken import degrades an entire corpus.

Hence: probe ONCE, up front, and refuse to convert on a broken docling unless
the caller explicitly opts into degraded output. Repair is offered rather than
merely described — `pdf4llm.install_grobid.ensure_docling` already knows how to
fix the common numpy 1.x/2.x ABI break (force-reinstall numpy+pandas+pyarrow,
then re-exec, since loaded C extensions can't be swapped in-process). It was
only ever wired into pdf4llm's own CLI, never into the library path this
pipeline uses — which is precisely why the breakage went unnoticed.
"""
from __future__ import annotations

import os
import shutil
import sys

# Docling + its ML model cache on first run.
DOCLING_DISK_GB = 5.0


def docling_importable() -> tuple[bool, str]:
    """(True, "") if docling imports cleanly here, else (False, error text).

    Imports `DocumentConverter` specifically: the failure we hit was a partially
    installed docling where `import docling` succeeded and the symbol import did
    not.
    """
    # BaseException, not Exception: a numpy 1.x/2.x ABI break can surface as a
    # bare SystemError (or worse) from C-extension init, which `except
    # Exception` lets through — and an uncaught one kills the process with a
    # traceback instead of our banner, exactly when the banner matters most.
    try:
        import importlib
        importlib.import_module("docling")
        getattr(importlib.import_module("docling.document_converter"),
                "DocumentConverter")
        return True, ""
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return False, f"{type(e).__name__}: {e}"


def is_numpy_abi_error(err: str) -> bool:
    """Does this read like the numpy 1.x/2.x ABI mismatch we know how to fix?

    Mirrors the sniff test in pdf4llm's installer: pandas/pyarrow compiled
    against a different numpy than the one loaded. Distinct from a plain
    "docling isn't installed", which needs a different pip command.
    """
    low = err.lower()
    return ("numpy" in low and any(
        t in low for t in ("abi", "multiarray", "compiled using numpy", "_array_api")
    )) or "unable to convert function return value" in low


def _banner(lines: list[str], width: int = 78) -> str:
    """A fixed-width banner. Long lines (error text, pip commands) wrap rather
    than stretching the rule to an unreadable width."""
    out: list[str] = []
    for line in lines:
        if len(line) <= width - 4:
            out.append(line)
            continue
        # Wrap on whitespace, keeping a hanging indent so continuations read as
        # part of the same field.
        words, cur = line.split(), ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > width - 4:
                out.append(cur)
                cur = "    " + w
            else:
                cur = f"{cur} {w}".strip() if cur else w
        if cur:
            out.append(cur)
    bar = "=" * width
    body = "\n".join(f"  {line}" for line in out)
    return f"\n{bar}\n{body}\n{bar}\n"


def explain(err: str, *, python_exe: str | None = None) -> str:
    """The unmissable banner: what broke, what it costs, how to fix it."""
    exe = python_exe or sys.executable
    cause = (
        "numpy 1.x / 2.x ABI mismatch — pandas/pyarrow here were compiled "
        "against a different numpy than the one loaded."
        if is_numpy_abi_error(err)
        else "docling is missing or its install is incomplete."
    )
    fix = (
        f"{exe} -m pip install --upgrade --force-reinstall --no-cache-dir "
        "numpy pandas pyarrow docling"
        if is_numpy_abi_error(err)
        else f"{exe} -m pip install --upgrade docling"
    )
    return _banner([
        "DOCLING IS NOT AVAILABLE — REFUSING TO CONVERT",
        "",
        "Docling is what captures TABLES. Without it this run would produce",
        "GROBID-only text containing NO TABLES AT ALL, and extraction would",
        "silently miss every outcome that lives only in a table — with no",
        "error anywhere. That has already cost this project real data.",
        "",
        f"Interpreter: {exe}",
        f"Cause:       {cause}",
        f"Error:       {err[:300]}",
        "",
        f"Fix:         {fix}",
        "",
        "Then re-run this command. To attempt the fix automatically, re-run",
        "with --fix-docling (or set BER_FIX_DOCLING=1).",
        "To deliberately accept table-free output, pass --allow-degraded.",
    ])


def _disk_free_gb() -> float:
    try:
        return shutil.disk_usage(os.path.expanduser("~")).free / (1024 ** 3)
    except OSError:
        return float("inf")


def attempt_repair(err: str, *, assume_yes: bool = False) -> bool:
    """Try to make docling importable, re-exec'ing this process on success.

    Borrows pdf4llm's `ensure_docling` when importable (it owns the ABI
    diagnosis + re-exec), but calls it with the base-env guard relaxed: our
    pipeline interpreter is anaconda base, not a venv, and pdf4llm's default
    refusal would emit venv advice that doesn't apply here.

    Returns False if repair was declined, impossible, or failed. On success it
    does NOT return — the process re-execs so C extensions reload cleanly.
    """
    if _disk_free_gb() < DOCLING_DISK_GB:
        print(f"Not enough free disk (~{DOCLING_DISK_GB:.0f} GB needed).",
              file=sys.stderr)
        return False

    if not assume_yes and sys.stdin.isatty():
        reply = input("Attempt to install/repair docling now? [Y/n] ").strip().lower()
        if reply and not reply.startswith("y"):
            return False

    try:
        from pdf4llm.install_grobid import ensure_docling
    except Exception:  # noqa: BLE001 — pdf4llm itself may be the broken import
        ensure_docling = None

    if ensure_docling is not None:
        try:
            # Newer pdf4llm accepts allow_base_env; older ones don't.
            try:
                ok = ensure_docling(allow_base_env=True)
            except TypeError:
                ok = _repair_with_pip(err)
                return ok
            if ok:
                _reexec()
            return bool(ok)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"pdf4llm repair path failed ({e}); falling back to pip.",
                  file=sys.stderr)

    return _repair_with_pip(err)


def _repair_with_pip(err: str) -> bool:
    """Direct pip repair, used when pdf4llm's installer can't be reached.

    Reinstalls the C-extension set together so they share one numpy ABI —
    the technique pdf4llm's installer uses. pip's resolver picks versions; we
    deliberately don't pin numpy.
    """
    import subprocess

    args = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if is_numpy_abi_error(err):
        args += ["--force-reinstall", "--no-cache-dir",
                 "numpy", "pandas", "pyarrow", "docling"]
    else:
        args += ["docling"]
    print("Running: " + " ".join(args), flush=True)
    try:
        subprocess.run(args, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"pip install failed: {e}", file=sys.stderr)
        return False

    ok, err2 = docling_importable()
    if not ok:
        # The import can still fail in THIS process even when the install is
        # good: already-loaded C extensions can't be replaced in-place. Re-exec
        # and let the fresh interpreter decide.
        _reexec()
    return True


#: Set in the environment of the re-exec'd process. A repair that did not fix
#: the import must not repair and re-exec AGAIN: with BER_FIX_DOCLING=1 still
#: exported, the restarted run used to repeat the whole cycle forever.
REEXEC_ENV = "BER_DOCLING_REEXEC"


def already_reexeced() -> bool:
    return os.environ.get(REEXEC_ENV) == "1"


def _reexec() -> None:
    """Restart this exact command so freshly installed C extensions load.

    At most once per command: the marker variable makes the restarted process
    give up instead of repairing again.
    """
    if already_reexeced():
        print("Repair already attempted and a restart did not fix the import; "
              "giving up.", file=sys.stderr, flush=True)
        sys.exit(1)
    print("\n✓ Repair finished — restarting to pick up the new packages.\n",
          flush=True)
    sys.stdout.flush()
    os.environ[REEXEC_ENV] = "1"
    try:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except OSError as e:
        print(f"Could not restart ({e}). Please re-run your command.",
              file=sys.stderr)
        sys.exit(1)


def require_docling(*, needed: bool, fix: bool = False,
                    allow_degraded: bool = False) -> None:
    """Gate a conversion run on docling being importable.

    ``needed`` is False for modes that never use docling, so those runs are
    unaffected. On a broken docling: print the banner, optionally attempt
    repair, and otherwise exit non-zero WITHOUT converting anything — a
    non-zero exit is what makes run_pipeline.py's step check and the studio's
    job runner report a real failure instead of a green run.
    """
    if not needed:
        return
    ok, err = docling_importable()
    if ok:
        return

    print(explain(err), file=sys.stderr, flush=True)

    if allow_degraded:
        print("--allow-degraded: continuing WITHOUT tables, as requested.\n",
              file=sys.stderr, flush=True)
        return

    if already_reexeced():
        # This process IS the restart after a repair, and docling still does
        # not import: repairing again would only loop.
        print("docling is still not importable after an automatic repair and "
              "restart; not repairing again. Fix the environment by hand.",
              file=sys.stderr, flush=True)
        sys.exit(1)

    if fix or os.environ.get("BER_FIX_DOCLING") == "1":
        if attempt_repair(err, assume_yes=True):
            ok, err = docling_importable()
            if ok:
                return
        print("Automatic repair did not succeed.", file=sys.stderr)
    elif sys.stdin.isatty():
        if attempt_repair(err):
            ok, err = docling_importable()
            if ok:
                return

    sys.exit(1)
