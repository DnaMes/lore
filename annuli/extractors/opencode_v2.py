"""OpenCode SQLite v2 tables ``session_v2`` and ``session_message`` (issue #197 / B2.8b)."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Set, Tuple

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from .cursor_agent import is_secret_field_name

if TYPE_CHECKING:
    from .opencode import OpenCodeExtractor

logger = logging.getLogger(__name__)

_MAX_SQLITE_BYTES = 512 * 1024 * 1024
_MAX_SESSIONS_PER_DB = 5000
_MAX_MESSAGES_PER_SESSION = 5000
_MAX_CONTENT_CHARS = 100_000
_MAX_REASONING_CHARS = 20_000
_MAX_TOOL_ARG_CHARS = 200
_MAX_SHELL_OUTPUT_CHARS = 20_000
_MAX_DATA_BYTES = 4 * 1024 * 1024
_MAX_PATH_CHARS = 4096
_MAX_TITLE_CHARS = 200
_MAX_FETCH_BYTES = 128 * 1024 * 1024
_MAX_TOOL_OUTPUT_CHARS = 20_000
_SQLITE_TIMEOUT = 2.0
_QUERY_SECONDS = 10.0

_IGNORED_MESSAGE_TYPES = frozenset(
    {
        "agent-switched",
        "model-switched",
        "location-switched",
        "idle",
    }
)

_SESSION_COLUMNS = (
    "id",
    "project_id",
    "directory",
    "title",
    "version",
    "parent_id",
    "time_created",
    "time_updated",
)


def yield_opencode_v2_sessions(
    extractor: OpenCodeExtractor,
    db_path: Path,
    skip_session_ids: Set[str],
) -> Iterator[UnifiedSession]:
    """Stream v2 sessions from a scratch copy; skip ids already yielded by v1."""
    if db_path.is_symlink():
        return
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), _MAX_SQLITE_BYTES)
            if copy_path is None:
                return
            conn = _open_copy(copy_path)
            if conn is None:
                return
            try:
                if not _table_exists(conn, "session_v2") or not _table_exists(
                    conn, "session_message"
                ):
                    return
                msg_cols = _table_columns(conn, "session_message")
                if not {"session_id", "type", "seq", "data"}.issubset(msg_cols):
                    return
                sess_cols = _table_columns(conn, "session_v2")
                if "id" not in sess_cols:
                    return

                rows = _load_session_rows(conn, sess_cols)
                if rows is None:
                    return

                unknown_types: Set[str] = set()

                def mark_unknown(msg_type: str) -> None:
                    if msg_type not in unknown_types:
                        unknown_types.add(msg_type)
                        extractor._count("records_unknown")

                for row in rows:
                    session_id = row.get("id")
                    if not isinstance(session_id, str) or not session_id:
                        extractor._count("records_unknown")
                        continue
                    if session_id in skip_session_ids:
                        continue
                    extractor._count("files_matched")
                    session = _build_session(
                        conn,
                        db_path,
                        row,
                        mark_unknown,
                        extractor,
                    )
                    if session is None:
                        continue
                    extractor._count("files_parsed")
                    if extractor.should_import_session(session):
                        yield session
            finally:
                conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
        return


def _open_copy(copy_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(
            f"file:{copy_path}?mode=ro",
            uri=True,
            timeout=_SQLITE_TIMEOUT,
        )
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + _QUERY_SECONDS
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        return conn
    except sqlite3.Error:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows if len(row) > 1}


def _load_session_rows(
    conn: sqlite3.Connection, columns: Set[str]
) -> Optional[List[Dict[str, Any]]]:
    select_parts = [c if c in columns else f"NULL AS {c}" for c in _SESSION_COLUMNS]
    query = f"SELECT {', '.join(select_parts)} FROM session_v2 ORDER BY time_updated DESC LIMIT ?"
    try:
        cur = conn.execute(query, (_MAX_SESSIONS_PER_DB + 1,))
        raw = cur.fetchall()
    except sqlite3.Error:
        return None
    if len(raw) > _MAX_SESSIONS_PER_DB:
        raw = raw[:_MAX_SESSIONS_PER_DB]
    out: List[Dict[str, Any]] = []
    for row in raw:
        out.append({name: row[name] for name in _SESSION_COLUMNS})
    return out


def _build_session(
    conn: sqlite3.Connection,
    db_path: Path,
    row: Dict[str, Any],
    mark_unknown: Any,
    extractor: OpenCodeExtractor,
) -> Optional[UnifiedSession]:
    session_id = row.get("id")
    if not isinstance(session_id, str) or not session_id:
        return None

    fallback = datetime.fromtimestamp(db_path.stat().st_mtime)
    created_at = _ms_to_datetime(row.get("time_created"), fallback)
    updated_at = _ms_to_datetime(row.get("time_updated"), created_at)

    messages = _load_messages(conn, session_id, mark_unknown, extractor, fallback)
    if not messages:
        return None

    parent_id = row.get("parent_id")
    parent_session_id: Optional[str] = None
    relation: Optional[str] = None
    origin = "interactive"
    if isinstance(parent_id, str) and parent_id.strip():
        parent_session_id = parent_id.strip()
        relation = "subagent"
        origin = "subagent"

    project_path = _clean_directory(row.get("directory"))
    raw_title = row.get("title")
    title = raw_title[:_MAX_TITLE_CHARS] if isinstance(raw_title, str) else None
    cli_version = row.get("version") if isinstance(row.get("version"), str) else None
    project_hash = row.get("project_id") if isinstance(row.get("project_id"), str) else None

    return UnifiedSession(
        tool=Tool.OPENCODE,
        session_id=session_id,
        created_at=created_at,
        last_updated=updated_at,
        messages=messages,
        project_path=project_path,
        project_hash=project_hash,
        thread_id=make_thread_id(project_path=project_path),
        title=title or None,
        cli_version=cli_version or None,
        source_path=str(db_path),
        origin=origin,
        parent_session_id=parent_session_id,
        relation=relation,
    )


def _load_messages(
    conn: sqlite3.Connection,
    session_id: str,
    mark_unknown: Any,
    extractor: OpenCodeExtractor,
    fallback: datetime,
) -> List[UnifiedMessage]:
    try:
        cur = conn.execute(
            """
            SELECT id, type, seq, time_created, time_updated, data
            FROM session_message
            WHERE session_id = ?
              AND length(data) <= ?
            ORDER BY seq
            LIMIT ?
            """,
            (session_id, _MAX_DATA_BYTES, _MAX_MESSAGES_PER_SESSION + 1),
        )
        messages: List[UnifiedMessage] = []
        fetched_bytes = 0
        for index, row in enumerate(cur):
            if index >= _MAX_MESSAGES_PER_SESSION:
                extractor._count("records_unknown")
                break
            fetched_bytes += len(row["data"] or "")
            if fetched_bytes > _MAX_FETCH_BYTES:  # bound memory per session
                extractor._count("records_unknown")
                break
            extras: List[UnifiedMessage] = []
            msg = _parse_message_row(row, mark_unknown, fallback, extras)
            if msg is not None:
                messages.append(msg)
            messages.extend(extras)
    except sqlite3.Error:
        return []
    return messages


def _parse_message_row(
    row: sqlite3.Row,
    mark_unknown: Any,
    fallback: datetime,
    extras: List[UnifiedMessage],
) -> Optional[UnifiedMessage]:
    msg_id = row["id"]
    msg_type = row["type"]
    if not isinstance(msg_type, str):
        return None
    if msg_type in _IGNORED_MESSAGE_TYPES:
        return None

    try:
        payload = json.loads(str(row["data"] or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    row_created = row["time_created"]
    message_id = msg_id if isinstance(msg_id, str) else None

    if msg_type == "user":
        text = payload.get("text")
        content = text if isinstance(text, str) else ""
        if not content.strip():
            return None
        ts = _message_time(payload, row_created, fallback)
        return UnifiedMessage(
            role=Role.USER,
            content=_cap(content, _MAX_CONTENT_CHARS),
            timestamp=ts,
            message_id=message_id,
        )

    if msg_type in ("synthetic", "system"):
        text = payload.get("text")
        content = text if isinstance(text, str) else ""
        ts = _message_time(payload, row_created, fallback)
        return UnifiedMessage(
            role=Role.INFO,
            content=_cap(content, _MAX_CONTENT_CHARS),
            timestamp=ts,
            message_id=message_id,
            kind="injected_context",
        )

    if msg_type == "skill":
        text = payload.get("text")
        if not isinstance(text, str):
            name = payload.get("name")
            text = name if isinstance(name, str) else ""
        ts = _message_time(payload, row_created, fallback)
        return UnifiedMessage(
            role=Role.INFO,
            content=_cap(text, _MAX_CONTENT_CHARS),
            timestamp=ts,
            message_id=message_id,
            kind="injected_context",
        )

    if msg_type == "assistant":
        return _parse_assistant(payload, row_created, fallback, message_id, extras)

    if msg_type == "compaction":
        status = payload.get("status")
        if status != "completed":
            return None
        summary = payload.get("summary")
        text = summary if isinstance(summary, str) else ""
        ts = _message_time(payload, row_created, fallback)
        return UnifiedMessage(
            role=Role.INFO,
            content=_cap(text, _MAX_CONTENT_CHARS),
            timestamp=ts,
            message_id=message_id,
            kind="compaction_summary",
        )

    if msg_type == "shell":
        command = payload.get("command")
        if not isinstance(command, str):
            return None
        cmd_display = _cap(command, 200)
        output = payload.get("output")
        out_text = output if isinstance(output, str) else ""
        body = f"[Shell: {cmd_display}]"
        if out_text:
            body = f"{body}\n{_cap(out_text, _MAX_SHELL_OUTPUT_CHARS)}"
        ts = _message_time(payload, row_created, fallback)
        return UnifiedMessage(
            role=Role.TOOL,
            content=_cap(body, _MAX_CONTENT_CHARS),
            timestamp=ts,
            message_id=message_id,
        )

    mark_unknown(msg_type)
    return None


def _parse_assistant(
    payload: Dict[str, Any],
    row_created: Any,
    fallback: datetime,
    message_id: Optional[str],
    extras: List[UnifiedMessage],
) -> Optional[UnifiedMessage]:
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []

    content_blocks = payload.get("content")
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            b_type = block.get("type")
            if b_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif b_type == "reasoning":
                text = block.get("text")
                if isinstance(text, str) and text:
                    reasoning_parts.append(text)
            elif b_type == "tool":
                tool_name = block.get("name")
                name = tool_name if isinstance(tool_name, str) and tool_name else "tool"
                state = block.get("state") if isinstance(block.get("state"), dict) else {}
                tool_input = state.get("input") if isinstance(state, dict) else None
                suffix = _render_tool_input(tool_input)
                text_parts.append(f"[Tool: {name}]{suffix}")
                status_raw = state.get("status") if isinstance(state, dict) else "unknown"
                status_str = status_raw if isinstance(status_raw, str) else str(status_raw)
                event_status, is_error = _tool_event_status(status_str)
                duration_ms = _tool_duration_ms(block.get("time"))
                output = _tool_output_text(state)
                if output:
                    extras.append(
                        UnifiedMessage(
                            role=Role.TOOL,
                            content=output,
                            timestamp=_message_time(payload, row_created, fallback),
                            message_id=message_id,
                        )
                    )
                tool_events.append(
                    {
                        "name": name,
                        "status": event_status,
                        "duration_ms": duration_ms,
                        "is_error": is_error,
                    }
                )

    content = "\n".join(text_parts)
    reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
    if reasoning:
        reasoning = _cap(reasoning, _MAX_REASONING_CHARS)

    if not text_parts and not reasoning_parts and not tool_events:
        return None
    model = _format_model(payload.get("model"))
    tokens = _normalize_tokens(payload.get("tokens"))
    ts = _message_time(payload, row_created, fallback)

    return UnifiedMessage(
        role=Role.ASSISTANT,
        content=_cap(content, _MAX_CONTENT_CHARS),
        timestamp=ts,
        message_id=message_id,
        model=model,
        tokens=tokens,
        reasoning=reasoning,
        tool_events=tool_events,
    )


def _tool_event_status(status: str) -> Tuple[str, bool]:
    if status == "completed":
        return "completed", False
    if status == "error":
        return "failed", True
    return ("running" if status in ("running", "streaming") else "unknown"), False


def _tool_output_text(state: Any) -> str:
    """Bounded text of a finished tool call (`state.content` is a list of content blocks)."""
    if not isinstance(state, dict):
        return ""
    parts: List[str] = []
    content = state.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    error = state.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        parts.append(error["message"])
    return _cap("\n".join(p for p in parts if p), _MAX_TOOL_OUTPUT_CHARS)


def _tool_duration_ms(time_block: Any) -> Optional[int]:
    if not isinstance(time_block, dict):
        return None
    ran = time_block.get("ran")
    completed = time_block.get("completed")
    if not isinstance(ran, (int, float)) or not isinstance(completed, (int, float)):
        return None
    try:
        if not math.isfinite(ran) or not math.isfinite(completed):
            return None
        delta = int(completed) - int(ran)
    except (OverflowError, ValueError):
        return None
    return delta if delta >= 0 else None


def _render_tool_input(tool_input: Any) -> str:
    rendered: List[str] = []
    if isinstance(tool_input, dict):
        for key, value in tool_input.items():
            if not isinstance(key, str) or is_secret_field_name(key):
                continue
            if isinstance(value, dict):
                rendered.append(f"{key}=<dict>")
                continue
            if isinstance(value, list):
                rendered.append(f"{key}=<list>")
                continue
            v_str = str(value)
            if len(v_str) > _MAX_TOOL_ARG_CHARS:
                v_str = v_str[:_MAX_TOOL_ARG_CHARS]
            rendered.append(f"{key}={v_str}")
    return (" " + " ".join(rendered)) if rendered else ""


def _format_model(model: Any) -> Optional[str]:
    if not isinstance(model, dict):
        return None
    provider = model.get("providerID")
    mid = model.get("id")
    if isinstance(provider, str) and isinstance(mid, str) and provider and mid:
        return f"{provider}/{mid}"
    if isinstance(mid, str) and mid:
        return mid
    return None


def _normalize_tokens(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict):
        return None
    out: Dict[str, int] = {}
    for src, dst in (("input", "input"), ("output", "output"), ("reasoning", "reasoning")):
        val = raw.get(src)
        if isinstance(val, int):
            out[dst] = val
    cache = raw.get("cache")
    if isinstance(cache, dict):
        for src, dst in (("read", "cache_read"), ("write", "cache_write")):
            val = cache.get(src)
            if isinstance(val, int):
                out[dst] = val
    return out or None


def _message_time(payload: Dict[str, Any], row_created: Any, fallback: datetime) -> datetime:
    time_block = payload.get("time")
    if isinstance(time_block, dict):
        created = time_block.get("created")
        if created is not None:
            return _ms_to_datetime(created, fallback)
    return _ms_to_datetime(row_created, fallback)


def _ms_to_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        if not math.isfinite(value):  # huge ints raise OverflowError here
            return fallback
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError):
        return fallback


def _clean_directory(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_PATH_CHARS:
        return None
    if any(c in value for c in "\x00\r\n") or ".." in Path(value).parts:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return value


def _cap(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len]
