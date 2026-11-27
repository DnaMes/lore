"""Message-level FTS5 index (migration 29): search evidence without re-tokenising transcripts.

Triggers keep ``message_fts`` current for every message written after the migration.
Messages that already existed (``id <= message_fts_upto``) are indexed here, in small
committed batches, so a 1.3 M message store never holds the write lock for minutes and a
stopped run continues where it left off. Readers call :func:`message_fts_ready` and fall
back to the per-request throwaway index until the backfill has finished.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

META_UPTO = "message_fts_upto"
META_CURSOR = "message_fts_cursor"
META_READY = "message_fts_ready"

# Ids per committed batch: about 2.5 MB of text, so a write lock is held for well under
# a second on a normal CPU and about a second on the 0.25-CPU worker.
BATCH_IDS = 2000


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def message_fts_ready(conn: sqlite3.Connection) -> bool:
    """True when ``message_fts`` indexes every message, so it may answer queries."""
    try:
        return _meta(conn, META_READY) == "1"
    except sqlite3.Error:  # store_meta missing: a store older than migration 29
        return False


def message_fts_pending(conn: sqlite3.Connection) -> bool:
    """True when migration 29 ran and the backfill still has messages to index."""
    try:
        return _meta(conn, META_UPTO) is not None and _meta(conn, META_READY) != "1"
    except sqlite3.Error:
        return False


def backfill_message_fts(
    conn: sqlite3.Connection, *, budget_seconds: Optional[float] = None
) -> int:
    """Index the pre-migration messages; returns how many rows this call indexed.

    Resumable: the cursor is committed with every batch. ``budget_seconds`` stops the
    run after the batch that exhausts it (so every call makes progress; ``None`` or ``0``
    means unlimited). Sets ``message_fts_ready`` once the cursor passes
    ``message_fts_upto``.
    """
    if not message_fts_pending(conn):
        return 0
    upto = int(_meta(conn, META_UPTO) or 0)
    cursor = int(_meta(conn, META_CURSOR) or 0)
    deadline = time.monotonic() + budget_seconds if budget_seconds else None
    indexed = 0
    while cursor < upto:
        high = min(cursor + BATCH_IDS, upto)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # A row <= upto that a trigger already indexed (its content was updated) would
            # collide on the rowid, so skip ids that are present.
            cur = conn.execute(
                "INSERT INTO message_fts(rowid, body) "
                "SELECT id, content FROM messages WHERE id > ? AND id <= ? "
                "AND id NOT IN (SELECT rowid FROM message_fts WHERE rowid > ? AND rowid <= ?)",
                (cursor, high, cursor, high),
            )
            indexed += max(cur.rowcount, 0)
            _set_meta(conn, META_CURSOR, str(high))
            if high >= upto:
                _set_meta(conn, META_READY, "1")
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        cursor = high
        if deadline is not None and time.monotonic() >= deadline:
            break
    return indexed
