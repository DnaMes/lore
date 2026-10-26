"""Read the v2 SQLite store back into the legacy index dict shape (#44, PR 3).

PR 2 made ``IndexBuilder`` mirror every rebuild into ``index_v2.sqlite``.
This module is the read side: :func:`load_index_v2` returns exactly the
``{"stats": {...}, "sessions": [...]}`` structure that the web layer's
``load_index()`` has always produced from ``index.json`` — so callers need
no changes when the reader is switched over.

Keeping the output shape identical is deliberate: it lets PR 3 flip the
source behind a feature flag with the JSON path as a fallback, and lets PR
3's tests assert v2 and JSON produce equivalent payloads.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from lore.core.models import AUTOMATED_ORIGINS

from .schema import current_version, open_connection
from .writer import v2_db_path

# A v2 DB is only usable once the denormalised display columns exist (v6).
_MIN_USABLE_VERSION = 6
# Contract columns (origin, relation, kind, ...) arrive with migration 17; a
# reader that opens an older store without migrating selects defaults instead.
_CONTRACT_VERSION = 17
_SESSION_CONTRACT_COLUMNS = (
    "origin",
    "parent_session_id",
    "relation",
    "source_version",
    "parser_version",
)
_SESSION_CONTRACT_DEFAULTS = (
    "'unknown' AS origin, NULL AS parent_session_id, NULL AS relation, "
    "NULL AS source_version, 0 AS parser_version"
)
_MESSAGE_CONTRACT_DEFAULTS = (
    "'text' AS kind, NULL AS parent_id, 1 AS active, NULL AS tool_events_json, NULL AS reasoning"
)
# store_meta.generated_at is written from migration 9 onward; below this a v2
# store cannot be staleness-checked, so it is not considered usable.
_MIN_STALENESS_VERSION = 9


def _read_generated_at(conn: sqlite3.Connection) -> Optional[str]:
    """Return the v2 store's generated_at stamp, or None if absent."""
    try:
        row = conn.execute("SELECT value FROM store_meta WHERE key = 'generated_at'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def v2_is_available(output_dir: Path, compare_to: Optional[Path] = None) -> bool:
    """True if a v2 DB exists, is migrated far enough, and is not stale.

    Args:
        output_dir: directory holding ``index_v2.sqlite``.
        compare_to: optional path (typically ``index.json``) — if the v2
            store's ``generated_at`` is older than this file's mtime, the v2
            store is considered stale and unusable, so callers fall back.
    """
    db = v2_db_path(output_dir)
    if not db.exists():
        return False
    try:
        conn = open_connection(db)
        try:
            version = current_version(conn)
            if version < _MIN_USABLE_VERSION:
                return False
            # Staleness check needs the generated_at stamp (migration 9+).
            if compare_to is not None and compare_to.exists():
                if version < _MIN_STALENESS_VERSION:
                    return False
                generated_at = _read_generated_at(conn)
                if not generated_at:
                    return False
                try:
                    v2_time = datetime.fromisoformat(generated_at)
                except ValueError:
                    return False
                # Compare both as POSIX timestamps so the v2 stamp (stored
                # UTC) and the file mtime (POSIX, tz-independent) line up
                # regardless of the local timezone. A 2 s grace window
                # absorbs the sub-second ordering of the JSON-then-v2 write.
                v2_ts = v2_time.timestamp()
                ref_ts = compare_to.stat().st_mtime
                if v2_ts < ref_ts - 2.0:
                    return False
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def session_contract_select(conn: sqlite3.Connection, alias: str = "") -> str:
    """SELECT fragment for the migration-17 session columns (defaults on older stores)."""
    if current_version(conn) < _CONTRACT_VERSION:
        return _SESSION_CONTRACT_DEFAULTS
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{col}" for col in _SESSION_CONTRACT_COLUMNS)


def automated_exclusion_sql(conn: sqlite3.Connection, alias: str = "s") -> str:
    """`` AND <alias>.origin NOT IN (...)`` for default views; empty before migration 17."""
    if current_version(conn) < _CONTRACT_VERSION:
        return ""
    marks = ", ".join(f"'{origin}'" for origin in AUTOMATED_ORIGINS)
    return f" AND {alias}.origin NOT IN ({marks})"


def _row_to_session_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Map one v2 ``sessions`` row to a legacy index session dict."""
    metadata: Dict[str, Any] = {}
    raw_meta = row["metadata_json"]
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
        except (ValueError, TypeError):
            metadata = {}
    return {
        "id": row["id"],
        "tool": row["tool"],
        "project": row["project"],
        "thread_id": row["thread_id"],
        "title": row["title"],
        "created": row["created"],
        "updated": row["updated"],
        "messages": int(row["messages_count"] or 0),
        "prompts": int(row["prompt_count"] or 0),
        "tokens": int(row["total_tokens"] or 0) or None,
        "prompt_outline": row["prompt_outline"],
        "export_path": row["export_path"],
        "git_branch": row["git_branch"],
        "git_commit": row["git_commit"],
        "source_path": row["source_path"],
        "source_mtime": row["source_mtime_ns"],
        "summary": metadata.get("summary"),
        "origin": row["origin"] or "unknown",
        "parent_session_id": row["parent_session_id"],
        "relation": row["relation"],
        "source_version": row["source_version"],
        "parser_version": int(row["parser_version"] or 0),
    }


def _compute_stats(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the legacy ``stats`` block from session dicts."""
    by_tool: Dict[str, int] = {}
    by_project: Dict[str, int] = {}
    total_messages = 0
    for s in sessions:
        tool = str(s.get("tool") or "")
        if tool:
            by_tool[tool] = by_tool.get(tool, 0) + 1
        project = s.get("project")
        if project:
            by_project[project] = by_project.get(project, 0) + 1
        total_messages += int(s.get("messages") or 0)
    return {
        "total_sessions": len(sessions),
        "total_messages": total_messages,
        "by_tool": by_tool,
        "by_project": by_project,
    }


def load_ignored_session_ids(output_dir: Path) -> set[str]:
    """Session ids listed in ``ignored.json`` (pruned); rows stay in SQLite."""
    ignore_path = output_dir / "ignored.json"
    if not ignore_path.exists():
        return set()
    try:
        data = json.loads(ignore_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            values = data.get("session_ids", [])
        else:
            values = data
        return {str(v) for v in (values or []) if v is not None}
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def load_index_v2(output_dir: Path) -> Dict[str, Any]:
    """Load the v2 store as a legacy-shaped index dict.

    Returns ``{"version": "2", "stats": {...}, "sessions": [...]}`` — the same
    structure ``load_index()`` returns from ``index.json``. Sessions are
    sorted newest-first by ``updated``. Pruned ids in ``ignored.json`` are
    omitted from the result (SQLite rows are kept for undo/restore).

    Raises:
        FileNotFoundError: if no v2 DB exists at ``output_dir``.
    """
    db = v2_db_path(output_dir)
    if not db.exists():
        raise FileNotFoundError(f"no v2 store at {db}")

    # A single SELECT is one consistent snapshot under WAL. A future reader
    # that needs sessions AND messages in two queries must wrap them in one
    # BEGIN — otherwise a concurrent full-replace write between the queries
    # could show a session row whose messages were just deleted (#40).
    conn = open_connection(db)
    try:
        session_contract = session_contract_select(conn)
        rows = conn.execute(
            f"""
            SELECT id, tool, project, thread_id, title, created, updated,
                   source_path, source_mtime_ns, git_branch, git_commit,
                   metadata_json, messages_count, prompt_count,
                   prompt_outline, export_path, total_tokens,
                   {session_contract}
            FROM sessions
            ORDER BY updated DESC
            """
        ).fetchall()
        generated_at = _read_generated_at(conn)
    finally:
        conn.close()

    ignored = load_ignored_session_ids(output_dir)
    sessions = [_row_to_session_dict(r) for r in rows if str(r["id"]) not in ignored]
    return {
        "version": "2",
        "generated_at": generated_at,
        "stats": _compute_stats(sessions),
        "sessions": sessions,
    }


def load_session_messages_v2(
    output_dir: Path, session_id: str, *, include_reasoning: bool = False
) -> Optional[list]:
    """Load one session's messages from the v2 store, with per-message tokens/model.

    Returns a list of ``UnifiedMessage`` (ordered by seq) when the session has
    its message rows synced (``messages_synced = 1``), else ``None`` so callers
    fall back to the token-less export-markdown path. This is the path that lets
    the default served view carry token/model chips without ``?live=1`` (#62).

    ``include_reasoning`` is opt-in: thinking text can be up to 20k characters per
    message and must not reach exports or MCP responses by default, so ``reasoning``
    is ``None`` unless a caller asks for it. Compact ``tool_events`` are always
    loaded; they are small and carry names and outcomes only.
    """
    from ..core.models import Role, UnifiedMessage

    db = v2_db_path(output_dir)
    if not db.exists():
        return None

    conn = open_connection(db)
    try:
        synced = conn.execute(
            "SELECT messages_synced FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not synced or not synced[0]:
            return None
        if current_version(conn) >= _CONTRACT_VERSION:
            reasoning_col = "reasoning" if include_reasoning else "NULL AS reasoning"
            message_contract = f"kind, parent_id, active, tool_events_json, {reasoning_col}"
        else:
            message_contract = _MESSAGE_CONTRACT_DEFAULTS
        rows = conn.execute(
            f"""
            SELECT seq, role, content, timestamp, model, tokens_json,
                   {message_contract}
            FROM messages
            WHERE session_id = ?
            ORDER BY seq
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    messages = []
    for (
        _seq,
        role,
        content,
        timestamp,
        model,
        tokens_json,
        kind,
        parent_id,
        active,
        tool_events_json,
        reasoning,
    ) in rows:
        try:
            role_enum = Role(role)
        except ValueError:
            role_enum = Role.USER
        ts = None
        if timestamp:
            try:
                ts = datetime.fromisoformat(timestamp)
            except ValueError:
                ts = None
        tokens = None
        if tokens_json:
            try:
                tokens = json.loads(tokens_json)
            except (ValueError, TypeError):
                tokens = None
        tool_events: list = []
        if tool_events_json:
            try:
                events = json.loads(tool_events_json)
            except (ValueError, TypeError):
                events = []
            if not isinstance(events, list):
                events = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                event = {
                    "name": ev.get("name") or "tool",
                    "status": ev.get("status"),
                    "duration_ms": ev.get("duration_ms"),
                    "is_error": bool(ev.get("is_error")),
                }
                if "dropped" in ev:
                    event["dropped"] = ev["dropped"]
                tool_events.append(event)
        messages.append(
            UnifiedMessage(
                role=role_enum,
                content=content or "",
                timestamp=ts or datetime.fromtimestamp(0),
                model=model,
                tokens=tokens,
                tool_events=tool_events,
                reasoning=reasoning,
                kind=kind or "text",
                parent_id=parent_id,
                active=bool(active),
            )
        )
    return messages
