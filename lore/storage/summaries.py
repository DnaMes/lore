"""Persisted multi-level session summaries (#197, migration 18)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from lore.services.index import load_deleted_session_ids

from .schema import open_v2_connection
from .writer import v2_db_path

SUMMARY_LEVELS = frozenset({"chunk", "session", "project", "day"})
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_IN_IDS = 500


def summary_fingerprint(
    compressed_text: str,
    model: str,
    prompt_version: int,
    compressor_version: int,
) -> str:
    """Return the sha256 hex digest of the compression inputs."""
    raw = "\x00".join(
        [
            compressed_text,
            model,
            str(prompt_version),
            str(compressor_version),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _connect(output_dir: Path) -> Optional[sqlite3.Connection]:
    """Open the v2 store, or return None when it does not exist (never create it)."""
    db = v2_db_path(output_dir)
    return open_v2_connection(db) if db.exists() else None


def _deleted_ids(output_dir: Path) -> set[str]:
    return load_deleted_session_ids(output_dir / "deleted_sessions.json")


def _row_hidden(
    session_id: Optional[str], deleted: set[str], payload: Optional[Dict[str, Any]] = None
) -> bool:
    """True for a deleted session's row, or an aggregate row that lists a deleted session."""
    if session_id and session_id in deleted:
        return True
    children = payload.get("children") if payload else None
    return isinstance(children, list) and any(isinstance(c, str) and c in deleted for c in children)


def _parse_payload(raw: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _row_to_dict(row: sqlite3.Row, deleted: set[str]) -> Optional[Dict[str, Any]]:
    if row["session_id"] and row["session_id"] in deleted:
        return None
    payload = _parse_payload(row["payload_json"])
    if payload is None or _row_hidden(row["session_id"], deleted, payload):
        return None
    return {
        "level": row["level"],
        "scope_key": row["scope_key"],
        "session_id": row["session_id"],
        "fingerprint": row["fingerprint"],
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "compressor_version": row["compressor_version"],
        "payload": payload,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "created": row["created"],
    }


def get_summary(output_dir: Path, level: str, scope_key: str) -> Optional[Dict[str, Any]]:
    """Return the stored summary for ``(level, scope_key)``, or None."""
    conn = _connect(output_dir)
    if conn is None:
        return None
    deleted = _deleted_ids(output_dir)
    try:
        row = conn.execute(
            "SELECT level, scope_key, session_id, fingerprint, model, prompt_version, "
            "compressor_version, payload_json, input_tokens, output_tokens, created "
            "FROM session_summaries WHERE level = ? AND scope_key = ?",
            (level, scope_key),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_dict(row, deleted)


def _validate_save(
    level: str, scope_key: str, session_id: Optional[str], payload: Dict[str, Any]
) -> str:
    if level not in SUMMARY_LEVELS:
        raise ValueError(f"invalid summary level: {level}")
    if level in ("session", "chunk"):
        if not session_id:
            raise ValueError(f"{level} summaries need a session_id")
        if not scope_key.startswith(f"{level}:{session_id}"):
            raise ValueError("scope_key does not match level and session_id")
    try:
        encoded = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"summary payload is not valid JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("summary payload exceeds 64 KB")
    return encoded


def save_summary(
    output_dir: Path,
    *,
    level: str,
    scope_key: str,
    session_id: Optional[str],
    fingerprint: str,
    model: str,
    prompt_version: int,
    compressor_version: int,
    payload: Dict[str, Any],
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    """Insert or replace the summary for ``(level, scope_key)``."""
    if not isinstance(payload, dict):
        raise ValueError("summary payload must be a dict")
    payload_json = _validate_save(level, scope_key, session_id, payload)
    if _row_hidden(session_id, _deleted_ids(output_dir), payload):
        return

    conn = open_v2_connection(v2_db_path(output_dir))
    try:
        conn.execute(
            """
            INSERT INTO session_summaries (
                level, scope_key, session_id, fingerprint, model, prompt_version,
                compressor_version, payload_json, input_tokens, output_tokens, created
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(level, scope_key) DO UPDATE SET
                session_id = excluded.session_id,
                fingerprint = excluded.fingerprint,
                model = excluded.model,
                prompt_version = excluded.prompt_version,
                compressor_version = excluded.compressor_version,
                payload_json = excluded.payload_json,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                created = excluded.created
            """,
            (
                level,
                scope_key,
                session_id,
                fingerprint,
                model,
                prompt_version,
                compressor_version,
                payload_json,
                input_tokens,
                output_tokens,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        # The tombstone is persisted before the purge runs, so a save that raced a deletion
        # either sees the tombstone here (and removes its own row) or is removed by the purge.
        if _row_hidden(session_id, _deleted_ids(output_dir), payload):
            conn.execute(
                "DELETE FROM session_summaries WHERE level = ? AND scope_key = ?",
                (level, scope_key),
            )
    finally:
        conn.close()


def list_summaries(
    output_dir: Path,
    level: Optional[str] = None,
    session_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Return stored summaries, optionally filtered by level and/or session ids."""
    ids: Optional[List[str]] = None
    if session_ids is not None:
        ids = [i for i in session_ids if isinstance(i, str)]
        if not ids:
            return []
        if len(ids) > _MAX_IN_IDS:  # stay below SQLite's bound-variable limit
            merged: List[Dict[str, Any]] = []
            for start in range(0, len(ids), _MAX_IN_IDS):
                merged.extend(list_summaries(output_dir, level, ids[start : start + _MAX_IN_IDS]))
            return merged
    conn = _connect(output_dir)
    if conn is None:
        return []
    deleted = _deleted_ids(output_dir)
    clauses: List[str] = []
    params: List[Any] = []
    if level is not None:
        clauses.append("level = ?")
        params.append(level)
    if ids is not None:
        placeholders = ",".join("?" * len(ids))
        clauses.append(f"session_id IN ({placeholders})")
        params.extend(ids)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = conn.execute(
            "SELECT level, scope_key, session_id, fingerprint, model, prompt_version, "
            f"compressor_version, payload_json, input_tokens, output_tokens, created "
            f"FROM session_summaries{where} ORDER BY created, scope_key",
            params,
        ).fetchall()
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for row in rows:
        parsed = _row_to_dict(row, deleted)
        if parsed is not None:
            results.append(parsed)
    return results


def delete_summaries_for_session(output_dir: Path, session_id: str) -> int:
    """Delete chunk/session rows for ``session_id`` and day/project rows that list it.

    Day and project payloads may include a ``children`` list of session ids; any
    row whose ``children`` contains ``session_id`` is removed (simple scan).
    """
    conn = _connect(output_dir)
    if conn is None:
        return 0
    removed = 0
    try:
        cur = conn.execute(
            "DELETE FROM session_summaries WHERE (session_id = ? AND level IN ('session', 'chunk')) "
            "OR scope_key = ? OR scope_key LIKE ? ESCAPE '\\'",
            (
                session_id,
                f"session:{session_id}",
                "chunk:"
                + session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + ":%",
            ),
        )
        removed += cur.rowcount
        rows = conn.execute(
            "SELECT level, scope_key, payload_json FROM session_summaries "
            "WHERE level IN ('day', 'project')"
        ).fetchall()
        for row in rows:
            payload = _parse_payload(row["payload_json"])
            children = payload.get("children") if payload else None
            if isinstance(children, list) and session_id in children:
                conn.execute(
                    "DELETE FROM session_summaries WHERE level = ? AND scope_key = ?",
                    (row["level"], row["scope_key"]),
                )
                removed += 1
    finally:
        conn.close()
    return removed


def has_current_summary(output_dir: Path, session_id: str, fingerprint: str) -> bool:
    """Return True when a session-level summary exists with ``fingerprint``."""
    row = get_summary(output_dir, "session", f"session:{session_id}")
    return row is not None and row.get("fingerprint") == fingerprint
