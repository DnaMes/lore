"""Goose agent extractor — ``sessions.db`` (issue #197 / B2.8c)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from .base import BaseExtractor
from .cursor_agent import is_secret_field_name
from .sqlite_common import (
    MAX_CONTENT_CHARS,
    MAX_DATA_BYTES,
    MAX_FETCH_BYTES,
    MAX_MESSAGES_PER_SESSION,
    MAX_REASONING_CHARS,
    MAX_SESSIONS_PER_DB,
    MAX_SQLITE_BYTES,
    MAX_TITLE_CHARS,
    MAX_TOOL_OUTPUT_CHARS,
    cap_text,
    clean_absolute_path,
    is_plain_db,
    open_readonly_copy,
    render_tool_arguments,
    safe_ts_any,
    select_column,
    table_columns,
    tool_result_status,
)

logger = logging.getLogger(__name__)

PARSER_VERSION = 1

_SESSION_FIELDS = (
    "id",
    "name",
    "working_dir",
    "created_at",
    "updated_at",
    "session_type",
    "schedule_id",
    "parent_session_id",
    "model_config_json",
)

_MESSAGE_FIELDS = (
    "id",
    "session_id",
    "role",
    "content_json",
    "created_timestamp",
    "timestamp",
    "metadata_json",
)


class GooseExtractor(BaseExtractor):
    """Reads Goose ``sessions.db`` via scratch copies (never opens originals)."""

    tool = Tool.GOOSE
    PARSER_VERSION = 1

    def __init__(self, goose_roots: Optional[List[Path]] = None) -> None:
        self.goose_roots = goose_roots
        self.record_counts: Dict[str, int] = {}

    def source_description(self) -> str:
        return (
            "~/.local/share/goose/sessions/sessions.db, "
            "~/.config/goose/sessions/sessions.db, "
            "$XDG_DATA_HOME/goose/sessions/sessions.db, "
            "~/.local/share/Block/goose/data/sessions/sessions.db"
        )

    def is_available(self) -> bool:
        return bool(self._discover_databases())

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n

    def _goose_db_paths(self) -> List[Path]:
        if self.goose_roots is not None:
            return list(self.goose_roots)
        home = Path.home()
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        candidates = [
            home / ".local" / "share" / "goose" / "sessions" / "sessions.db",
            home / ".config" / "goose" / "sessions" / "sessions.db",
            xdg / "goose" / "sessions" / "sessions.db",
            home / ".local" / "share" / "Block" / "goose" / "data" / "sessions" / "sessions.db",
        ]
        seen: Set[str] = set()
        out: List[Path] = []
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if path.exists() and is_plain_db(path):
                out.append(path)
        return out

    def _discover_databases(self) -> List[Path]:
        return self._goose_db_paths()

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        for db_path in self._discover_databases():
            if db_path.is_symlink():
                self._count_record("skipped_databases")
                continue
            for session in self._parse_database(db_path):
                if self.should_import_session(session):
                    yield session

    def _parse_database(self, db_path: Path) -> Iterator[UnifiedSession]:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), MAX_SQLITE_BYTES)
                if copy_path is None:
                    self._count_record("skipped_databases")
                    return
                conn = open_readonly_copy(copy_path)
                if conn is None:
                    self._count_record("skipped_databases")
                    return
                try:
                    session_cols = table_columns(conn, "sessions")
                    if "id" not in session_cols:
                        self._count_record("skipped_databases")
                        return
                    message_cols = table_columns(conn, "messages")
                    if "session_id" not in message_cols or "content_json" not in message_cols:
                        self._count_record("skipped_databases")
                        return
                    self._count("files_parsed")
                    rows = self._load_sessions(conn, session_cols)
                    if rows is None:
                        self._count_record("skipped_databases")
                        return
                    for row in rows:
                        session = self._build_session(db_path, row, conn, message_cols)
                        if session is not None:
                            self._count("files_matched")
                            yield session
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._count_record("skipped_databases")

    def _load_sessions(
        self, conn: sqlite3.Connection, columns: Set[str]
    ) -> Optional[List[Dict[str, Any]]]:
        select = ", ".join(select_column(columns, name) for name in _SESSION_FIELDS)
        try:
            cur = conn.execute(
                f"SELECT {select} FROM sessions ORDER BY id LIMIT ?",
                (MAX_SESSIONS_PER_DB + 1,),
            )
            raw = cur.fetchall()
        except sqlite3.Error:
            return None
        if len(raw) > MAX_SESSIONS_PER_DB:
            self._count_record("skipped_oversize")
            raw = raw[:MAX_SESSIONS_PER_DB]
        out: List[Dict[str, Any]] = []
        for row in raw:
            out.append({name: row[name] for name in _SESSION_FIELDS})
        return out

    def _build_session(
        self,
        db_path: Path,
        row: Dict[str, Any],
        conn: sqlite3.Connection,
        message_cols: Set[str],
    ) -> Optional[UnifiedSession]:
        session_id = row.get("id")
        if not isinstance(session_id, str) or not session_id:
            return None
        fallback = datetime.fromtimestamp(db_path.stat().st_mtime)
        created_at = safe_ts_any(row.get("created_at"), fallback)
        updated_at = safe_ts_any(row.get("updated_at"), created_at)

        model_name = _session_model(row.get("model_config_json"))
        messages = self._load_messages(
            conn,
            message_cols,
            session_id,
            fallback,
            model_name,
        )
        if not messages:
            return None

        raw_parent = row.get("parent_session_id")
        parent_session_id: Optional[str] = None
        relation: Optional[str] = None
        if isinstance(raw_parent, str) and raw_parent.strip():
            parent_session_id = raw_parent.strip()
            relation = "subagent"
            origin = "subagent"
        else:
            origin = _map_origin(row.get("session_type"), row.get("schedule_id"))

        project_path = clean_absolute_path(row.get("working_dir"))
        raw_name = row.get("name")
        title: Optional[str] = None
        if isinstance(raw_name, str) and raw_name.strip():
            title = cap_text(raw_name.strip(), MAX_TITLE_CHARS)

        return UnifiedSession(
            tool=Tool.GOOSE,
            session_id=session_id,
            created_at=created_at,
            last_updated=updated_at,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            source_path=f"{db_path}#{session_id}",
            origin=origin,
            parent_session_id=parent_session_id,
            relation=relation,
        )

    def _load_messages(
        self,
        conn: sqlite3.Connection,
        columns: Set[str],
        session_id: str,
        fallback: datetime,
        model_name: Optional[str],
    ) -> List[UnifiedMessage]:
        order_col = "created_timestamp" if "created_timestamp" in columns else "rowid"
        select = ", ".join(select_column(columns, name) for name in _MESSAGE_FIELDS)
        try:
            cur = conn.execute(
                f"""
                SELECT {select} FROM messages
                WHERE session_id = ?
                  AND (content_json IS NULL OR length(content_json) <= ?)
                ORDER BY {order_col}, rowid
                LIMIT ?
                """,
                (session_id, MAX_DATA_BYTES, MAX_MESSAGES_PER_SESSION + 1),
            )
        except sqlite3.Error:
            return []

        messages: List[UnifiedMessage] = []
        pending_tools: Dict[str, Tuple[int, int]] = {}
        fetched_bytes = 0
        unknown_types: Set[str] = set()

        for index, row in enumerate(cur):
            if index >= MAX_MESSAGES_PER_SESSION:
                self._count_record("skipped_oversize")
                break
            payload = row["content_json"]
            if isinstance(payload, str):
                if len(payload) > MAX_DATA_BYTES:
                    self._count_record("skipped_oversize")
                    continue
                fetched_bytes += len(payload)
            if fetched_bytes > MAX_FETCH_BYTES:
                self._count_record("skipped_oversize")
                break

            role_raw = row["role"]
            if not isinstance(role_raw, str):
                self._count("records_unknown")
                continue

            ts = safe_ts_any(row["created_timestamp"], fallback)
            if ts == fallback:
                ts = safe_ts_any(row["timestamp"], fallback)

            metadata = _parse_json_obj(row["metadata_json"])
            kind = "text"
            if role_raw == "user" and metadata.get("userVisible") is False:
                kind = "injected_context"

            blocks = _parse_content_blocks(payload)
            if blocks is None:
                if payload not in (None, ""):
                    self._count_record("skipped_oversize")
                continue

            parsed = _parse_row_blocks(
                blocks,
                role_raw,
                ts,
                kind,
                model_name if role_raw == "assistant" else None,
                unknown_types,
            )
            if parsed.main is not None:
                if parsed.main.role == Role.ASSISTANT:
                    idx = len(messages)
                    messages.append(parsed.main)
                    for call_id, event_idx in parsed.tool_call_ids:
                        pending_tools[call_id] = (idx, event_idx)
                else:
                    messages.append(parsed.main)
            messages.extend(parsed.extras)

            for call_id, status, is_error in parsed.tool_updates:
                entry = pending_tools.get(call_id)
                if entry is None:
                    continue
                owner_idx, event_idx = entry
                if owner_idx >= len(messages):
                    continue
                owner = messages[owner_idx]
                if (
                    owner.role == Role.ASSISTANT
                    and owner.tool_events
                    and event_idx < len(owner.tool_events)
                ):
                    owner.tool_events[event_idx]["status"] = status
                    owner.tool_events[event_idx]["is_error"] = is_error

        for utype in unknown_types:
            self._count("records_unknown")

        return messages


def _map_origin(session_type: Any, schedule_id: Any) -> str:
    if session_type == "scheduled" or schedule_id is not None:
        return "cron"
    if session_type == "sub_agent":
        return "subagent"
    return "interactive"


def _session_model(raw: Any) -> Optional[str]:
    data = _parse_json_obj(raw) if isinstance(raw, str) else None
    if not isinstance(data, dict):
        return None
    name = data.get("model_name")
    return name if isinstance(name, str) and name else None


def _parse_json_obj(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_content_blocks(raw: Any) -> Optional[List[Any]]:
    if raw is None:
        return []
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return None
    if isinstance(parsed, dict):
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


@dataclass
class _ParsedRow:
    main: Optional[UnifiedMessage] = None
    extras: List[UnifiedMessage] = field(default_factory=list)
    tool_call_ids: List[Tuple[str, int]] = field(default_factory=list)
    tool_updates: List[Tuple[str, str, bool]] = field(default_factory=list)


def _parse_row_blocks(
    blocks: List[Any],
    role_raw: str,
    ts: datetime,
    kind: str,
    model_name: Optional[str],
    unknown_types: Set[str],
) -> _ParsedRow:
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    extras: List[UnifiedMessage] = []
    tool_call_ids: List[Tuple[str, int]] = []
    tool_updates: List[Tuple[str, str, bool]] = []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if not isinstance(btype, str):
            continue
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif btype == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                reasoning_parts.append(thinking)
        elif btype == "redactedThinking":
            continue
        elif btype == "toolRequest":
            call_id = block.get("id")
            tool_call = block.get("toolCall")
            name = "tool"
            args_suffix = ""
            if isinstance(tool_call, dict) and tool_call.get("status") != "error":
                value = tool_call.get("value")
                if isinstance(value, dict):
                    if isinstance(value.get("name"), str):
                        name = value["name"]
                    args_suffix = render_tool_arguments(
                        value.get("arguments"), is_secret_field_name
                    )
            text_parts.append(f"[Tool: {name}]{args_suffix}")
            event_idx = len(tool_events)
            tool_events.append(
                {
                    "name": name,
                    "status": "completed",
                    "duration_ms": None,
                    "is_error": False,
                }
            )
            if isinstance(call_id, str) and call_id:
                tool_call_ids.append((call_id, event_idx))
        elif btype == "toolResponse":
            call_id = block.get("id")
            body = _tool_response_body(block.get("toolResult"))
            if body:
                extras.append(
                    UnifiedMessage(
                        role=Role.TOOL,
                        content=cap_text(body, MAX_TOOL_OUTPUT_CHARS),
                        timestamp=ts,
                    )
                )
            status, is_error = _goose_tool_status(block.get("toolResult"))
            if isinstance(call_id, str) and call_id:
                tool_updates.append((call_id, status, is_error))
        elif btype == "systemNotification":
            msg = block.get("msg")
            if isinstance(msg, str) and msg.strip():
                extras.append(
                    UnifiedMessage(
                        role=Role.INFO,
                        content=cap_text(msg, MAX_CONTENT_CHARS),
                        timestamp=ts,
                        kind="injected_context",
                    )
                )
        elif btype in ("image", "document", "toolConfirmationRequest", "actionRequired"):
            continue
        elif btype == "error":
            err = block.get("text") or block.get("message") or block.get("error")
            if isinstance(err, str) and err.strip():
                extras.append(
                    UnifiedMessage(
                        role=Role.INFO,
                        content=cap_text(err, MAX_CONTENT_CHARS),
                        timestamp=ts,
                        kind="injected_context",
                    )
                )
        elif btype not in unknown_types:
            unknown_types.add(btype)

    content = "\n".join(text_parts)
    reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
    if reasoning:
        reasoning = cap_text(reasoning, MAX_REASONING_CHARS)

    main: Optional[UnifiedMessage] = None
    if role_raw == "user" and (content.strip() or kind == "injected_context"):
        main = UnifiedMessage(
            role=Role.USER,
            content=cap_text(content, MAX_CONTENT_CHARS),
            timestamp=ts,
            kind=kind,
        )
    elif role_raw == "assistant" and (content.strip() or reasoning or tool_events):
        main = UnifiedMessage(
            role=Role.ASSISTANT,
            content=cap_text(content, MAX_CONTENT_CHARS),
            timestamp=ts,
            reasoning=reasoning,
            tool_events=tool_events,
            model=model_name,
        )

    return _ParsedRow(
        main=main,
        extras=extras,
        tool_call_ids=tool_call_ids,
        tool_updates=tool_updates,
    )


def _goose_tool_status(tool_result: Any) -> Tuple[str, bool]:
    if not isinstance(tool_result, dict):
        return "completed", False
    status = tool_result.get("status")
    is_error = False
    if status == "error":
        return "failed", True
    value = tool_result.get("value")
    if isinstance(value, dict) and value.get("isError") in (True, 1):
        is_error = True
    return tool_result_status(status, is_error)


def _tool_response_body(tool_result: Any) -> str:
    if not isinstance(tool_result, dict):
        return ""
    if tool_result.get("status") == "error":
        err = tool_result.get("error")
        return err if isinstance(err, str) else ""
    value = tool_result.get("value")
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    parts: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)
