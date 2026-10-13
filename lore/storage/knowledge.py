"""Persisted per-session knowledge extractions (#197, migration 14).

One row per session holds the parsed LLM extraction together with a
fingerprint of the exact prompt input, so ``lore knowledge`` can skip a
session whose conversation, model and prompt version did not change.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import open_v2_connection
from .writer import v2_db_path


def prompt_fingerprint(conversation: str, model: str, prompt_version: int) -> str:
    """Return the sha256 hex digest of the prompt input, model and prompt version."""
    raw = "\x00".join([str(prompt_version), model, conversation])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _connect(output_dir: Path) -> Optional[sqlite3.Connection]:
    """Open the v2 store, or return None when it does not exist (never create it)."""
    db = v2_db_path(output_dir)
    return open_v2_connection(db) if db.exists() else None


def get_session_knowledge(output_dir: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """Return the stored extraction for ``session_id``, or None."""
    conn = _connect(output_dir)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT fingerprint, model, prompt_version, payload_json "
            "FROM session_knowledge WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except ValueError:
        return None  # corrupt row: treated as absent, so the session is re-extracted
    return {
        "fingerprint": row["fingerprint"],
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "payload": payload,
    }


def save_session_knowledge(
    output_dir: Path,
    session_id: str,
    fingerprint: str,
    model: str,
    prompt_version: int,
    payload: Dict[str, Any],
) -> None:
    """Insert or replace the extraction for ``session_id``."""
    conn = open_v2_connection(v2_db_path(output_dir))
    try:
        conn.execute(
            """
            INSERT INTO session_knowledge
                (session_id, fingerprint, model, prompt_version, payload_json, created)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                model = excluded.model,
                prompt_version = excluded.prompt_version,
                payload_json = excluded.payload_json,
                created = excluded.created
            """,
            (
                session_id,
                fingerprint,
                model,
                prompt_version,
                json.dumps(payload),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
    finally:
        conn.close()


def list_session_knowledge(output_dir: Path) -> List[Dict[str, Any]]:
    """Return every stored extraction (``session_id``, ``payload``, ``created``)."""
    conn = _connect(output_dir)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT session_id, payload_json, created FROM session_knowledge "
            "ORDER BY created, session_id"
        ).fetchall()
    finally:
        conn.close()
    results: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except ValueError:
            continue  # corrupt row: skipped here, re-extracted by the next run
        results.append({"session_id": r["session_id"], "payload": payload, "created": r["created"]})
    return results
