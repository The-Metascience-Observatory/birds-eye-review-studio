"""Proactive Claude Code usage-limit guard for long headless batch runs.

The interactive `/usage` gauge is reachable headlessly: `claude -p "/usage"
--output-format json` returns a `result` text containing lines like:

    Current session: 2% used · resets Jun 20, 9:10pm (America/New_York)
    Current week (all models): 3% used · resets Jun 26, 3am (America/New_York)

This module probes that, parses the 5-hour **session** percentage, and — when it
reaches a threshold — PAUSES the run, polling every `poll_min` minutes until the
session window resets (the percentage drops). Probes are interval-gated (each probe
itself consumes a little session quota), so we only check every `interval_min`.

It is wired into ``claude_cli.call_claude`` so EVERY model call in the pipeline is
guarded automatically (sonnet extraction and haiku screening alike). Disable with
``BER_USAGE_GUARD=0``. Tune via ``BER_USAGE_THRESHOLD`` / ``BER_USAGE_INTERVAL_MIN``
/ ``BER_USAGE_POLL_MIN``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

_SESSION_RE = re.compile(r"Current session:\s*(\d+)\s*%\s*used", re.I)
_WEEK_RE = re.compile(r"Current week \(all models\):\s*(\d+)\s*%\s*used", re.I)

_lock = threading.Lock()
_last_probe = 0.0

# Set once the weekly ceiling is crossed. guard() runs in WORKER threads, so it
# must not raise: a SystemExit there is swallowed by the per-paper handler or,
# worse, leaves ``ThreadPoolExecutor.__exit__`` sending every queued paper
# through the model and discarding the results. The latch makes every later
# call return empty at once; main loops cancel what is queued and exit non-zero
# via ``claude_cli.raise_if_auth_failed``.
_STOP: dict[str, str] = {}


def stop_reason() -> str | None:
    """Why the guard stopped the run, or None while it may continue."""
    return _STOP.get("reason")


def reset_stop() -> None:
    """Clear the stop latch (tests, or a long-lived process starting a new run)."""
    _STOP.clear()


def request_stop(reason: str) -> None:
    """Stop a batch after a provider reports exhausted account capacity."""
    with _lock:
        _STOP.setdefault("reason", reason)


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def probe_usage(timeout: int = 60) -> tuple[int | None, int | None]:
    """Return (session_pct, week_pct) from `/usage`, or (None, None) on failure."""
    try:
        # Same sandbox as every model call: empty private cwd, no tools, no MCP,
        # no settings. `/usage` is a fixed CLI command, so it stays on argv.
        from ber_core.claude_cli import _child_env, cli_command, sandbox_dir
        cmd = cli_command(None, prompt_arg="/usage")
        proc = subprocess.run(
            cmd + ["--no-session-persistence"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(sandbox_dir()), env=_child_env(),
        )
        text = json.loads(proc.stdout).get("result", "")
    except Exception as e:  # network / parse / timeout — fail open
        print(f"  [usage_guard] probe failed: {e}", file=sys.stderr, flush=True)
        return (None, None)
    s = _SESSION_RE.search(text)
    w = _WEEK_RE.search(text)
    return (int(s.group(1)) if s else None, int(w.group(1)) if w else None)


def guard(model: str | None = None) -> None:
    """Interval-gated usage check. If the 5-hour session is at/over the threshold,
    block until it resets (polling every poll_min). Safe to call before every model
    call: it only actually probes once per interval. Thread-safe — concurrent
    callers serialize on the lock, so all workers pause together.

    ``model`` sets how close to the WEEKLY ceiling this call is allowed to run.
    The weekly figure used to be printed but never enforced, so a long run could
    quietly drain the week. Haiku is cheap enough to run closer to the line than
    Sonnet/Opus, hence the separate, higher default.

    Unlike the session gate, exceeding the weekly ceiling does NOT sleep: the
    weekly window resets once a week, so pausing would hang for days. It stops
    the run instead (by latching :func:`stop_reason`, never by raising from a
    worker thread), which is recoverable because every caller writes results
    incrementally and skips what it already has on a re-run.
    """
    if os.environ.get("BER_USAGE_GUARD", "1") == "0" or _STOP:
        return
    threshold = _envf("BER_USAGE_THRESHOLD", 80)
    is_haiku = "haiku" in (model or "").lower()
    week_ceiling = (_envf("BER_USAGE_WEEK_THRESHOLD_HAIKU", 95) if is_haiku
                    else _envf("BER_USAGE_WEEK_THRESHOLD", 95))
    interval = _envf("BER_USAGE_INTERVAL_MIN", 25) * 60
    poll = _envf("BER_USAGE_POLL_MIN", 15) * 60

    global _last_probe
    with _lock:
        now = time.monotonic()
        if now - _last_probe < interval:
            return
        _last_probe = now
        sess, week = probe_usage()
        if sess is None:
            return
        print(f"  [usage_guard] session {sess}% / week {week}% used"
              f" (weekly ceiling {week_ceiling:.0f}%{' haiku' if is_haiku else ''})",
              file=sys.stderr, flush=True)
        if week is not None and week >= week_ceiling:
            msg = (f"weekly usage {week}% >= ceiling {week_ceiling:.0f}% — stopping. "
                   f"Re-run after the weekly window resets; completed work is kept. "
                   f"Override with BER_USAGE_WEEK_THRESHOLD"
                   f"{'_HAIKU' if is_haiku else ''}=<pct>.")
            print(f"  [usage_guard] {msg}", file=sys.stderr, flush=True)
            _STOP["reason"] = f"usage_guard: {msg}"
            return
        while sess is not None and sess >= threshold:
            print(f"  [usage_guard] session {sess}% >= {threshold:.0f}% — pausing "
                  f"{poll/60:.0f} min for window reset (week {week}%)...",
                  file=sys.stderr, flush=True)
            time.sleep(poll)
            sess, week = probe_usage()
            _last_probe = time.monotonic()
            if sess is not None:
                print(f"  [usage_guard] re-check: session {sess}% used", file=sys.stderr, flush=True)
        print("  [usage_guard] under threshold — continuing.", file=sys.stderr, flush=True)


if __name__ == "__main__":  # quick manual check: print current usage
    s, w = probe_usage()
    print(f"session={s}% week={w}%")
