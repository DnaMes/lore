"""User-editable session tags / bookmarks (#56).

These are distinct from the LLM-generated tags carried in the flat index: those
are regenerated on every rebuild, while these are explicit user choices that must
persist across rebuilds. They live in the v2 store's ``session_tags`` table
(migration 12) and are keyed by a loose ``session_id`` reference (no FK), so a
session can be tagged before it is synced into the store.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .schema import open_v2_connection
from .writer import v2_db_path

# A reserved tag the UI may treat specially (star / favourite).
BOOKMARK_TAG = "bookmark"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_tag(tag: str) -> str:
    return (tag or "").strip().lower()


def _connect(output_dir: Path) -> sqlite3.Connection:
    # open_v2_connection applies pending migrations (incl. 12) only when the
    # schema is behind (#128); a current DB skips the bookkeeping entirely.
    return open_v2_connection(v2_db_path(output_dir))


def add_session_tag(output_dir: Path, session_id: str, tag: str) -> List[str]:
    """Add ``tag`` to ``session_id``. Returns the session's full tag list."""
    norm = _normalize_tag(tag)
    if not session_id or not norm:
        raise ValueError("session_id and a non-empty tag are required")
    conn = _connect(output_dir)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO session_tags (session_id, tag, created) VALUES (?, ?, ?)",
            (session_id, norm, _now()),
        )
        conn.commit()
        return _read_tags(conn, session_id)
    finally:
        conn.close()


def remove_session_tag(output_dir: Path, session_id: str, tag: str) -> List[str]:
    """Remove ``tag`` from ``session_id``. Returns the remaining tag list."""
    norm = _normalize_tag(tag)
    conn = _connect(output_dir)
    try:
        conn.execute(
            "DELETE FROM session_tags WHERE session_id = ? AND tag = ?",
            (session_id, norm),
        )
        conn.commit()
        return _read_tags(conn, session_id)
    finally:
        conn.close()


def get_session_tags(output_dir: Path, session_id: str) -> List[str]:
    """Return the user tags for one session (sorted)."""
    conn = _connect(output_dir)
    try:
        return _read_tags(conn, session_id)
    finally:
        conn.close()


def get_tags_for_sessions(output_dir: Path, session_ids: List[str]) -> Dict[str, List[str]]:
    """Batch-load tags for many sessions: ``{session_id: [tag, ...]}``.

    Sessions with no tags are omitted. One query, for the list-view fan-out.
    """
    ids = [s for s in session_ids if s]
    if not ids:
        return {}
    conn = _connect(output_dir)
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT session_id, tag FROM session_tags "  # noqa: S608 - ids are bound params
            f"WHERE session_id IN ({placeholders}) ORDER BY tag",
            ids,
        ).fetchall()
    finally:
        conn.close()
    out: Dict[str, List[str]] = {}
    for session_id, tag in rows:
        out.setdefault(session_id, []).append(tag)
    return out


def list_all_tags(output_dir: Path) -> Dict[str, int]:
    """Return ``{tag: session_count}`` across all user-tagged sessions."""
    conn = _connect(output_dir)
    try:
        rows = conn.execute(
            "SELECT tag, COUNT(DISTINCT session_id) FROM session_tags GROUP BY tag ORDER BY tag"
        ).fetchall()
    finally:
        conn.close()
    return {tag: int(count) for tag, count in rows}


def _read_tags(conn: sqlite3.Connection, session_id: str) -> List[str]:
    rows = conn.execute(
        "SELECT tag FROM session_tags WHERE session_id = ? ORDER BY tag",
        (session_id,),
    ).fetchall()
    return [r[0] for r in rows]
