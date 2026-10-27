"""Zed agent thread extractor — ``threads.db`` (issue #197 / B2.8e)."""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from .base import BaseExtractor
from .cursor_agent import is_secret_field_name
from .sqlite_common import (
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
_MAX_COMPRESSED_BLOB = 8 * 1024 * 1024
_MAX_DECODED_JSON = 32 * 1024 * 1024
_MAX_COMPACTION_CHARS = 100_000
_MAX_CONTENT_CHARS = 100_000

_THREAD_FIELDS = (
    "id",
    "summary",
    "updated_at",
    "data_type",
    "data",
    "parent_id",
    "folder_paths",
    "created_at",
)


class ZedExtractor(BaseExtractor):
    """Reads Zed agent threads via scratch copies of ``threads.db``."""

    tool = Tool.ZED
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, db_paths: Optional[List[Path]] = None) -> None:
        self.db_paths = db_paths
        self.record_counts: Dict[str, int] = {}
        self._unknown_tags: Set[str] = set()

    def source_description(self) -> str:
        return (
            "~/.local/share/zed/threads/threads.db, "
            "$XDG_DATA_HOME/zed/threads/threads.db "
            "(zstd rows need optional ``zstandard``)"
        )

    def is_available(self) -> bool:
        return bool(self._zed_db_paths())

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n

    def _zed_db_paths(self) -> List[Path]:
        if self.db_paths is not None:
            return list(self.db_paths)
        home = Path.home()
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        candidates = [
            home / ".local" / "share" / "zed" / "threads" / "threads.db",
            xdg / "zed" / "threads" / "threads.db",
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

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        for db_path in self._zed_db_paths():
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
                    columns = table_columns(conn, "threads")
                    if "id" not in columns or "data" not in columns:
                        self._count_record("skipped_databases")
                        return
                    self._count("files_parsed")
                    rows = self._load_threads(conn, columns)
                    if rows is None:
                        self._count_record("skipped_databases")
                        return
                    for row in rows:
                        session = self._build_session(db_path, row, columns)
                        if session is not None:
                            self._count("files_matched")
                            yield session
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._count_record("skipped_databases")

    def _load_threads(
        self, conn: sqlite3.Connection, columns: Set[str]
    ) -> Optional[List[Dict[str, Any]]]:
        select = ", ".join(
            select_column(columns, name, large_json=name == "data") for name in _THREAD_FIELDS
        )
        order = "ORDER BY updated_at DESC" if "updated_at" in columns else "ORDER BY rowid DESC"
        try:
            cur = conn.execute(
                f"SELECT {select} FROM threads {order} LIMIT ?",
                (MAX_SESSIONS_PER_DB + 1,),
            )
            out: List[Dict[str, Any]] = []
            kept_bytes = 0
            for index, row in enumerate(cur):  # incremental: no fetchall of every blob
                if index >= MAX_SESSIONS_PER_DB:
                    self._count_record("skipped_oversize")
                    break
                data = row["data"]
                kept_bytes += len(data) if isinstance(data, (bytes, str)) else 0
                if kept_bytes > MAX_FETCH_BYTES:  # bound the compressed blobs held at once
                    self._count_record("skipped_oversize")
                    break
                out.append({name: row[name] for name in _THREAD_FIELDS})
        except sqlite3.Error:
            return None
        return out

    def _build_session(
        self,
        db_path: Path,
        row: Dict[str, Any],
        columns: Set[str],
    ) -> Optional[UnifiedSession]:
        thread_id = row.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            return None
        fallback = datetime.fromtimestamp(db_path.stat().st_mtime)  # naive local, like the rest
        created_at = (
            safe_ts_any(row.get("created_at"), fallback) if "created_at" in columns else fallback
        )
        updated_at = safe_ts_any(row.get("updated_at"), created_at)

        payload = self._decode_thread_blob(row.get("data_type"), row.get("data"))
        if payload is None:
            return None
        if isinstance(payload.get("updated_at"), str):
            updated_at = safe_ts_any(payload["updated_at"], updated_at)

        project_path = _first_folder_path(row.get("folder_paths"))
        messages = _parse_thread_messages(
            payload,
            updated_at,
            self._unknown_tags,
            lambda: self._count("records_unknown"),
        )
        if not messages:
            return None
        if len(messages) > MAX_MESSAGES_PER_SESSION:
            messages = messages[:MAX_MESSAGES_PER_SESSION]
            self._count_record("skipped_oversize")

        tokens = _usage_tokens(payload.get("cumulative_token_usage"))
        model_label = _model_label(payload.get("model"))
        for msg in reversed(messages):
            if msg.role == Role.ASSISTANT:
                if tokens:
                    msg.tokens = tokens
                if model_label:
                    msg.model = model_label
                break

        raw_parent = row.get("parent_id")
        parent_session_id: Optional[str] = None
        relation: Optional[str] = None
        origin = "interactive"
        if isinstance(raw_parent, str) and raw_parent.strip():
            parent_session_id = raw_parent.strip()
            relation = "subagent"
            origin = "subagent"

        title = _thread_title(row.get("summary"), payload.get("title"))

        return UnifiedSession(
            tool=Tool.ZED,
            session_id=thread_id,
            created_at=created_at,
            last_updated=updated_at,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            source_path=f"{db_path}#{thread_id}",
            origin=origin,
            parent_session_id=parent_session_id,
            relation=relation,
            parser_version=self.PARSER_VERSION,
        )

    def _decode_thread_blob(self, data_type: Any, data: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(data_type, str):
            return None
        if data is None:
            return None
        raw: Optional[bytes]
        if isinstance(data, memoryview):
            raw = bytes(data)
        elif isinstance(data, bytes):
            raw = data
        elif isinstance(data, str):
            raw = data.encode("utf-8", errors="replace")
        else:
            return None

        decoded: Optional[bytes]
        if data_type == "json":
            decoded = raw
        elif data_type == "zstd":
            if raw is None or len(raw) > _MAX_COMPRESSED_BLOB:
                self._count_record("skipped_oversize")
                return None
            try:
                zstd = importlib.import_module("zstandard")
            except ImportError:
                # One warning per run: the per-run counter resets in extract_sessions().
                if not self.discovery.get("skipped_missing_zstd"):
                    logger.warning(
                        "Skipping zstd-compressed Zed thread rows: the 'zstandard' package is "
                        "not installed. Install it with: pip install 'lore[zstd]' "
                        "(or: pipx inject lore zstandard)"
                    )
                self._count("skipped_missing_zstd")
                return None
            decoded = _decompress_zstd(raw, zstd)
            if decoded is None:
                self._count_record("skipped_oversize")
                return None
        else:
            return None

        if decoded is None or len(decoded) > _MAX_DECODED_JSON:
            self._count_record("skipped_oversize")
            return None
        try:
            parsed = json.loads(decoded)
        except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
            return None
        return parsed if isinstance(parsed, dict) else None


def _decompress_zstd(data: bytes, zstd: Any) -> Optional[bytes]:
    out = io.BytesIO()
    decoded = 0
    try:
        reader = zstd.ZstdDecompressor().stream_reader(io.BytesIO(data))
        while True:
            chunk = reader.read(65536)
            if not chunk:
                break
            decoded += len(chunk)
            if decoded > _MAX_DECODED_JSON:
                return None
            out.write(chunk)
    except (OSError, ValueError, EOFError, RuntimeError):
        return None
    return out.getvalue()


def _first_folder_path(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    for line in raw.splitlines():
        path = clean_absolute_path(line.strip())
        if path is not None:
            return path
    return None


def _thread_title(summary: Any, json_title: Any) -> Optional[str]:
    if isinstance(summary, str) and summary.strip():
        return cap_text(summary.strip(), MAX_TITLE_CHARS)
    if isinstance(json_title, str) and json_title.strip():
        return cap_text(json_title.strip(), MAX_TITLE_CHARS)
    return None


def _model_label(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    model = raw.get("model")
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        return f"{provider}/{model}"
    if isinstance(model, str) and model:
        return model
    return None


def _usage_tokens(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict):
        return None
    out: Dict[str, int] = {}
    mapping = (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache_read"),
        ("cache_creation_input_tokens", "cache_write"),
    )
    for src, dst in mapping:
        val = raw.get(src)
        if isinstance(val, int) and not isinstance(val, bool):
            out[dst] = val
    return out or None


def _parse_thread_messages(
    payload: Dict[str, Any],
    msg_ts: datetime,
    unknown_tags: Set[str],
    count_unknown: Any,
) -> List[UnifiedMessage]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return []
    messages: List[UnifiedMessage] = []
    for item in raw_messages:
        if item == "Resume":
            continue
        if not isinstance(item, dict) or len(item) != 1:
            _mark_unknown("?", unknown_tags, count_unknown)
            continue
        tag, body = next(iter(item.items()))
        if tag == "User":
            text = _user_text(body)
            if text:
                messages.append(
                    UnifiedMessage(
                        role=Role.USER, content=cap_text(text, _MAX_CONTENT_CHARS), timestamp=msg_ts
                    )
                )
        elif tag == "Agent":
            agent_msgs = _agent_messages(body, msg_ts, unknown_tags, count_unknown)
            messages.extend(agent_msgs)
        elif tag == "Compaction":
            if isinstance(body, dict) and "Summary" in body:
                summary = body.get("Summary")
                if isinstance(summary, str) and summary:
                    messages.append(
                        UnifiedMessage(
                            role=Role.INFO,
                            content=cap_text(summary, _MAX_COMPACTION_CHARS),
                            timestamp=msg_ts,
                            kind="compaction_summary",
                        )
                    )
        else:
            _mark_unknown(tag, unknown_tags, count_unknown)
        if len(messages) >= MAX_MESSAGES_PER_SESSION:
            break
    return messages


def _mark_unknown(tag: str, unknown_tags: Set[str], count_unknown: Any) -> None:
    if tag not in unknown_tags:
        unknown_tags.add(tag)
        count_unknown()


def _user_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    parts: List[str] = []
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    for part in content:
        if not isinstance(part, dict) or len(part) != 1:
            continue
        kind, value = next(iter(part.items()))
        if kind == "Text" and isinstance(value, str):
            parts.append(value)
        elif kind == "Mention" and isinstance(value, dict):
            text = value.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _agent_messages(
    body: Any,
    msg_ts: datetime,
    unknown_tags: Set[str],
    count_unknown: Any,
) -> List[UnifiedMessage]:
    if not isinstance(body, dict):
        return []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    tool_results_raw = body.get("tool_results")
    tool_results: Dict[str, Any] = tool_results_raw if isinstance(tool_results_raw, dict) else {}

    content = body.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or len(part) != 1:
                continue
            kind, value = next(iter(part.items()))
            if kind == "Text" and isinstance(value, str):
                text_parts.append(value)
            elif kind == "Thinking" and isinstance(value, dict):
                text = value.get("text")
                if isinstance(text, str):
                    reasoning_parts.append(text)
            elif kind == "RedactedThinking":
                continue
            elif kind == "ToolUse" and isinstance(value, dict):
                name = value.get("name")
                if not isinstance(name, str) or not name:
                    continue
                args = value.get("input")
                suffix = (
                    render_tool_arguments(args, is_secret_field_name)
                    if isinstance(args, dict)
                    else ""
                )
                text_parts.append(f"[Tool: {name}]{suffix}")
                call_id = value.get("id")
                status, is_error = "completed", False
                if isinstance(call_id, str):
                    result = tool_results.get(call_id)
                    if isinstance(result, dict):
                        status, is_error = tool_result_status(None, result.get("is_error"))
                tool_events.append(
                    {"name": name, "status": status, "duration_ms": None, "is_error": is_error}
                )

    reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
    if reasoning and len(reasoning) > MAX_REASONING_CHARS:
        reasoning = reasoning[:MAX_REASONING_CHARS]

    out: List[UnifiedMessage] = []
    content_str = cap_text("\n".join(text_parts), _MAX_CONTENT_CHARS)
    if content_str or reasoning or tool_events:
        out.append(
            UnifiedMessage(
                role=Role.ASSISTANT,
                content=content_str,
                timestamp=msg_ts,
                reasoning=reasoning,
                tool_events=tool_events,
            )
        )

    if isinstance(tool_results_raw, dict):
        for _tid, result in tool_results_raw.items():
            if not isinstance(result, dict):
                continue
            text = _tool_result_text(result.get("content"))
            if text:
                out.append(
                    UnifiedMessage(
                        role=Role.TOOL,
                        content=cap_text(text, MAX_TOOL_OUTPUT_CHARS),
                        timestamp=msg_ts,
                    )
                )
    return out


def _tool_result_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and len(raw) == 1:
        kind, value = next(iter(raw.items()))
        if kind == "Text" and isinstance(value, str):
            return value
    return ""
