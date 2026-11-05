"""Shared scaffolding for the batch LLM classifiers.

canonicalize_interventions.py, categorize_interventions.py and
classify_intervention_indication.py each send batches of items to a model and
accumulate a JSON map; classify_trial_verdicts.py sends one trial per call.
They used to carry four copies of the same resume / retry / write code, with
the map written only once at the very end — so an interrupted run threw away
every paid-for batch. This module is the one copy:

* ``load_map``          resume from the existing map ({} on missing/invalid);
* ``call_json``         one prompt -> parsed JSON, retried on parse failure;
* ``classify_batches``  chunk -> call -> accept -> merge, persisting the map
                        atomically after EVERY batch;
* ``write_map_atomic``  temp file + ``os.replace``, so a crash mid-write never
                        leaves a truncated map.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_TIMEOUT = 300
DEFAULT_RETRIES = 2


def _log_status(seq: int, total: int, status: str, detail: str = "") -> None:
    from ber_core.utils import log_status
    log_status(seq, total, status, detail)


def load_map(path: Path, *, label: str = "map") -> dict:
    """The existing JSON object at ``path`` (for resume), or {}."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        _log_status(0, 0, "WARN", f"{label} unreadable; rebuilding")
        return {}
    if not isinstance(data, dict):
        _log_status(0, 0, "WARN", f"{label} is not a JSON object; rebuilding")
        return {}
    return data


def write_map_atomic(path: Path, mapping: dict, *, sort: bool = True) -> None:
    """Write ``mapping`` as indented JSON via a temp file + atomic rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if sort:
        mapping = dict(sorted(mapping.items(), key=lambda kv: str(kv[0]).lower()))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def call_json(prompt: str, model: str, label: str, *, timeout: int = DEFAULT_TIMEOUT,
              retries: int = DEFAULT_RETRIES, seq: int = 0, total: int = 0):
    """Send one prompt; return (parsed JSON object or None, cost in USD).

    A response that does not parse is retried up to ``retries`` attempts in
    total; the cost of every attempt is counted.
    """
    from ber_core.claude_cli import call_claude, parse_json_response
    cost = 0.0
    for attempt in range(1, max(1, retries) + 1):
        resp = call_claude(prompt, model, label, timeout=timeout, use_stdin=True)
        cost += float(getattr(resp, "cost_usd", 0.0) or 0.0)
        parsed = parse_json_response(resp.text, label)
        if parsed:
            return parsed, cost
        _log_status(seq, total, "RETRY", f"{label}: no JSON (attempt {attempt})")
    return None, cost


def batches(items: list, size: int) -> Iterable[list]:
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def lookup(mapping: dict, name: str):
    """``mapping[name]``, tolerating a model that changed the key's case/whitespace."""
    if name in mapping:
        return mapping[name]
    want = name.strip().lower()
    for k, v in mapping.items():
        if isinstance(k, str) and k.strip().lower() == want:
            return v
    return None


def classify_batches(items: list, *, batch_size: int,
                     make_prompt: Callable[[list], str],
                     accept: Callable[[list, dict], dict],
                     model: str, label: str,
                     store: dict, store_path: Path | None,
                     timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
                     persist: Callable[[dict], dict] | None = None) -> float:
    """Classify ``items`` batch by batch, merging accepted results into ``store``.

    ``make_prompt(batch)`` builds the prompt; ``accept(batch, parsed)`` returns
    the {key: value} entries to keep (items it leaves out are retried on the
    next run). After every successful batch ``store`` is written to
    ``store_path`` atomically (``persist(store)`` may transform what is
    written). Returns the total cost.
    """
    chunks = list(batches(items, batch_size))
    total = len(chunks)
    start = time.monotonic()
    cost = 0.0
    from ber_core.utils import log_progress
    for bi, batch in enumerate(chunks, 1):
        parsed, c = call_json(make_prompt(batch), model, f"{label} batch {bi}/{total}",
                              timeout=timeout, retries=retries, seq=bi, total=total)
        cost += c
        if not parsed:
            _log_status(bi, total, "FAIL", f"{label} batch {bi}: no JSON; items retry next run")
            continue
        hits = accept(batch, parsed) or {}
        store.update(hits)
        if store_path is not None:
            write_map_atomic(store_path, persist(store) if persist else store)
        status = "OK" if len(hits) == len(batch) else "WARN" if hits else "FAIL"
        _log_status(bi, total, status, f"{label} batch {bi}: {len(hits)}/{len(batch)} accepted")
        log_progress(bi, total, cost=cost, start_time=start)
    if total:
        print(f"  {label}: {total} batch(es), ${cost:.2f}", file=sys.stderr)
    return cost
