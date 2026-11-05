"""Local SciSciNet SQLite tier for metadata + abstracts.

A read-first fast path in front of the API waterfalls in ``metadata_fetcher.py``
and ``fetch_abstracts.py``. Measured on a real corpus: ~82% metadata hit rate,
~52% abstract hit rate, ~60-100 ms/lookup against an external USB drive -- versus
seconds per paper through the APIs.

This module is strictly an OPTIMIZATION. Every public function swallows its own
errors and returns None/{} on any problem, so an unset ``BER_SCISCINET_DIR``, a
missing directory, or a drive yanked mid-run all degrade to exactly the previous
behaviour (pure API path). Nothing here may ever raise into a pipeline run.

Configuration
-------------
``BER_SCISCINET_DIR``   directory holding journals.db / abstracts.db / papers_extra.db
``BER_SCISCINET_WRITE`` set to 1 to enable write-back (default OFF)

Write-back
----------
API results are written back so each paper costs a network fetch at most once:
the three biblio fields SciSciNet structurally lacks (volume/issue/pages) go into
columns added to ``paper_summary``; papers SciSciNet doesn't have at all go into a
separate ``ber_papers`` table so upstream rows are never overwritten.

Writes are refused on FUSE filesystems: both DBs are in WAL mode, which needs
shared-memory locking that FUSE/NTFS doesn't provide reliably, and journals.db is
~1.5 TB. Prod (ext4/NVMe) is fine; the dev external drive is not.

Schema notes (verified against the 01/29/2026 backup)
-----------------------------------------------------
* ``paper_summary.doi`` is a full LOWERCASE url: ``https://doi.org/10.xxxx/...``
  (zero non-lowercase rows), indexed by ``idx_paper_summary_doi``.
* ``paper_pubmed`` has no PMID index -- reverse PMID lookups go through
  ``mesh.db.paperid_pmid``, which is indexed both ways.
* ``paper_authors.author_position`` is only first/middle/last and does NOT encode
  order; use rowid order and de-duplicate (one sampled paper had 956 rows).
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Below this an abstract is treated as missing, matching fetch_abstracts.py.
MIN_ABSTRACT_CHARS = 50

# The 8-key contract fetch_metadata_from_doi returns.
META_FIELDS = ("authors", "title", "journal", "volume", "issue", "pages", "year", "url")

_DB_FILES = {
    "journals": "journals.db",
    "abstracts": "abstracts.db",
    "papers_extra": "papers_extra.db",
    "mesh": "mesh.db",
}

# Latches on first hard failure so a 100K-paper run doesn't retry a dead mount
# 100K times. Reads and writes latch independently: losing writes must not cost
# us the (still working) read tier.
_read_disabled = False
_write_disabled = False
_warned: set[str] = set()

_local = threading.local()  # per-thread sqlite connections (they aren't thread-safe)
_writer_lock = threading.Lock()
_writer: "_Writer | None" = None


def _warn_once(key: str, msg: str) -> None:
    """Log at WARNING the first time only -- per-paper logging would flood a long run."""
    if key not in _warned:
        _warned.add(key)
        logger.warning("local_sciscinet: %s", msg)


def _env(name: str) -> str:
    """Read an env var, falling back to .env.local.

    metadata_fetcher.py hand-parses .env.local rather than using python-dotenv, and
    imports this module, so we can't rely on load_dotenv having run.
    """
    val = os.environ.get(name)
    if val:
        return val
    try:
        env_path = REPO_ROOT / ".env.local"
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith(f"{name}="):
                        return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""


def data_dir() -> Path | None:
    """The configured SciSciNet directory, or None when unset/missing."""
    raw = _env("BER_SCISCINET_DIR")
    if not raw:
        return None
    try:
        p = Path(raw)
        return p if p.is_dir() else None
    except Exception:
        return None


def _fstype(path: Path) -> str:
    """Filesystem type via longest-prefix match against /proc/mounts.

    os.statvfs doesn't expose the fs type, so parse /proc/mounts. Returns "" on
    non-Linux, where the caller then requires the explicit opt-in flag alone.
    """
    try:
        target = os.path.realpath(str(path))
        best = ("", -1)
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                if target.startswith(mount) and len(mount) > best[1]:
                    best = (parts[2], len(mount))
        return best[0]
    except Exception:
        return ""


def available() -> bool:
    """True when the local tier can serve reads. Cheap enough to call per paper."""
    if _read_disabled:
        return False
    d = data_dir()
    if d is None:
        return False
    return (d / _DB_FILES["journals"]).exists()


def writable() -> bool:
    """True only when write-back is explicitly enabled AND safe.

    Three independent gates: the opt-in flag (default off), a non-FUSE filesystem,
    and actually-writable files.
    """
    if _write_disabled or not available():
        return False
    if _env("BER_SCISCINET_WRITE").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    d = data_dir()
    if d is None:
        return False
    fs = _fstype(d)
    if fs.startswith("fuse"):
        _warn_once(
            "fuse",
            f"refusing write-back: {d} is on {fs!r}; SQLite WAL locking is "
            "unreliable on FUSE. Writes are disabled (reads still work).",
        )
        return False
    if not os.access(d, os.W_OK) or not os.access(d / _DB_FILES["journals"], os.W_OK):
        _warn_once("nowrite", f"refusing write-back: {d} is not writable")
        return False
    return True


def _connect(which: str) -> sqlite3.Connection | None:
    """Per-thread read-only connection to one of the DBs, or None if unusable."""
    global _read_disabled
    if _read_disabled:
        return None
    d = data_dir()
    if d is None:
        return None
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    if which in cache:
        return cache[which]
    path = d / _DB_FILES[which]
    conn = None
    if path.exists():
        try:
            # immutable=1 promises the file won't change under us, which lets SQLite
            # skip locking entirely -- but it's a lie once we write, so only the
            # never-written DBs get it.
            immutable = "&immutable=1" if which in ("mesh",) else ""
            conn = sqlite3.connect(
                f"file:{path}?mode=ro{immutable}", uri=True, timeout=30,
                check_same_thread=False,
            )
        except Exception as e:  # noqa: BLE001 -- a bad mount must not kill the run
            _warn_once(f"open:{which}", f"cannot open {path}: {e}")
            conn = None
    cache[which] = conn
    return conn


def _query(which: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read query, degrading to [] on any failure."""
    global _read_disabled
    conn = _connect(which)
    if conn is None:
        return []
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        # A disk/IO error means the mount is likely gone: stop trying entirely.
        if isinstance(e, (sqlite3.OperationalError, sqlite3.DatabaseError)) and (
            "disk I/O" in str(e) or "not a database" in str(e)
        ):
            _read_disabled = True
            _warn_once("io", f"disabling local tier after I/O error: {e}")
        else:
            _warn_once(f"q:{which}", f"query failed on {which}: {e}")
        return []
    except Exception as e:  # noqa: BLE001
        _warn_once(f"q:{which}", f"query failed on {which}: {e}")
        return []


def _doi_key(doi: str) -> str:
    """The join key: DOIs are stored as full lowercase https://doi.org/ urls."""
    from ber_core.identity import canonical_doi
    from ber_core.utils import norm_doi

    canonical = canonical_doi(doi)
    if canonical is None:
        # Not a well-formed DOI. Keep the old lenient key so an odd value still
        # matches whatever the DB stored for it (it never matches a real row).
        canonical = norm_doi(doi).replace("https://doi.org/", "").replace("http://doi.org/", "")
    return f"https://doi.org/{canonical}" if canonical else ""


def _resolve_paperid(doi: str = "", pmid: str = "") -> str:
    """DOI or PMID -> SciSciNet paperid ("" when not found)."""
    if doi:
        key = _doi_key(doi)
        if key:
            rows = _query(
                "journals", "SELECT paperid FROM paper_summary WHERE doi=? LIMIT 1", (key,)
            )
            if rows:
                return rows[0][0] or ""
    if pmid:
        # paper_pubmed has no PMID index; mesh.paperid_pmid is indexed both ways.
        try:
            pmid_int = int(str(pmid).strip())
        except (TypeError, ValueError):
            return ""
        rows = _query(
            "mesh", "SELECT paperid FROM paperid_pmid WHERE pmid=? LIMIT 1", (pmid_int,)
        )
        if rows:
            return rows[0][0] or ""
    return ""


def _authors_for(paperid: str) -> str:
    """"; "-joined author names, de-duplicated, in table order.

    author_position is only first/middle/last so it can't order authors; rowid
    order is the real sequence. Duplicate rows exist (956 on one sampled paper).
    """
    rows = _query(
        "journals",
        "SELECT display_name FROM paper_authors WHERE paperid=? ORDER BY rowid",
        (paperid,),
    )
    seen: set[str] = set()
    names: list[str] = []
    for (name,) in rows:
        name = (name or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return "; ".join(names)


def lookup_metadata(doi: str = "", pmid: str = "") -> dict | None:
    """Metadata for one paper, or None on a miss.

    Returns the same 8 keys as ``fetch_metadata_from_doi`` plus ``pmid``/``pmcid``.
    volume/issue/pages come from the write-back columns when present, else None --
    so the caller's waterfall fills only those gaps.
    """
    if not available():
        return None
    try:
        paperid = _resolve_paperid(doi=doi, pmid=pmid)
        if not paperid:
            # Not in SciSciNet -- but we may have written it back ourselves.
            return _lookup_ber_paper(doi=doi, pmid=pmid)

        meta: dict = {k: None for k in META_FIELDS}

        rows = _query(
            "journals",
            "SELECT title, doi, year, source_name FROM paper_summary WHERE paperid=? LIMIT 1",
            (paperid,),
        )
        found = False
        if rows:
            found = True
            title, row_doi, year, source_name = rows[0]
            meta["title"] = (title or "").strip() or None
            meta["journal"] = (source_name or "").strip() or None
            meta["year"] = str(year) if year else None
            meta["url"] = (row_doi or "").strip() or None
            for field, value in zip(("volume", "issue", "pages"), _extra_biblio(paperid)):
                meta[field] = value

        if not meta["year"]:
            # paper_core carries a full publication_date when year is absent.
            core = _query(
                "papers_extra",
                "SELECT publication_date FROM paper_core WHERE paperid=? LIMIT 1",
                (paperid,),
            )
            if core and core[0][0]:
                found = True
                meta["year"] = str(core[0][0])[:4] or None

        authors = _authors_for(paperid)
        if authors:
            found = True
            meta["authors"] = authors

        pm = _query(
            "papers_extra",
            "SELECT pmid, pmcid FROM paper_pubmed WHERE paperid=? LIMIT 1",
            (paperid,),
        )
        if pm:
            meta["pmid"] = str(pm[0][0]) if pm[0][0] else None
            meta["pmcid"] = str(pm[0][1]) if pm[0][1] else None

        if not found or not (meta.get("title") or meta.get("authors")):
            return None
        if not meta.get("url") and doi:
            meta["url"] = _doi_key(doi)
        meta["_paperid"] = paperid
        return meta
    except Exception as e:  # noqa: BLE001 -- never raise into a pipeline run
        _warn_once("lookup_meta", f"lookup_metadata failed: {e}")
        return None


def _lookup_ber_paper(doi: str = "", pmid: str = "") -> dict | None:
    """Metadata for a paper we wrote back ourselves (absent from SciSciNet)."""
    if not _has_ber_table():
        return None
    key = _doi_key(doi) if doi else ""
    if not key and pmid:
        key = f"pmid:{str(pmid).strip()}"
    if not key:
        return None
    rows = _query(
        "journals",
        """SELECT title, journal, year, authors, volume, issue, pages, pmid
             FROM ber_papers WHERE doi=? LIMIT 1""",
        (key,),
    )
    if not rows:
        return None
    title, journal, year, authors, volume, issue, pages, pm = rows[0]
    if not (title or authors):
        return None
    return {
        "title": title or None, "journal": journal or None, "year": year or None,
        "authors": authors or None, "volume": volume or None, "issue": issue or None,
        "pages": pages or None, "url": key, "pmid": pm or None,
    }


def _extra_biblio(paperid: str) -> tuple:
    """(volume, issue, pages) from the write-back columns, if they exist yet."""
    if not _has_biblio_columns():
        return (None, None, None)
    rows = _query(
        "journals",
        "SELECT volume, issue, pages FROM paper_summary WHERE paperid=? LIMIT 1",
        (paperid,),
    )
    if not rows:
        return (None, None, None)
    return tuple((v or None) for v in rows[0])


_biblio_cols: bool | None = None


def _has_biblio_columns() -> bool:
    """Whether paper_summary has our added volume/issue/pages columns. Cached."""
    global _biblio_cols
    if _biblio_cols is None:
        try:
            cols = {r[1] for r in _query("journals", "PRAGMA table_info(paper_summary)")}
            _biblio_cols = {"volume", "issue", "pages"}.issubset(cols)
        except Exception:  # noqa: BLE001
            _biblio_cols = False
    return _biblio_cols


def lookup_abstract(doi: str = "", pmid: str = "") -> tuple[str, str] | None:
    """(title, abstract) for one paper, or None when absent/too short."""
    if not available():
        return None
    try:
        rows: list = []
        paperid = _resolve_paperid(doi=doi, pmid=pmid)
        if paperid:
            rows = _query(
                "abstracts",
                "SELECT title, abstract FROM paper_abstracts WHERE paperid=? LIMIT 1",
                (paperid,),
            )
        if not rows and _has_ber_table():
            # Written-back papers are keyed by DOI (or pmid: pseudo-key) -- most have
            # no SciSciNet paperid at all, so this must not depend on _resolve_paperid.
            key = _doi_key(doi) if doi else ""
            if not key and pmid:
                key = f"pmid:{str(pmid).strip()}"
            if key:
                rows = _query(
                    "journals",
                    "SELECT title, abstract FROM ber_papers WHERE doi=? LIMIT 1",
                    (key,),
                )
            if not rows and paperid:
                rows = _query(
                    "journals",
                    "SELECT title, abstract FROM ber_papers WHERE paperid=? LIMIT 1",
                    (paperid,),
                )
        if not rows:
            return None
        title = (rows[0][0] or "").strip()
        abstract = (rows[0][1] or "").strip()
        if len(abstract) < MIN_ABSTRACT_CHARS:
            return None
        return title, abstract
    except Exception as e:  # noqa: BLE001
        _warn_once("lookup_abs", f"lookup_abstract failed: {e}")
        return None


def lookup_batch(dois: list[str]) -> dict[str, dict]:
    """Bulk DOI -> metadata for the studio's pass 1. Missing DOIs are absent.

    Chunked ``WHERE doi IN (...)`` to resolve paperids, then per-paper author and
    pubmed lookups (both indexed).
    """
    out: dict[str, dict] = {}
    if not available() or not dois:
        return out
    try:
        from ber_core.utils import norm_doi

        by_key: dict[str, str] = {}
        for d in dois:
            key = _doi_key(d)
            if key:
                by_key.setdefault(key, d)
        keys = list(by_key)
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = _query(
                "journals",
                f"SELECT doi, paperid FROM paper_summary WHERE doi IN ({placeholders})",
                tuple(chunk),
            )
            for row_doi, paperid in rows:
                original = by_key.get(row_doi)
                if not original or not paperid:
                    continue
                meta = lookup_metadata(doi=original)
                if meta:
                    out[norm_doi(original)] = meta
        return out
    except Exception as e:  # noqa: BLE001
        _warn_once("batch", f"lookup_batch failed: {e}")
        return out


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

_ber_table: bool | None = None


def _has_ber_table() -> bool:
    global _ber_table
    if _ber_table is None:
        try:
            rows = _query(
                "journals",
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ber_papers'",
            )
            _ber_table = bool(rows)
        except Exception:  # noqa: BLE001
            _ber_table = False
    return _ber_table


class _Writer:
    """Single writer thread draining a queue.

    One writer, not N: journals.db is ~1.5 TB and shared with the sciscinet
    dashboard, so 5-8 pipeline threads contending for the write lock would be far
    worse than serializing through one connection. Batched transactions keep the
    lock held briefly.
    """

    BATCH = 200

    def __init__(self, path: Path):
        self.path = path
        self.q: queue.Queue = queue.Queue(maxsize=10000)
        self.thread = threading.Thread(target=self._run, name="sciscinet-writer", daemon=True)
        self.written = 0
        self.thread.start()

    def submit(self, item: tuple) -> None:
        try:
            self.q.put_nowait(item)
        except queue.Full:
            pass  # dropping an enrichment is harmless; it'll be refetched next run

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")  # never fail a write on a dashboard read
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Add our columns/table. ALTER TABLE ADD COLUMN is O(1) metadata-only in
        SQLite -- it does NOT rewrite the 1.5 TB file."""
        existing = {r[1] for r in conn.execute("PRAGMA table_info(paper_summary)")}
        for col in ("volume", "issue", "pages", "ber_enriched_at"):
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE paper_summary ADD COLUMN {col} VARCHAR")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ber_papers (
                   doi VARCHAR PRIMARY KEY, paperid VARCHAR, title VARCHAR,
                   journal VARCHAR, year VARCHAR, authors VARCHAR,
                   volume VARCHAR, issue VARCHAR, pages VARCHAR,
                   pmid VARCHAR, abstract VARCHAR, ber_enriched_at VARCHAR)"""
        )
        conn.commit()

    def _run(self) -> None:
        global _write_disabled
        conn = None
        try:
            conn = self._open()
            self._ensure_schema(conn)
        except Exception as e:  # noqa: BLE001
            _write_disabled = True
            _warn_once("wopen", f"disabling write-back: {e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return

        pending: list[tuple] = []
        while True:
            try:
                item = self.q.get(timeout=2.0)
                if item is None:
                    break
                pending.append(item)
            except queue.Empty:
                pass
            if pending and (len(pending) >= self.BATCH or self.q.empty()):
                try:
                    self._flush(conn, pending)
                except Exception as e:  # noqa: BLE001
                    _write_disabled = True
                    _warn_once("wflush", f"disabling write-back after error: {e}")
                    break
                finally:
                    pending = []
        try:
            conn.close()
        except Exception:
            pass

    def _flush(self, conn: sqlite3.Connection, items: list[tuple]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("BEGIN")
        for kind, payload in items:
            if kind == "biblio":
                paperid, vol, iss, pgs = payload
                # COALESCE: only fill blanks, never overwrite SciSciNet's own data.
                conn.execute(
                    """UPDATE paper_summary
                          SET volume=COALESCE(volume, ?), issue=COALESCE(issue, ?),
                              pages=COALESCE(pages, ?), ber_enriched_at=?
                        WHERE paperid=?""",
                    (vol, iss, pgs, now, paperid),
                )
            elif kind == "paper":
                d = payload
                conn.execute(
                    """INSERT INTO ber_papers
                       (doi, paperid, title, journal, year, authors, volume, issue,
                        pages, pmid, abstract, ber_enriched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(doi) DO UPDATE SET
                         title=COALESCE(excluded.title, title),
                         journal=COALESCE(excluded.journal, journal),
                         year=COALESCE(excluded.year, year),
                         authors=COALESCE(excluded.authors, authors),
                         volume=COALESCE(excluded.volume, volume),
                         issue=COALESCE(excluded.issue, issue),
                         pages=COALESCE(excluded.pages, pages),
                         pmid=COALESCE(excluded.pmid, pmid),
                         abstract=COALESCE(excluded.abstract, abstract),
                         ber_enriched_at=excluded.ber_enriched_at""",
                    (
                        d.get("doi"), d.get("paperid"), d.get("title"), d.get("journal"),
                        d.get("year"), d.get("authors"), d.get("volume"), d.get("issue"),
                        d.get("pages"), d.get("pmid"), d.get("abstract"), now,
                    ),
                )
        conn.commit()
        self.written += len(items)


def _get_writer() -> "_Writer | None":
    global _writer
    if not writable():
        return None
    with _writer_lock:
        if _writer is None:
            d = data_dir()
            if d is None:
                return None
            _writer = _Writer(d / _DB_FILES["journals"])
        return _writer


def save_metadata(doi: str, meta: dict) -> None:
    """Persist API-fetched metadata. No-op unless write-back is enabled and safe."""
    if not meta:
        return
    w = _get_writer()
    if w is None:
        return
    try:
        paperid = meta.get("_paperid") or _resolve_paperid(doi=doi, pmid=meta.get("pmid") or "")
        vol, iss, pgs = meta.get("volume"), meta.get("issue"), meta.get("pages")
        if paperid:
            if vol or iss or pgs:
                w.submit(("biblio", (paperid, vol, iss, pgs)))
            return
        # Not in SciSciNet at all -> our own table, so upstream rows stay pristine.
        key = _doi_key(doi)
        if not key:
            return
        w.submit(("paper", {
            "doi": key, "paperid": None, "title": meta.get("title"),
            "journal": meta.get("journal"), "year": meta.get("year"),
            "authors": meta.get("authors"), "volume": vol, "issue": iss, "pages": pgs,
            "pmid": meta.get("pmid"), "abstract": None,
        }))
    except Exception as e:  # noqa: BLE001
        _warn_once("save_meta", f"save_metadata failed: {e}")


def save_abstract(doi: str = "", pmid: str = "", title: str = "", abstract: str = "") -> None:
    """Persist an API-fetched abstract. No-op unless write-back is enabled and safe."""
    if not abstract or len(abstract.strip()) < MIN_ABSTRACT_CHARS:
        return
    w = _get_writer()
    if w is None:
        return
    try:
        key = _doi_key(doi) if doi else ""
        if not key and pmid:
            key = f"pmid:{str(pmid).strip()}"
        if not key:
            return
        paperid = _resolve_paperid(doi=doi, pmid=pmid)
        w.submit(("paper", {
            "doi": key, "paperid": paperid or None, "title": (title or "").strip() or None,
            "journal": None, "year": None, "authors": None, "volume": None,
            "issue": None, "pages": None,
            "pmid": str(pmid).strip() if pmid else None,
            "abstract": abstract.strip(),
        }))
    except Exception as e:  # noqa: BLE001
        _warn_once("save_abs", f"save_abstract failed: {e}")


def flush(timeout: float = 30.0) -> int:
    """Drain pending writes. Call at the end of a run; returns rows written."""
    global _writer
    with _writer_lock:
        w = _writer
    if w is None:
        return 0
    import time

    try:
        deadline = time.monotonic() + timeout
        # Let the writer drain what's queued, then send the stop sentinel.
        while not w.q.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        w.q.put(None)
        w.thread.join(timeout=max(1.0, deadline - time.monotonic()))
        return w.written
    except Exception:  # noqa: BLE001
        return 0


def stats() -> dict:
    """Diagnostics for logging: is the tier on, and is it writing?"""
    d = data_dir()
    return {
        "dir": str(d) if d else None,
        "available": available(),
        "writable": writable(),
        "fstype": _fstype(d) if d else None,
        "written": _writer.written if _writer else 0,
    }
