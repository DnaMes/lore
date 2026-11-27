"""Retention state that must outlive an index rebuild: tombstones and pruned ids (#48).

``deleted_sessions`` (migration 5) and ``ignored_sessions`` (migration 27) are
append-only id sets. Until #48 they lived only in the JSON files
``deleted_sessions.json`` and ``ignored.json``, which made the export format the
source of truth for state that has nothing to do with export: a rebuild had to
re-read JSON to know it, and a store built on another machine could not tell
which sessions the user had deleted or pruned.

The tables are now the source of truth. The JSON files are still written as a
compatibility mirror for builds that predate this and are read as a fallback
when the store — or its retention table — does not exist yet, so an archive
mid-upgrade never loses an id.

Both sets are append-only: :func:`record_tombstone` and :func:`record_ignored`
use ``INSERT OR IGNORE`` keyed by ``session_id``, so recording the same id twice
is a no-op and a reindex cannot erase it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from .schema import open_v2_connection
from .writer import v2_db_path

#: Table per retention kind. Fixed names — never built from caller input.
TOMBSTONE_TABLE = "deleted_sessions"
IGNORED_TABLE = "ignored_sessions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(output_dir: Path) -> Optional[sqlite3.Connection]:
    """Open the v2 store, or return ``None`` when there is no store yet.

    Retention state follows the store: recording an id must never materialise an
    empty ``index_v2.sqlite`` in a directory that has no archive.
    """
    db = v2_db_path(output_dir)
    if not db.exists():
        return None
    return open_v2_connection(db)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _store_stat(db: Path) -> tuple:
    """Cache key for the id reads: main DB *and* its WAL sidecar.

    In WAL mode a committed write can land in ``-wal`` without changing the main
    file's size or mtime, so keying on the main file alone would serve a stale
    id set after the first tombstone of a session.
    """
    wal = db.with_name(db.name + "-wal")
    stat = db.stat()
    if wal.exists():
        wal_stat = wal.stat()
        return (stat.st_mtime_ns, stat.st_size, wal_stat.st_mtime_ns, wal_stat.st_size)
    return (stat.st_mtime_ns, stat.st_size, 0, 0)


@lru_cache(maxsize=8)
def _read_ids(db_path: str, mtime_ns: int, size: int, wal_mtime_ns: int, wal_size: int, table: str):
    conn = open_v2_connection(Path(db_path))
    try:
        if not _has_table(conn, table):
            return frozenset()
        return frozenset(str(row[0]) for row in conn.execute(f"SELECT session_id FROM {table}"))
    finally:
        conn.close()


def _load(output_dir: Path, table: str) -> frozenset[str]:
    db = v2_db_path(output_dir)
    if not db.exists():
        return frozenset()
    try:
        key = _store_stat(db)
        return _read_ids(str(db), *key, table)
    except (OSError, sqlite3.Error):
        # Unreadable retention state degrades to the JSON mirror the callers
        # union in; it must never break a page load or a search.
        return frozenset()


def _record(output_dir: Path, table: str, session_ids: Iterable[str], reason: Optional[str]) -> int:
    """Append ``session_ids`` to ``table``. Returns the number of new rows."""
    ids = sorted({str(session_id) for session_id in session_ids if str(session_id).strip()})
    if not ids:
        return 0
    conn = _connect(output_dir)
    if conn is None:
        return 0
    try:
        if not _has_table(conn, table):
            return 0
        stamp_column = "deleted_at" if table == TOMBSTONE_TABLE else "ignored_at"
        before = conn.total_changes
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} (session_id, {stamp_column}, reason) VALUES (?, ?, ?)",
            [(session_id, _now(), reason) for session_id in ids],
        )
        added = conn.total_changes - before
        conn.commit()
        _read_ids.cache_clear()
        return added
    finally:
        conn.close()


def load_tombstones(output_dir: Path) -> frozenset[str]:
    """Deleted session ids recorded in the store (empty when there is no store)."""
    return _load(output_dir, TOMBSTONE_TABLE)


def load_ignored(output_dir: Path) -> frozenset[str]:
    """Pruned session ids recorded in the store (empty when there is no store)."""
    return _load(output_dir, IGNORED_TABLE)


def record_tombstone(output_dir: Path, session_id: str, *, reason: Optional[str] = None) -> bool:
    """Append one tombstone. Returns True when the id was newly recorded."""
    return _record(output_dir, TOMBSTONE_TABLE, [session_id], reason) > 0


def record_tombstones(
    output_dir: Path, session_ids: Iterable[str], *, reason: Optional[str] = None
) -> int:
    """Append several tombstones at once (the whole set, idempotently)."""
    return _record(output_dir, TOMBSTONE_TABLE, session_ids, reason)


def record_ignored(
    output_dir: Path, session_ids: Iterable[str], *, reason: Optional[str] = None
) -> int:
    """Append pruned ids. Returns the number of newly recorded ids."""
    return _record(output_dir, IGNORED_TABLE, session_ids, reason)


def clear_retention_cache() -> None:
    """Drop the cached id reads (tests and write paths that bypass mtime keys)."""
    _read_ids.cache_clear()
