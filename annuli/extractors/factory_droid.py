"""Factory Droid session extractor — ``~/.factory/sessions`` (issue #197 / B2.8e)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.paths import make_thread_id
from .base import BaseExtractor
from .cursor_agent import _open_plain_file, is_secret_field_name
from .sqlite_common import cap_text, render_tool_arguments, tool_result_status

logger = logging.getLogger(__name__)

PARSER_VERSION = 1
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_SETTINGS_BYTES = 64 * 1024
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 20_000
_MAX_REASONING_CHARS = 20_000
_MAX_CONTENT_CHARS = 100_000
_MAX_MESSAGES = 20_000
_MAX_PATH_CHARS = 4096
_SESSION_FILE_RE = re.compile(r"^[0-9a-fA-F-]{32,36}\.jsonl$")


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


class FactoryDroidExtractor(BaseExtractor):
    """Reads Factory Droid JSONL sessions under ``~/.factory/sessions/<slug>/``."""

    tool = Tool.FACTORY_DROID
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, factory_root: Optional[Path] = None) -> None:
        self.factory_root = factory_root
        self.record_counts: Dict[str, int] = {}
        self._unknown_line_types: Set[str] = set()

    def source_description(self) -> str:
        return (
            "~/.factory/sessions/<project-slug>/<session-uuid>.jsonl "
            "and <session-uuid>.settings.json (format from third-party parsers)"
        )

    def is_available(self) -> bool:
        root = self._sessions_root()
        return root is not None and root.is_dir()

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n
        if key == "unknown_records":
            self._count("records_unknown", n)

    def _factory_home(self) -> Path:
        if self.factory_root is not None:
            return self.factory_root
        return Path.home() / ".factory"

    def _sessions_root(self) -> Optional[Path]:
        root = self._factory_home() / "sessions"
        try:
            if root.is_symlink():
                return None
            if root.is_dir():
                return root
        except OSError:
            return None
        return None

    @staticmethod
    def _is_plain_dir(path: Path) -> bool:
        try:
            return path.is_dir() and not path.is_symlink()
        except OSError:
            return False

    @staticmethod
    def _is_session_file(path: Path) -> bool:
        return bool(_SESSION_FILE_RE.match(path.name))

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        sessions_root = self._sessions_root()
        if sessions_root is None:
            return
        try:
            slugs = sorted(sessions_root.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for slug_dir in slugs:
            if not self._is_plain_dir(slug_dir):
                continue
            try:
                files = sorted(slug_dir.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for path in files:
                if not self._is_session_file(path):
                    continue
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                except OSError:
                    continue
                if self._source_unchanged(path):
                    continue
                self._count("files_matched")
                try:
                    session = self._parse_file(path)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    OverflowError,
                    RecursionError,
                ) as exc:
                    logger.warning("Failed to parse %s: %s", path, type(exc).__name__)
                    continue
                if session is None:
                    continue
                self._count("files_parsed")
                if self.should_import_session(session):
                    yield session

    def _load_settings(self, path: Path) -> Tuple[Optional[str], Optional[str]]:
        try:
            if path.is_symlink() or not path.is_file():
                return None, None
            if path.stat().st_size > _MAX_SETTINGS_BYTES:
                return None, None
            raw = path.read_bytes()
        except OSError:
            return None, None
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError, TypeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        model = data.get("model")
        title = data.get("title")
        model_out = model if isinstance(model, str) and model else None
        title_out = (
            title.strip()[:_MAX_TITLE_CHARS] if isinstance(title, str) and title.strip() else None
        )
        return model_out, title_out

    def _parse_file(self, path: Path) -> Optional[UnifiedSession]:
        opened = _open_plain_file(path)
        if opened is None:
            return None
        stream, file_mtime = opened
        try:
            if os.fstat(stream.fileno()).st_size > _MAX_FILE_BYTES:
                stream.close()
                self._count_record("skipped_oversize")
                return None
        except OSError:
            stream.close()
            return None
        settings_path = path.with_suffix(".settings.json")
        settings_model, settings_title = self._load_settings(settings_path)

        session_id = path.stem
        project_path: Optional[str] = None
        title: Optional[str] = None
        messages: List[UnifiedMessage] = []
        tool_results: Dict[str, Dict[str, Any]] = {}
        pending_events: Dict[str, Dict[str, Any]] = {}

        with stream as handle:
            while len(messages) < _MAX_MESSAGES:
                line = handle.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                    self._count_record("malformed_lines")
                    while line and not line.endswith("\n"):
                        line = handle.readline(_MAX_LINE_CHARS)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError, OverflowError):
                    self._count_record("malformed_lines")
                    continue
                if not isinstance(rec, dict):
                    self._count_record("malformed_lines")
                    continue
                line_type = rec.get("type")
                if line_type == "session_start":
                    if project_path is None:
                        project_path = _clean_path(rec.get("cwd"))
                    for key in ("title", "sessionTitle"):
                        val = rec.get(key)
                        if isinstance(val, str) and val.strip() and title is None:
                            title = val.strip()[:_MAX_TITLE_CHARS]
                elif line_type == "message":
                    ts = _safe_ts(rec.get("timestamp"), file_mtime)
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    visibility = msg.get("visibility")
                    if visibility == "user_only":
                        continue
                    role = msg.get("role")
                    if visibility == "llm_only" and role == "user":
                        text = self._join_text_blocks(msg.get("content"))
                        if text:
                            messages.append(
                                UnifiedMessage(
                                    role=Role.INFO,
                                    content=cap_text(text, _MAX_CONTENT_CHARS),
                                    timestamp=ts,
                                    kind="injected_context",
                                )
                            )
                        continue
                    if role == "user":
                        parsed = self._parse_user_message(msg, ts, tool_results, pending_events)
                        messages.extend(parsed)
                    elif role == "assistant":
                        text, reasoning, tool_events = self._format_assistant(
                            msg, tool_results, pending_events
                        )
                        content = cap_text(text, _MAX_CONTENT_CHARS)
                        if content or reasoning or tool_events:
                            messages.append(
                                UnifiedMessage(
                                    role=Role.ASSISTANT,
                                    content=content,
                                    timestamp=ts,
                                    reasoning=reasoning,
                                    tool_events=tool_events,
                                    model=settings_model,
                                )
                            )
                elif line_type == "agent_turn_outcome":
                    continue
                else:
                    if isinstance(line_type, str) and line_type not in self._unknown_line_types:
                        self._unknown_line_types.add(line_type)
                        self._count_record("unknown_records")

        if not messages:
            return None

        if title is None and settings_title:
            title = settings_title

        created_at = messages[0].timestamp
        last_updated = messages[-1].timestamp
        thread_id = make_thread_id(project_path=project_path) if project_path else None
        return UnifiedSession(
            tool=Tool.FACTORY_DROID,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            origin="interactive",
            source_path=str(path),
            parser_version=self.PARSER_VERSION,
        )

    def _join_text_blocks(self, raw: Any) -> str:
        if not isinstance(raw, list):
            return ""
        parts: List[str] = []
        for block in raw:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    def _parse_user_message(
        self,
        msg: Dict[str, Any],
        ts: datetime,
        tool_results: Dict[str, Dict[str, Any]],
        pending_events: Dict[str, Dict[str, Any]],
    ) -> List[UnifiedMessage]:
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        text_parts: List[str] = []
        tool_msgs: List[UnifiedMessage] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif btype == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    tool_results[tool_use_id] = block
                    event = pending_events.pop(tool_use_id, None)
                    if event is not None:  # the call was recorded before its result arrived
                        status, is_error = tool_result_status(None, block.get("is_error"))
                        event["status"] = status
                        event["is_error"] = is_error
                body = block.get("content")
                text = body if isinstance(body, str) else ""
                tool_msgs.append(
                    UnifiedMessage(
                        role=Role.TOOL,
                        content=cap_text(text, _MAX_BODY_CHARS),
                        timestamp=ts,
                    )
                )
        out: List[UnifiedMessage] = []
        if text_parts:
            out.append(
                UnifiedMessage(
                    role=Role.USER,
                    content=cap_text("\n".join(text_parts), _MAX_CONTENT_CHARS),
                    timestamp=ts,
                )
            )
        out.extend(tool_msgs)
        return out

    def _format_assistant(
        self,
        msg: Dict[str, Any],
        tool_results: Dict[str, Dict[str, Any]],
        pending_events: Dict[str, Dict[str, Any]],
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_events: List[Dict[str, Any]] = []
        content = msg.get("content")
        if not isinstance(content, list):
            return "", None, []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif btype == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    reasoning_parts.append(thinking)
            elif btype == "tool_use":
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                args = block.get("input")
                suffix = (
                    render_tool_arguments(args, is_secret_field_name)
                    if isinstance(args, dict)
                    else ""
                )
                text_parts.append(f"[Tool: {name}]{suffix}")
                call_id = block.get("id")
                status, is_error = "completed", False
                if isinstance(call_id, str):
                    result = tool_results.get(call_id)
                    if isinstance(result, dict):
                        status, is_error = tool_result_status(None, result.get("is_error"))
                event = {
                    "name": name,
                    "status": status,
                    "duration_ms": None,
                    "is_error": is_error,
                }
                tool_events.append(event)
                if isinstance(call_id, str):
                    pending_events[call_id] = event
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        if reasoning and len(reasoning) > _MAX_REASONING_CHARS:
            reasoning = reasoning[:_MAX_REASONING_CHARS]
        return "\n".join(text_parts), reasoning, tool_events
