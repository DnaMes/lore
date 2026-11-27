"""Stable evidence references for memory links (#160).

A memory that points at a transcript used to store ``memory_sources.message_id``,
an autoincrement SQLite rowid. Re-indexing a session deletes and re-inserts its
message rows, so that integer silently starts pointing at a different message.
This module records an identity that survives reindexing — session namespace,
sequence, native id and a content hash — and resolves or repairs the legacy
integer when a caller needs it.

Contract: ``docs/superpowers/specs/2026-10-26-source-revision-and-identity-design.md``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

#: Fields selected from a message row for identity purposes.
_MESSAGE_IDENTITY_SQL = (
    "SELECT m.id, m.seq, m.native_id, m.role, m.content, m.timestamp, m.model, "
    "s.source_namespace, s.source_revision_id "
    "FROM messages m JOIN sessions s ON s.id = m.session_id "
)


def message_sha256(row: Any) -> str:
    """Content hash of one message, independent of its storage rowid."""
    payload = {
        "role": row["role"],
        "content": row["content"] or "",
        "timestamp": row["timestamp"],
        "model": row["model"],
    }
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode("utf-8", "surrogatepass")).hexdigest()


def identity_for_message(conn, session_id: str, seq: int) -> Optional[Dict[str, Any]]:
    """Stable identity of ``(session_id, seq)``, or ``None`` when it is unknown."""
    row = conn.execute(
        _MESSAGE_IDENTITY_SQL + "WHERE m.session_id = ? AND m.seq = ?",
        (session_id, seq),
    ).fetchone()
    if row is None:
        return None
    return {
        "message_id": int(row["id"]),
        "message_seq": int(row["seq"]),
        "message_native_id": row["native_id"],
        "message_sha256": message_sha256(row),
        "session_namespace": row["source_namespace"],
        "source_revision_id": row["source_revision_id"],
    }


def identity_for_rowid(conn, message_rowid: int) -> Optional[Dict[str, Any]]:
    """Stable identity of a legacy ``message_id`` (rowid), if the row still exists."""
    row = conn.execute(_MESSAGE_IDENTITY_SQL + "WHERE m.id = ?", (message_rowid,)).fetchone()
    if row is None:
        return None
    return {
        "message_id": int(row["id"]),
        "message_seq": int(row["seq"]),
        "message_native_id": row["native_id"],
        "message_sha256": message_sha256(row),
        "session_namespace": row["source_namespace"],
        "source_revision_id": row["source_revision_id"],
    }


def record_evidence_link(
    conn,
    *,
    memory_id: int,
    session_id: str,
    message_id: Optional[int] = None,
    message_seq: Optional[int] = None,
    note: Optional[str] = None,
) -> None:
    """Insert a ``memory_sources`` row carrying both the legacy rowid and the
    stable identity. Falls back to the legacy fields when the transcript is not
    in this store yet (a memory may be written before its session is synced).
    """
    identity = None
    if message_seq is not None:
        identity = identity_for_message(conn, session_id, message_seq)
    elif message_id is not None:
        identity = identity_for_rowid(conn, message_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_sources (
            memory_id, session_id, message_id, note, message_seq,
            message_native_id, message_sha256, source_revision_id, session_namespace
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            session_id,
            (identity or {}).get("message_id", message_id),
            note,
            (identity or {}).get("message_seq", message_seq),
            (identity or {}).get("message_native_id"),
            (identity or {}).get("message_sha256"),
            (identity or {}).get("source_revision_id"),
            (identity or {}).get("session_namespace"),
        ),
    )


def resolve_evidence(conn, link: Any) -> Optional[Dict[str, Any]]:
    """Resolve one ``memory_sources`` row to the message it actually points at.

    Tries the stored rowid first (fast path), then the stable identity. Returns
    the current message identity plus ``resolved_by`` so a caller can tell
    whether the rowid was stale. ``None`` when the message is gone.
    """
    rowid = link["message_id"] if "message_id" in link.keys() else None
    if rowid is not None:
        identity = identity_for_rowid(conn, int(rowid))
        if identity is not None:
            return {**identity, "resolved_by": "rowid"}
    seq = link["message_seq"] if "message_seq" in link.keys() else None
    session_id = link["session_id"] if "session_id" in link.keys() else None
    if seq is not None and session_id:
        identity = identity_for_message(conn, session_id, int(seq))
        if identity is not None:
            return {**identity, "resolved_by": "seq"}
    native_id = link["message_native_id"] if "message_native_id" in link.keys() else None
    if native_id and session_id:
        row = conn.execute(
            _MESSAGE_IDENTITY_SQL + "WHERE m.session_id = ? AND m.native_id = ?",
            (session_id, native_id),
        ).fetchone()
        if row is not None:
            return {
                "message_id": int(row["id"]),
                "message_seq": int(row["seq"]),
                "message_native_id": row["native_id"],
                "message_sha256": message_sha256(row),
                "session_namespace": row["source_namespace"],
                "source_revision_id": row["source_revision_id"],
                "resolved_by": "native_id",
            }
    digest = link["message_sha256"] if "message_sha256" in link.keys() else None
    if digest and session_id:
        for candidate in conn.execute(
            "SELECT id, seq, native_id, role, content, timestamp, model "
            "FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ):
            if message_sha256(candidate) == digest:
                identity = identity_for_message(conn, session_id, int(candidate["seq"]))
                if identity is not None:
                    return {**identity, "resolved_by": "sha256"}
    return None


def repair_evidence_rowids(conn) -> int:
    """Re-point stale legacy rowids at the current rows. Returns rows updated.

    Only links with a stable identity are touched: a link whose message cannot
    be found keeps its original rowid and is reported by
    :func:`unresolved_evidence_links` instead of being silently dropped.
    """
    updated = 0
    rows = conn.execute(
        "SELECT memory_id, session_id, message_id, message_seq, message_native_id, "
        "message_sha256 FROM memory_sources"
    ).fetchall()
    for link in rows:
        resolved = resolve_evidence(conn, link)
        if resolved is None:
            continue
        if link["message_id"] == resolved["message_id"]:
            continue
        conn.execute(
            "UPDATE memory_sources SET message_id = ? WHERE memory_id = ? AND session_id = ? "
            "AND message_id IS ?",
            (resolved["message_id"], link["memory_id"], link["session_id"], link["message_id"]),
        )
        updated += 1
    return updated


def unresolved_evidence_links(conn, limit: int = 200) -> list:
    """Links whose message cannot be found any more (deleted transcript)."""
    rows = conn.execute(
        "SELECT memory_id, session_id, message_id, message_seq, message_native_id, "
        "message_sha256 FROM memory_sources ORDER BY memory_id"
    ).fetchall()
    unresolved = []
    for link in rows:
        if resolve_evidence(conn, link) is None:
            unresolved.append({key: link[key] for key in link.keys()})
        if len(unresolved) >= limit:
            break
    return unresolved
