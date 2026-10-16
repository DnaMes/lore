"""OpenAI Codex CLI rollout JSONL extractor."""

from __future__ import annotations

import importlib
import io
import json
import logging
import re
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_SQLITE_BYTES = 512 * 1024 * 1024
_SQLITE_TIMEOUT = 2.0
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_TOOL_ARG_CHARS = 200
_MAX_TOOL_OUTPUT_CHARS = 20_000
_MAX_PATH_CHARS = 4096

# Codex wraps injected context in a tag that spans the whole message, and the AGENTS.md
# block starts with a fixed header followed by an <INSTRUCTIONS> section. A real prompt
# that merely mentions "AGENTS.md" or opens a tag must stay a user prompt: hiding it
# would lower user_prompt_count and could drop a genuine session.
_WRAPPED_TAGS = ("environment_context", "user_instructions", "user_action")
_AGENTS_HEADER = re.compile(r"^# AGENTS\.md instructions for \S")


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    try:
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError, RecursionError):
        return fallback


def _clean_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_PATH_CHARS or any(c in value for c in "\x00\r\n"):
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return value


def _clean_call_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _is_injected_user_content(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    lowered = text.lower()
    for tag in _WRAPPED_TAGS:
        if lowered.startswith(f"<{tag}>") and lowered.endswith(f"</{tag}>"):
            return True
    if _AGENTS_HEADER.match(text) and "<instructions>" in lowered:
        return True
    if text.startswith("<!-- BEGIN AGENTS") and "<!-- END AGENTS" in text:
        return True
    return False


def _nonempty_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parent_from_subagent_source(source: Dict[str, Any]) -> Optional[str]:
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    if isinstance(spawn, dict):
        return _nonempty_str(spawn.get("parent_thread_id"))
    return None


def _origin_from_session_meta(metadata: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    """Map Codex session_meta payload to origin, relation, and parent session id."""
    origin = "unknown"
    relation: Optional[str] = None
    parent_session_id: Optional[str] = None

    source = metadata.get("source")
    originator = metadata.get("originator")

    if source == "exec" or originator == "codex_exec":
        origin = "exec"
    elif isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        origin = "subagent"
        relation = "subagent"
        parent_session_id = _parent_from_subagent_source(source)
        if not parent_session_id:
            parent_session_id = _nonempty_str(metadata.get("parent_thread_id"))
    elif source in ("cli", "vscode"):
        origin = "interactive"
    else:
        origin = "unknown"

    forked = metadata.get("forked_from_id")
    if isinstance(forked, str) and forked.strip() and not parent_session_id:
        # A subagent without a recorded spawn parent keeps its subagent relation and
        # falls back to the fork source as parent; anything else is a plain fork.
        if relation is None:
            relation = "fork"
        parent_session_id = forked.strip()

    return origin, relation, parent_session_id


_MAX_THREAD_ROWS = 20_000
_MAX_TITLE_CHARS = 200
_SQLITE_QUERY_SECONDS = 5.0


def _load_thread_titles(codex_base: Path) -> Dict[str, str]:
    """Read thread titles from a scratch copy of state_5.sqlite (never open the original)."""
    db_path = codex_base / "state_5.sqlite"
    titles: Dict[str, str] = {}
    rows: List[Tuple[Any, ...]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), _MAX_SQLITE_BYTES)
            if copy_path is None:
                return {}
            conn = sqlite3.connect(
                f"file:{copy_path}?mode=ro",
                uri=True,
                timeout=_SQLITE_TIMEOUT,
            )
            try:
                # The timeout above only bounds lock waits: stop long-running queries too.
                deadline = time.monotonic() + _SQLITE_QUERY_SECONDS
                conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
                cursor = conn.execute(
                    "SELECT id, rollout_path, substr(title, 1, ?), "
                    "substr(first_user_message, 1, ?) FROM threads LIMIT ?",
                    (_MAX_TITLE_CHARS, _MAX_TITLE_CHARS, _MAX_THREAD_ROWS),
                )
                while len(rows) < _MAX_THREAD_ROWS:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    rows.extend(tuple(r) for r in batch)
            finally:
                conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return {}

    for row in rows:
        if len(row) < 4:
            continue
        thread_id, rollout_path, title, first_user = row[0], row[1], row[2], row[3]
        label = _nonempty_str(title)
        if not label:
            label = _nonempty_str(first_user)
        if not label:
            continue
        if isinstance(thread_id, str) and thread_id:
            titles[thread_id] = label
        if isinstance(rollout_path, str) and rollout_path:
            titles[Path(rollout_path).name] = label
    return titles


def _format_token_usage(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict):
        return None
    tokens: Dict[str, int] = {}
    mapping = (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cached_tokens", "cached"),
        ("reasoning_tokens", "reasoning"),
        ("total_tokens", "total"),
    )
    for src, dst in mapping:
        val = raw.get(src)
        if isinstance(val, (int, float)):
            tokens[dst] = int(val)
    return tokens or None


class CodexExtractor(BaseExtractor):
    """Extract chat history from OpenAI Codex CLI."""

    PARSER_VERSION = 3
    AUTOMATED_BYPASSES_PROMPT_FLOOR = True

    def __init__(self) -> None:
        self.base_path = Path.home() / ".codex"
        self.session_roots = self._discover_session_roots()

    def _discover_session_roots(self) -> List[Path]:
        roots = [self.base_path / "sessions"]
        for candidate in discover_home_marker_paths(".codex/sessions"):
            if candidate not in roots:
                roots.append(candidate)
        return [root for root in roots if root.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.CODEX

    def is_available(self) -> bool:
        if self.session_roots:
            return True
        archived = self.base_path / "archived_sessions"
        return archived.is_dir()

    def source_description(self) -> str:
        return (
            "~/.codex/sessions/*/*/*/rollout-*.jsonl, "
            "~/.codex/archived_sessions/**/rollout-*.jsonl[.zst]"
        )

    @staticmethod
    def _is_plain_file(path: Path) -> bool:
        try:
            return not path.is_symlink() and path.is_file()
        except OSError:
            return False

    def _iter_rollout_paths(self) -> Iterator[Path]:
        for sessions_dir in self.session_roots:
            for year_dir in sessions_dir.iterdir():
                if not year_dir.is_dir():
                    continue
                for month_dir in year_dir.iterdir():
                    if not month_dir.is_dir():
                        continue
                    for day_dir in month_dir.iterdir():
                        if not day_dir.is_dir():
                            continue
                        for pattern in ("rollout-*.jsonl", "rollout-*.jsonl.zst"):
                            for session_file in day_dir.glob(pattern):
                                if self._is_plain_file(session_file):
                                    yield session_file
        archived = self.base_path / "archived_sessions"
        if archived.is_dir():
            for pattern in ("rollout-*.jsonl", "rollout-*.jsonl.zst"):
                for session_file in archived.rglob(pattern):
                    if self._is_plain_file(session_file):
                        yield session_file

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        titles = _load_thread_titles(self.base_path)
        seen_ids: Set[str] = set()
        for session_file in self._iter_rollout_paths():
            self._count("files_matched")
            if self._source_unchanged(session_file):
                continue
            try:
                if session_file.stat().st_size > _MAX_FILE_BYTES:
                    continue
                session = self._parse_session_file(session_file, titles)
                self._count("files_parsed")
                if session.session_id in seen_ids:
                    # The same rollout can exist in sessions/ and archived_sessions/;
                    # live files are listed first and win.
                    self._count("duplicate_sessions")
                    continue
                if self.should_import_session(session):
                    seen_ids.add(session.session_id)
                    yield session
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                OverflowError,
                RecursionError,
            ) as e:
                logger.warning("Failed to parse %s: %s", session_file, type(e).__name__)

    def _open_rollout_lines(self, path: Path) -> Optional[Iterator[str]]:
        """Return a streaming line iterator over a rollout; None when it cannot be read.

        Plain files are read line by line with a per-line cap. ``.jsonl.zst`` files are
        stream-decompressed (optional ``zstandard`` module) with a cap on the decoded size.
        """
        if path.name.endswith(".jsonl.zst"):
            try:
                zstd = importlib.import_module("zstandard")
            except ImportError:
                self._count("skipped_compressed")
                return None
            return self._iter_zst_lines(path, zstd)
        return self._iter_plain_lines(path)

    @staticmethod
    def _iter_plain_lines(path: Path) -> Iterator[str]:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            while True:
                line = handle.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = handle.readline(_MAX_LINE_CHARS)
                    continue
                yield line.rstrip("\n")

    @staticmethod
    def _iter_zst_lines(path: Path, zstd: Any) -> Iterator[str]:
        decoded = 0
        with open(path, "rb") as raw:
            reader = zstd.ZstdDecompressor().stream_reader(raw)
            text = io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
            while True:
                line = text.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                decoded += len(line)
                if decoded > _MAX_FILE_BYTES:
                    return
                if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = text.readline(_MAX_LINE_CHARS)
                        if not line:
                            break
                        decoded += len(line)
                        if decoded > _MAX_FILE_BYTES:
                            return
                    continue
                yield line.rstrip("\n")

    @staticmethod
    def _apply_native_title(session: UnifiedSession, titles: Dict[str, str]) -> None:
        if session.title and session.title.strip():
            return
        label = titles.get(session.session_id)
        if not label and session.source_path:
            label = titles.get(Path(session.source_path).name)
        if label:
            session.title = label
            session.title_source = TitleSource.NATIVE

    def _mark_unknown(self, key: str, seen: Set[str]) -> None:
        if key not in seen:
            seen.add(key)
            self._count("records_unknown")

    def _parse_session_file(
        self, path: Path, titles: Optional[Dict[str, str]] = None
    ) -> UnifiedSession:
        messages: List[UnifiedMessage] = []
        metadata: Dict[str, Any] = {}
        created_at: Optional[datetime] = None
        last_updated: Optional[datetime] = None
        file_mtime = datetime.fromtimestamp(path.stat().st_mtime)
        current_model: Optional[str] = None
        turn_cwd: Optional[str] = None
        pending_calls: Dict[str, List[Tuple[int, str]]] = {}
        last_assistant_idx = -1
        unknown_seen: Set[str] = set()
        session_total_tokens: Optional[int] = None

        def touch_ts(ts: datetime) -> None:
            nonlocal created_at, last_updated
            if created_at is None or ts < created_at:
                created_at = ts
            if last_updated is None or ts > last_updated:
                last_updated = ts

        def append_message(msg: UnifiedMessage) -> int:
            nonlocal last_assistant_idx
            messages.append(msg)
            if msg.role == Role.ASSISTANT:
                last_assistant_idx = len(messages) - 1
            return len(messages) - 1

        def attach_tool_result(
            call_id: Optional[str],
            output_text: str,
            meta: Any,
            ts: datetime,
            default_name: str = "tool",
        ) -> None:
            if output_text:
                append_message(UnifiedMessage(role=Role.TOOL, content=output_text, timestamp=ts))
            queue = pending_calls.get(call_id) if call_id else None
            if not call_id or not queue:
                return
            idx, registered_name = queue.pop(0)
            if not queue:
                del pending_calls[call_id]
            if idx < 0 or idx >= len(messages):
                return
            assistant = messages[idx]
            if assistant.role != Role.ASSISTANT:
                return
            is_error = False
            duration_ms: Optional[int] = None
            if isinstance(meta, dict):
                exit_code = meta.get("exit_code")
                if isinstance(exit_code, (int, float)) and int(exit_code) != 0:
                    is_error = True
                if meta.get("error") or meta.get("is_error"):
                    is_error = True
                dur = meta.get("duration_ms")
                if isinstance(dur, (int, float)):
                    duration_ms = int(dur)
            name = registered_name or default_name
            event = {
                "name": name,
                "status": "failed" if is_error else "completed",
                "duration_ms": duration_ms,
                "is_error": is_error,
            }
            assistant.tool_events = list(assistant.tool_events) + [event]

        def register_tool_call(call_id: Optional[str], idx: int, name: Any) -> None:
            if call_id:
                tool_name = name if isinstance(name, str) and name else "tool"
                pending_calls.setdefault(call_id, []).append((idx, tool_name[:80]))

        rollout_lines = self._open_rollout_lines(path)
        if rollout_lines is None:
            raise ValueError("unreadable rollout")

        for raw_line in rollout_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue

            record_type = record.get("type")
            timestamp = _safe_ts(record.get("timestamp", ""), file_mtime)
            touch_ts(timestamp)

            if record_type == "session_meta":
                payload = record.get("payload")
                if isinstance(payload, dict):
                    metadata = payload
            elif record_type == "message":
                self._handle_legacy_message(record, timestamp, append_message)
            elif record_type == "response_item":
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type", "")
                if payload_type == "message":
                    self._handle_response_message(payload, timestamp, current_model, append_message)
                elif payload_type == "reasoning":
                    self._handle_reasoning(payload, timestamp, append_message)
                elif payload_type == "function_call":
                    idx = self._handle_function_call(payload, timestamp, append_message)
                    register_tool_call(
                        _clean_call_id(payload.get("call_id")), idx, payload.get("name")
                    )
                elif payload_type == "custom_tool_call":
                    idx = self._handle_custom_tool_call(payload, timestamp, append_message)
                    register_tool_call(
                        _clean_call_id(payload.get("call_id")), idx, payload.get("name")
                    )
                elif payload_type == "function_call_output":
                    call_id = _clean_call_id(payload.get("call_id"))
                    output_text = self._coerce_output_text(payload.get("output"))
                    attach_tool_result(
                        call_id,
                        output_text,
                        payload.get("metadata"),
                        timestamp,
                    )
                elif payload_type == "custom_tool_call_output":
                    call_id = _clean_call_id(payload.get("call_id"))
                    output_text = self._coerce_output_text(payload.get("output"))
                    name = payload.get("name")
                    default_name = name if isinstance(name, str) else "tool"
                    attach_tool_result(
                        call_id,
                        output_text,
                        payload.get("metadata"),
                        timestamp,
                        default_name=default_name,
                    )
                elif payload_type == "agent_message":
                    self._handle_agent_message(payload, timestamp, append_message)
                else:
                    self._mark_unknown(f"response_item:{payload_type}", unknown_seen)
            elif record_type == "event_msg":
                payload = record.get("payload")
                if isinstance(payload, dict) and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict):
                        last_usage = _format_token_usage(info.get("last_token_usage"))
                        total_usage = _format_token_usage(info.get("total_token_usage"))
                        if total_usage and "total" in total_usage:
                            session_total_tokens = total_usage["total"]
                        if last_usage and last_assistant_idx >= 0:
                            messages[last_assistant_idx].tokens = last_usage
                else:
                    sub = payload.get("type") if isinstance(payload, dict) else ""
                    self._mark_unknown(f"event_msg:{sub}", unknown_seen)
            elif record_type == "turn_context":
                payload = record.get("payload")
                if isinstance(payload, dict):
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        current_model = model
                    cwd = _clean_path(payload.get("cwd"))
                    if cwd:
                        turn_cwd = cwd
            elif record_type == "compacted":
                payload = record.get("payload")
                if isinstance(payload, dict):
                    message = payload.get("message")
                    text = message if isinstance(message, str) else ""
                    if text:
                        append_message(
                            UnifiedMessage(
                                role=Role.INFO,
                                content=text,
                                timestamp=timestamp,
                                kind="compaction_summary",
                            )
                        )
            elif record_type in (
                "session_meta",
                "response_item",
                "event_msg",
                "turn_context",
                "compacted",
            ):
                pass
            else:
                self._mark_unknown(f"record:{record_type}", unknown_seen)

        if created_at is None:
            created_at = file_mtime
        if last_updated is None:
            last_updated = file_mtime

        project_path = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
        if not project_path:
            project_path = turn_cwd

        # Calls whose output never arrived keep an explicit status instead of vanishing.
        for queue in pending_calls.values():
            for idx, tool_name in queue:
                if 0 <= idx < len(messages):
                    owner = messages[idx]
                    owner.tool_events = list(owner.tool_events) + [
                        {
                            "name": tool_name,
                            "status": "no_result",
                            "duration_ms": None,
                            "is_error": False,
                        }
                    ]

        origin, relation, parent_session_id = _origin_from_session_meta(metadata)
        session = UnifiedSession(
            tool=Tool.CODEX,
            session_id=metadata.get("id", path.stem)
            if isinstance(metadata.get("id"), str)
            else path.stem,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            cli_version=metadata.get("cli_version")
            if isinstance(metadata.get("cli_version"), str)
            else None,
            source_path=str(path),
            origin=origin,
            parent_session_id=parent_session_id,
            relation=relation,
        )
        if titles:
            self._apply_native_title(session, titles)
        if session_total_tokens is not None and last_assistant_idx >= 0:
            tok = dict(session.messages[last_assistant_idx].tokens or {})
            tok["session_total"] = session_total_tokens
            session.messages[last_assistant_idx].tokens = tok
        return session

    @staticmethod
    def _coerce_output_text(value: Any) -> str:
        if isinstance(value, str):
            text = value
        elif value is None:
            return ""
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return ""
        if len(text) > _MAX_TOOL_OUTPUT_CHARS:
            text = text[:_MAX_TOOL_OUTPUT_CHARS] + "\n[truncated]"
        return text

    def _handle_legacy_message(
        self,
        record: Dict[str, Any],
        timestamp: datetime,
        append_message,
    ) -> None:
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            return
        role_str = payload.get("role", "user")
        role = Role.USER if role_str == "user" else Role.ASSISTANT
        content = self._normalize_content(payload.get("content", ""))
        kind = "text"
        if role == Role.USER and _is_injected_user_content(content):
            kind = "injected_context"
        msg_id = payload.get("id")
        append_message(
            UnifiedMessage(
                role=role,
                content=content,
                timestamp=timestamp,
                message_id=msg_id if isinstance(msg_id, str) else None,
                kind=kind,
            )
        )

    def _handle_response_message(
        self,
        payload: Dict[str, Any],
        timestamp: datetime,
        model: Optional[str],
        append_message,
    ) -> None:
        role_str = payload.get("role", "assistant")
        role = Role.USER if role_str == "user" else Role.ASSISTANT
        content = self._content_from_list(payload.get("content", []))
        if not content:
            return
        kind = "text"
        if role == Role.USER and _is_injected_user_content(content):
            kind = "injected_context"
        msg_id = payload.get("id")
        append_message(
            UnifiedMessage(
                role=role,
                content=content,
                timestamp=timestamp,
                message_id=msg_id if isinstance(msg_id, str) else None,
                model=model if role == Role.ASSISTANT else None,
                kind=kind,
            )
        )

    @staticmethod
    def _handle_reasoning(payload: Dict[str, Any], timestamp: datetime, append_message) -> None:
        summary = payload.get("summary", [])
        summary_text = ""
        if isinstance(summary, list):
            for s in summary:
                if isinstance(s, dict) and s.get("type") == "summary_text":
                    text = s.get("text", "")
                    if isinstance(text, str):
                        summary_text = text
                        break
        if summary_text:
            append_message(
                UnifiedMessage(
                    role=Role.ASSISTANT,
                    content=f"💭 {summary_text}",
                    timestamp=timestamp,
                    reasoning=summary_text,
                )
            )

    def _handle_function_call(
        self, payload: Dict[str, Any], timestamp: datetime, append_message
    ) -> int:
        func_name = payload.get("name", "unknown")
        if not isinstance(func_name, str):
            func_name = "unknown"
        func_args_str = payload.get("arguments", "{}")
        if not isinstance(func_args_str, str):
            func_args_str = "{}"
        try:
            func_args = json.loads(func_args_str)
        except json.JSONDecodeError:
            func_args = {"raw": func_args_str}
        if not isinstance(func_args, dict):
            func_args = {"raw": func_args}
        content = self._format_tool_call_content(func_name, func_args)
        call_id = payload.get("call_id")
        return append_message(
            UnifiedMessage(
                role=Role.ASSISTANT,
                content=content,
                timestamp=timestamp,
                message_id=_clean_call_id(call_id),
            )
        )

    def _handle_custom_tool_call(
        self, payload: Dict[str, Any], timestamp: datetime, append_message
    ) -> int:
        tool_name = payload.get("name", "tool")
        if not isinstance(tool_name, str):
            tool_name = "tool"
        tool_input = payload.get("input")
        if isinstance(tool_input, dict):
            content = self._format_tool_call_content(tool_name, tool_input)
        elif isinstance(tool_input, str):
            preview = tool_input[:_MAX_TOOL_ARG_CHARS]
            content = f"[Tool: {tool_name}] {preview}"
        else:
            content = self._format_tool_call_content(tool_name, {})
        call_id = payload.get("call_id")
        return append_message(
            UnifiedMessage(
                role=Role.ASSISTANT,
                content=content,
                timestamp=timestamp,
                message_id=_clean_call_id(call_id),
            )
        )

    @staticmethod
    def _handle_agent_message(payload: Dict[str, Any], timestamp: datetime, append_message) -> None:
        author = payload.get("author", "agent")
        if not isinstance(author, str):
            author = "agent"
        content = payload.get("content", "")
        if not isinstance(content, str):
            content = str(content) if content is not None else ""
        if content:
            append_message(
                UnifiedMessage(
                    role=Role.INFO,
                    content=f"{author}: {content}",
                    timestamp=timestamp,
                )
            )

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if isinstance(text, str):
                        text_parts.append(text)
            return "\n".join(text_parts)
        if isinstance(content, str):
            return content
        return "" if content is None else str(content)

    @staticmethod
    def _content_from_list(content_list: Any) -> str:
        if not isinstance(content_list, list):
            return CodexExtractor._normalize_content(content_list)
        text_parts = []
        for item in content_list:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type in ("output_text", "input_text"):
                    text = item.get("text", "")
                    if isinstance(text, str):
                        text_parts.append(text)
        return "\n".join(text_parts)

    @classmethod
    def _format_tool_call_content(cls, func_name: str, func_args: Dict[str, Any]) -> str:
        if func_name == "shell_command":
            cmd = func_args.get("command", "")
            if not isinstance(cmd, str):
                cmd = str(cmd)
            return f"[Tool: Bash]\n```bash\n{cmd}\n```"
        if func_name in ("read_file", "str_replace_editor"):
            file_path = func_args.get("path", func_args.get("file_path", ""))
            return f"[Tool: {func_name}] {file_path}"
        if func_name == "write_file":
            file_path = func_args.get("path", "")
            return f"[Tool: Write] {file_path}"
        args_preview = ", ".join(
            f"{k}={str(v)[:_MAX_TOOL_ARG_CHARS]}" for k, v in list(func_args.items())[:3]
        )
        return f"[Tool: {func_name}] {args_preview}"
