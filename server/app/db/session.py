"""Async engine + session factory built from settings.DATABASE_URL.

Works unchanged on sqlite+aiosqlite (default) and postgresql+asyncpg.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

# SQLite needs check_same_thread relaxed under async; asyncpg ignores it.
# `timeout` is the busy timeout in SECONDS: how long a writer waits for a lock
# before giving up. The driver default of 5s is far too short here — the API and
# TWO arq workers all write to one file, and a background job (metadata
# enrichment over a whole corpus) writes in a tight loop for minutes.
_connect_args: dict = {}
if _IS_SQLITE:
    _connect_args = {"check_same_thread": False, "timeout": 30.0}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    connect_args=_connect_args,
)


if _IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - I/O
        """Make concurrent access survivable on the sqlite dev database.

        Without this, a config PATCH failed with `OperationalError: database is
        locked` whenever a background job was running — surfacing in the studio
        as "Cannot reach the API … is this origin in CORS_ORIGINS?", because an
        unhandled 500 carries no CORS headers (see main.py).

        - **WAL**: the default `journal_mode=delete` gives a writer an EXCLUSIVE
          lock on the whole database, blocking every other reader and writer.
          WAL lets one writer proceed alongside concurrent readers, which is
          exactly this app's shape (one writer at a time, many polling reads
          from the studio). It is a persistent property of the database file.
        - **busy_timeout**: wait for a contended lock instead of failing at once.
          Set here as well as via `timeout` so it applies however the connection
          was made.
        - **synchronous=NORMAL**: the recommended pairing with WAL — durable
          against process crashes, and much cheaper than FULL on every commit.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# Columns added to EXISTING tables after their first release. ``create_all``
# only creates missing tables — it never alters an existing one, and this repo
# has no Alembic — so these are applied by hand, idempotently, on every startup.
# (table, column, "ADD COLUMN" DDL fragment)
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("users", "llm_provider",
     "llm_provider VARCHAR(64) NOT NULL DEFAULT 'codex_cli'"),
    ("users", "openrouter_key_encrypted", "openrouter_key_encrypted TEXT"),
    ("users", "pdf_dir", "pdf_dir TEXT"),
]


def _ensure_columns_sqlite(sync_conn) -> None:
    """Idempotently add any missing additive columns (sqlite dev DB).

    Uses ``PRAGMA table_info`` to skip columns that already exist, so re-runs are
    safe. No-ops cleanly on a brand-new DB (create_all already made the columns)
    and on non-sqlite backends (the PRAGMA returns nothing / a deploy uses real
    migrations there)."""
    for table, column, ddl in _ADDITIVE_COLUMNS:
        try:
            existing = {
                row[1]
                for row in sync_conn.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
        except Exception:
            return  # not sqlite, or table absent — nothing to backfill here
        if not existing:  # table doesn't exist yet (fresh create handled it)
            continue
        if column not in existing:
            sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


#: Stage slugs that have been RENAMED, old -> new. `StageState.stage` stores the
#: slug, so a rename without this silently orphans the stored approval and the
#: stage reads as never-approved. Entries are permanent: they must keep applying
#: for any database that predates the rename.
_RENAMED_STAGE_SLUGS = (
    ("export", "view-dashboard"),
)


def _migrate_renamed_stage_slugs(sync_conn) -> None:
    """Point stored approvals at renamed stage slugs. Idempotent.

    Guarded by a NOT EXISTS on the destination so a review that somehow has rows
    under BOTH slugs keeps the newer one rather than tripping the composite PK.
    """
    for old, new in _RENAMED_STAGE_SLUGS:
        try:
            sync_conn.exec_driver_sql(
                "UPDATE stage_states SET stage = ? WHERE stage = ? "
                "AND NOT EXISTS (SELECT 1 FROM stage_states s2 "
                "                WHERE s2.review_id = stage_states.review_id "
                "                  AND s2.stage = ?)",
                (new, old, new),
            )
            # Anything left under the old slug is a duplicate of a row that
            # already exists under the new one; drop it so the table has one
            # approval per (review, stage) as the PK intends.
            sync_conn.exec_driver_sql(
                "DELETE FROM stage_states WHERE stage = ?", (old,)
            )
        except Exception:
            return  # table absent (fresh DB) or non-sqlite — nothing to migrate


async def init_models() -> None:
    """Create all tables (there is no Alembic: new tables appear here, new
    columns on existing tables only via _ADDITIVE_COLUMNS)."""
    from .base import Base
    from . import models

    assert models.Base is Base  # importing models registers every mapper

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.DATABASE_URL.startswith("sqlite"):
            await conn.run_sync(_ensure_columns_sqlite)
            await conn.run_sync(_migrate_renamed_stage_slugs)
