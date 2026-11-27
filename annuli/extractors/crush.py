"""Crush agent extractor — ``crush.db`` via ``projects.json`` (issue #197 / B2.8c)."""

from __future__ import annotations

import hashlib
import json
import logging
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

_MAX_REGISTRY_ENTRIES = 200

_SESSION_FIELDS = (
    "id",
    "parent_session_id",
    "title",
    "created_at",
    "updated_at",
)

_MESSAGE_FIELDS = (
    "id",
    "session_id",
    "role",
    "parts",
    "model",
    "created_at",
    "is_summary_message",
)

_IGNORED_PART_TYPES = frozenset({"finish", "image_url", "binary"})


_MAX_REGISTRY_BYTES = 1024 * 1024


class CrushExtractor(BaseExtractor):
    """Reads ``crush.db`` files listed in ``~/.local/share/crush/projects.json``."""

    tool = Tool.CRUSH
    PARSER_VERSION = 1

    def __init__(self, crush_share: Optional[Path] = None) -> None:
        self.crush_share = crush_share
        self.record_counts: Dict[str, int] = {}

    def source_description(self) -> str:
        return "~/.local/share/crush/projects.json → <project>/.crush/crush.db"

    def is_available(self) -> bool:
        return bool(self._discover_databases())

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n

    def _share_root(self) -> Path:
        if self.crush_share is not None:
            return self.crush_share
        return Path.home() / ".local" / "share" / "crush"

    def _discover_databases(self) -> List[Tuple[Path, Path]]:
        """Return ``(project_path, crush.db path)`` pairs."""
        registry = self._share_root() / "projects.json"
        if not registry.is_file() or registry.is_symlink():
            return []
        try:
            if registry.stat().st_size > _MAX_REGISTRY_BYTES:
                return []
            raw = registry.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
            return []
        if not isinstance(data, dict):
            return []
        projects = data.get("projects")
        if not isinstance(projects, list):
            return []

        found: List[Tuple[Path, Path]] = []
        for entry in projects[:_MAX_REGISTRY_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            project_raw = entry.get("path")
            data_dir_raw = entry.get("data_dir")
            if not isinstance(project_raw, str) or not isinstance(data_dir_raw, str):
                continue
            project_path = _resolve_registry_path(project_raw)
            if project_path is None:
                continue
            try:
                if project_path.is_symlink():
                    continue
            except OSError:
                continue
            data_path = _resolve_data_dir(project_path, data_dir_raw)
            if data_path is None:
                continue
            db_path = data_path / "crush.db"
            if db_path.name != "crush.db":
                continue
            if not is_plain_db(db_path):
                continue
            try:
                if data_path.is_symlink():
                    continue
            except OSError:
                continue
            found.append((project_path, db_path))
        return found

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        databases = self._discover_databases()
        if not databases:
            return

        id_dbs: Dict[str, Set[str]] = {}
        for _project, db_path in databases:
            db_key = str(db_path.resolve())
            for sid in self._list_session_ids(db_path):
                id_dbs.setdefault(sid, set()).add(db_key)

        qualify_ids = {sid for sid, dbs in id_dbs.items() if len(dbs) > 1}

        for project_path, db_path in databases:
            if db_path.is_symlink():
                self._count_record("skipped_databases")
                continue
            db_key = str(db_path.resolve())
            short_hash = hashlib.sha256(db_key.encode("utf-8")).hexdigest()[:8]
            for session in self._parse_database(db_path, project_path, qualify_ids, short_hash):
                if self.should_import_session(session):
                    yield session

    def _list_session_ids(self, db_path: Path) -> List[str]:
        ids: List[str] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), MAX_SQLITE_BYTES)
                if copy_path is None:
                    return []
                conn = open_readonly_copy(copy_path)
                if conn is None:
                    return []
                try:
                    cols = table_columns(conn, "sessions")
                    if "id" not in cols:
                        return []
                    cur = conn.execute(f"SELECT id FROM sessions LIMIT {MAX_SESSIONS_PER_DB + 1}")
                    for row in cur:
                        sid = row[0]
                        if isinstance(sid, str) and sid:
                            ids.append(sid)
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError):
            return []
        return ids

    def _parse_database(
        self,
        db_path: Path,
        project_path: Path,
        qualify_ids: Set[str],
        short_hash: str,
    ) -> Iterator[UnifiedSession]:
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
                    if "session_id" not in message_cols or "parts" not in message_cols:
                        self._count_record("skipped_databases")
                        return
                    self._count("files_parsed")
                    rows = self._load_sessions(conn, session_cols)
                    if rows is None:
                        self._count_record("skipped_databases")
                        return
                    for row in rows:
                        session = self._build_session(
                            db_path,
                            project_path,
                            row,
                            conn,
                            message_cols,
                            qualify_ids,
                            short_hash,
                        )
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
        return [{name: row[name] for name in _SESSION_FIELDS} for row in raw]

    def _qualify_id(self, session_id: str, qualify_ids: Set[str], short_hash: str) -> str:
        if session_id in qualify_ids:
            return f"{short_hash}:{session_id}"
        return session_id

    def _build_session(
        self,
        db_path: Path,
        project_path: Path,
        row: Dict[str, Any],
        conn: sqlite3.Connection,
        message_cols: Set[str],
        qualify_ids: Set[str],
        short_hash: str,
    ) -> Optional[UnifiedSession]:
        raw_id = row.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            return None
        session_id = self._qualify_id(raw_id, qualify_ids, short_hash)

        fallback = datetime.fromtimestamp(db_path.stat().st_mtime)
        created_at = safe_ts_any(row.get("created_at"), fallback)
        updated_at = safe_ts_any(row.get("updated_at"), created_at)

        messages = self._load_messages(conn, message_cols, raw_id, fallback)
        if not messages:
            return None

        raw_parent = row.get("parent_session_id")
        parent_session_id: Optional[str] = None
        relation: Optional[str] = None
        origin = "interactive"
        if isinstance(raw_parent, str) and raw_parent.strip():
            parent_session_id = self._qualify_id(raw_parent.strip(), qualify_ids, short_hash)
            relation = "subagent"
            origin = "subagent"

        registry_project = clean_absolute_path(str(project_path))
        raw_title = row.get("title")
        title: Optional[str] = None
        if isinstance(raw_title, str) and raw_title.strip():
            title = cap_text(raw_title.strip(), MAX_TITLE_CHARS)

        return UnifiedSession(
            tool=Tool.CRUSH,
            session_id=session_id,
            created_at=created_at,
            last_updated=updated_at,
            messages=messages,
            project_path=registry_project,
            thread_id=make_thread_id(project_path=registry_project),
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            source_path=f"{db_path}#{raw_id}",
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
    ) -> List[UnifiedMessage]:
        select = ", ".join(select_column(columns, name) for name in _MESSAGE_FIELDS)
        try:
            cur = conn.execute(
                f"""
                SELECT {select} FROM messages
                WHERE session_id = ?
                  AND (parts IS NULL OR length(parts) <= ?)
                ORDER BY created_at, rowid
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
            payload = row["parts"]
            if isinstance(payload, str):
                if len(payload) > MAX_DATA_BYTES:
                    self._count_record("skipped_oversize")
                    continue
                fetched_bytes += len(payload)
            if fetched_bytes > MAX_FETCH_BYTES:
                self._count_record("skipped_oversize")
                break

            ts = safe_ts_any(row["created_at"], fallback)
            model = row["model"] if isinstance(row["model"], str) else None
            is_summary = row["is_summary_message"] if "is_summary_message" in columns else None

            parts = _parse_parts(payload)
            if parts is None:
                if payload not in (None, ""):
                    self._count_record("skipped_oversize")
                continue

            if is_summary in (1, True):
                text = _summary_text(parts)
                if text:
                    messages.append(
                        UnifiedMessage(
                            role=Role.INFO,
                            content=cap_text(text, MAX_CONTENT_CHARS),
                            timestamp=ts,
                            kind="compaction_summary",
                        )
                    )
                continue

            role_raw = row["role"]
            if not isinstance(role_raw, str):
                self._count("records_unknown")
                continue

            parsed = _parse_crush_parts(
                parts,
                role_raw,
                ts,
                model if role_raw == "assistant" else None,
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


def _resolve_registry_path(raw: str) -> Optional[Path]:
    raw = raw.strip()
    if not raw or ".." in Path(raw).parts:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    return path


def _resolve_data_dir(project_path: Path, data_dir_raw: str) -> Optional[Path]:
    data_dir_raw = data_dir_raw.strip()
    if not data_dir_raw or ".." in Path(data_dir_raw).parts:
        return None
    candidate = Path(data_dir_raw)
    resolved = candidate if candidate.is_absolute() else project_path / candidate
    try:
        if resolved.is_symlink():
            return None
    except OSError:
        return None
    return resolved


def _parse_parts(raw: Any) -> Optional[List[Any]]:
    if raw is None:
        return []
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


def _summary_text(parts: List[Any]) -> str:
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        data = part.get("data")
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"]
    return ""


@dataclass
class _CrushParsed:
    main: Optional[UnifiedMessage] = None
    extras: List[UnifiedMessage] = field(default_factory=list)
    tool_call_ids: List[Tuple[str, int]] = field(default_factory=list)
    tool_updates: List[Tuple[str, str, bool]] = field(default_factory=list)


def _parse_crush_parts(
    parts: List[Any],
    role_raw: str,
    ts: datetime,
    model_name: Optional[str],
    unknown_types: Set[str],
) -> _CrushParsed:
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    extras: List[UnifiedMessage] = []
    tool_call_ids: List[Tuple[str, int]] = []
    tool_updates: List[Tuple[str, str, bool]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if not isinstance(ptype, str):
            continue
        raw_data = part.get("data")
        data: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}

        if ptype == "text":
            text = data.get("text")
            hidden = data.get("hidden")
            if isinstance(text, str) and text:
                if hidden in (True, 1):
                    extras.append(
                        UnifiedMessage(
                            role=Role.INFO,
                            content=cap_text(text, MAX_CONTENT_CHARS),
                            timestamp=ts,
                            kind="injected_context",
                        )
                    )
                else:
                    text_parts.append(text)
        elif ptype == "reasoning":
            thinking = data.get("thinking")
            if isinstance(thinking, str) and thinking:
                reasoning_parts.append(thinking)
        elif ptype == "tool_call":
            call_id = data.get("id")
            name = data.get("name") if isinstance(data.get("name"), str) else "tool"
            tool_input = data.get("input")
            args_suffix = _crush_tool_input(tool_input)
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
        elif ptype == "tool_result":
            call_id = data.get("tool_call_id")
            content = data.get("content")
            body = content if isinstance(content, str) else ""
            if body:
                extras.append(
                    UnifiedMessage(
                        role=Role.TOOL,
                        content=cap_text(body, MAX_TOOL_OUTPUT_CHARS),
                        timestamp=ts,
                    )
                )
            status, is_error = tool_result_status(
                "error" if data.get("is_error") in (True, 1) else "success",
                data.get("is_error"),
            )
            if isinstance(call_id, str) and call_id:
                tool_updates.append((call_id, status, is_error))
        elif ptype == "shell_command":
            command = data.get("command")
            cmd = command if isinstance(command, str) else ""
            cmd_display = cap_text(cmd, 200)
            output = data.get("output")
            out_text = output if isinstance(output, str) else ""
            body = f"[Shell: {cmd_display}]"
            if out_text:
                body = f"{body}\n{cap_text(out_text, MAX_TOOL_OUTPUT_CHARS)}"
            extras.append(
                UnifiedMessage(
                    role=Role.TOOL,
                    content=cap_text(body, MAX_CONTENT_CHARS),
                    timestamp=ts,
                )
            )
        elif ptype in _IGNORED_PART_TYPES:
            continue
        elif ptype not in unknown_types:
            unknown_types.add(ptype)

    content = "\n".join(text_parts)
    reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
    if reasoning:
        reasoning = cap_text(reasoning, MAX_REASONING_CHARS)

    main: Optional[UnifiedMessage] = None
    if role_raw == "user" and content.strip():
        main = UnifiedMessage(
            role=Role.USER,
            content=cap_text(content, MAX_CONTENT_CHARS),
            timestamp=ts,
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

    return _CrushParsed(
        main=main,
        extras=extras,
        tool_call_ids=tool_call_ids,
        tool_updates=tool_updates,
    )


def _crush_tool_input(raw: Any) -> str:
    if not isinstance(raw, str):
        return render_tool_arguments(raw, is_secret_field_name)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return ""  # malformed input may hold a truncated secret: render nothing
    return render_tool_arguments(parsed, is_secret_field_name)
