"""Kimi CLI extractor — ``context.jsonl`` / ``wire.jsonl`` (issue #197 / B2.8d)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.paths import make_thread_id
from .base import BaseExtractor
from .cursor_agent import _open_plain_file, is_secret_field_name
from .sqlite_common import cap_text, render_tool_arguments

logger = logging.getLogger(__name__)

PARSER_VERSION = 1
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_REGISTRY_ENTRIES = 500
_MAX_STATE_BYTES = 64 * 1024
_MAX_WIRE_SMALL_BYTES = 8 * 1024 * 1024
_MAX_WIRE_SEEK_BYTES = 64 * 1024
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 20_000
_MAX_REASONING_CHARS = 20_000
_MAX_CONTENT_CHARS = 100_000
_MAX_MESSAGES = 20_000
_MAX_PATH_CHARS = 4096
_MD5_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
_CHECKPOINT_RE = re.compile(r"^(?:<system>)?CHECKPOINT\b", re.IGNORECASE)


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value) or value <= 0:
                return fallback
            return make_naive(datetime.fromtimestamp(float(value)))
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


def _clean_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _md5_workdir(path: str) -> str:
    return hashlib.md5(path.encode("utf-8")).hexdigest()


def _tool_failed(content: str) -> bool:
    return content.startswith("ERROR") or content.startswith("<system>ERROR")


class KimiExtractor(BaseExtractor):
    """Reads Kimi CLI sessions under ``~/.kimi/sessions/<md5>/<session_id>/``."""

    tool = Tool.KIMI
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, kimi_root: Optional[Path] = None) -> None:
        self.kimi_root = kimi_root
        self.record_counts: Dict[str, int] = {}
        self._unknown_roles: Set[str] = set()
        self._unknown_types: Set[str] = set()

    def source_description(self) -> str:
        return "~/.kimi/kimi.json, ~/.kimi/sessions/<md5>/<session_id>/context.jsonl"

    def is_available(self) -> bool:
        root = self._sessions_root()
        return root is not None and root.is_dir()

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n
        if key == "unknown_records":
            self._count("records_unknown", n)

    def _kimi_home(self) -> Path:
        if self.kimi_root is not None:
            return self.kimi_root
        return Path.home() / ".kimi"

    def _sessions_root(self) -> Optional[Path]:
        root = self._kimi_home() / "sessions"
        try:
            if root.is_symlink():
                return None
            if root.is_dir():
                return root
        except OSError:
            return None
        return None

    def _load_workdir_map(self) -> Dict[str, str]:
        registry = self._kimi_home() / "kimi.json"
        out: Dict[str, str] = {}
        try:
            if not registry.is_file() or registry.is_symlink():
                return out
            if registry.stat().st_size > _MAX_REGISTRY_BYTES:
                return out
            raw = registry.read_bytes()
        except OSError:
            return out
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError, TypeError):
            return out
        if not isinstance(data, dict):
            return out
        work_dirs = data.get("work_dirs")
        if not isinstance(work_dirs, list):
            return out
        for entry in work_dirs[:_MAX_REGISTRY_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            if entry.get("kaos") != "local":
                continue
            path = _clean_path(entry.get("path"))
            if path is None:
                continue
            digest = _md5_workdir(path)
            if digest not in out:
                out[digest] = path
        return out

    @staticmethod
    def _is_plain_dir(path: Path) -> bool:
        try:
            return path.is_dir() and not path.is_symlink()
        except OSError:
            return False

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        sessions_root = self._sessions_root()
        if sessions_root is None:
            return
        work_map = self._load_workdir_map()
        refs: List[Tuple[str, str, Path]] = []
        for md5_path in sorted(sessions_root.iterdir(), key=lambda p: p.name):
            if not self._is_plain_dir(md5_path):
                continue
            if not _MD5_DIR_RE.match(md5_path.name):
                continue
            try:
                children = sorted(md5_path.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for session_dir in children:
                if not self._is_plain_dir(session_dir):
                    continue
                context = session_dir / "context.jsonl"
                try:
                    if context.is_symlink() or not context.is_file():
                        continue
                except OSError:
                    continue
                self._count("files_matched")
                refs.append((md5_path.name, session_dir.name, context))

        id_counts: Dict[str, int] = {}
        for _md5, sid, _path in refs:
            id_counts[sid] = id_counts.get(sid, 0) + 1
        qualify = {sid for sid, n in id_counts.items() if n > 1}

        for md5_hex, session_id, context_path in refs:
            if self._source_unchanged(context_path):
                continue
            qualified_id = f"{md5_hex}:{session_id}" if session_id in qualify else session_id
            project_path = work_map.get(md5_hex)
            try:
                session = self._parse_session(
                    context_path=context_path,
                    session_dir=context_path.parent,
                    session_id=qualified_id,
                    project_path=project_path,
                )
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                OverflowError,
                RecursionError,
            ) as exc:
                logger.warning("Failed to parse %s: %s", context_path, type(exc).__name__)
                continue
            if session is None:
                continue
            self._count("files_parsed")
            if self.should_import_session(session):
                yield session

    def _parse_session(
        self,
        context_path: Path,
        session_dir: Path,
        session_id: str,
        project_path: Optional[str],
    ) -> Optional[UnifiedSession]:
        opened = _open_plain_file(context_path)
        if opened is None:
            return None
        stream, file_mtime = opened
        title = self._read_state_title(session_dir / "state.json")
        wire_path = session_dir / "wire.jsonl"
        created_at, last_updated, user_times = self._wire_timing(wire_path, file_mtime)

        messages: List[UnifiedMessage] = []
        pending_events: Dict[str, Dict[str, Any]] = {}  # tool_call_id -> event awaiting result
        user_turn_idx = 0
        current_user_ts = last_updated
        with stream as handle:
            while True:
                if len(messages) >= _MAX_MESSAGES:
                    self._count_record("message_limit")
                    break
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
                role = rec.get("role")
                if role in ("_system_prompt", "_checkpoint", "_usage"):
                    continue
                if role not in ("user", "assistant", "tool"):
                    if isinstance(role, str) and role not in self._unknown_roles:
                        self._unknown_roles.add(role)
                        self._count_record("unknown_records")
                    continue
                if role == "user":
                    if self._is_checkpoint_marker(rec.get("content")):
                        continue
                    if user_turn_idx < len(user_times):
                        ts = user_times[user_turn_idx]
                    elif user_times:
                        ts = user_times[-1]
                    else:
                        ts = last_updated
                    current_user_ts = ts
                    user_turn_idx += 1
                    content = cap_text(
                        self._join_text_parts(rec.get("content")), _MAX_CONTENT_CHARS
                    )
                    if content:
                        messages.append(
                            UnifiedMessage(role=Role.USER, content=content, timestamp=ts)
                        )
                elif role == "assistant":
                    text, reasoning, tool_events = self._format_assistant(rec, pending_events)
                    content = cap_text(text, _MAX_CONTENT_CHARS)
                    if content or reasoning or tool_events:
                        messages.append(
                            UnifiedMessage(
                                role=Role.ASSISTANT,
                                content=content,
                                timestamp=current_user_ts,
                                reasoning=reasoning,
                                tool_events=tool_events,
                            )
                        )
                else:
                    text = self._join_text_parts(rec.get("content"))
                    call_id = rec.get("tool_call_id")
                    event = pending_events.pop(call_id, None) if isinstance(call_id, str) else None
                    if event is not None and _tool_failed(text):
                        event["status"] = "failed"
                        event["is_error"] = True
                    content = cap_text(text, _MAX_BODY_CHARS)
                    if content:
                        messages.append(
                            UnifiedMessage(
                                role=Role.TOOL, content=content, timestamp=current_user_ts
                            )
                        )

        if not messages:
            return None
        if created_at > last_updated:
            last_updated = created_at
        for msg in messages:
            if msg.timestamp > last_updated:
                last_updated = msg.timestamp

        thread_id = make_thread_id(project_path=project_path) if project_path else None
        return UnifiedSession(
            tool=Tool.KIMI,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            origin="interactive",
            source_path=str(context_path),
            parser_version=self.PARSER_VERSION,
        )

    def _read_state_title(self, path: Path) -> Optional[str]:
        try:
            if path.is_symlink() or not path.is_file():
                return None
            if path.stat().st_size > _MAX_STATE_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, RecursionError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        title = data.get("custom_title")
        if not isinstance(title, str) or not title.strip():
            return None
        return title.strip()[:_MAX_TITLE_CHARS]

    def _wire_timing(
        self, wire_path: Path, fallback: datetime
    ) -> Tuple[datetime, datetime, List[datetime]]:
        created = fallback
        updated = fallback
        turn_times: List[datetime] = []
        try:
            if wire_path.is_symlink() or not wire_path.is_file():
                return created, updated, turn_times
            size = wire_path.stat().st_size
        except OSError:
            return created, updated, turn_times

        first_ts, last_ts = self._wire_end_timestamps(wire_path, size)
        if first_ts is not None:
            created = first_ts
        if last_ts is not None:
            updated = last_ts

        if size <= _MAX_WIRE_SMALL_BYTES:
            turn_times = self._wire_turn_begins(wire_path)
        return created, updated, turn_times

    def _wire_end_timestamps(
        self, wire_path: Path, size: int
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        first: Optional[datetime] = None
        last: Optional[datetime] = None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(wire_path, flags)
        except OSError:
            return None, None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None, None
            head = os.read(fd, min(size, _MAX_WIRE_SEEK_BYTES))
            if size > _MAX_WIRE_SEEK_BYTES:
                os.lseek(fd, max(0, size - _MAX_WIRE_SEEK_BYTES), os.SEEK_SET)
                tail = os.read(fd, _MAX_WIRE_SEEK_BYTES)
            else:
                tail = head
        except OSError:
            return None, None
        finally:
            os.close(fd)
        for chunk in (head, tail):
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = self._wire_record_ts(rec)
                if ts is None:
                    continue
                if first is None:
                    first = ts
                last = ts
        return first, last

    def _wire_turn_begins(self, wire_path: Path) -> List[datetime]:
        times: List[datetime] = []
        opened = _open_plain_file(wire_path)
        if opened is None:
            return times
        stream, fallback = opened
        with stream as handle:
            while True:
                line = handle.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = handle.readline(_MAX_LINE_CHARS)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError, OverflowError):
                    continue
                if not isinstance(rec, dict):
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict) or msg.get("type") != "TurnBegin":
                    continue
                ts = self._wire_record_ts(rec)
                if ts is not None:
                    times.append(ts)
        return times

    @staticmethod
    def _wire_record_ts(rec: Dict[str, Any]) -> Optional[datetime]:
        raw = rec.get("timestamp")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        if not math.isfinite(raw) or raw <= 0:
            return None
        try:
            return make_naive(datetime.fromtimestamp(float(raw)))
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _is_checkpoint_marker(content: Any) -> bool:
        if isinstance(content, str):
            return bool(_CHECKPOINT_RE.match(content.strip()))
        if isinstance(content, list) and len(content) == 1:
            part = content[0]
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    return bool(_CHECKPOINT_RE.match(text.strip()))
        return False

    def _join_text_parts(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    def _format_assistant(
        self, rec: Dict[str, Any], pending_events: Dict[str, Dict[str, Any]]
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        content = rec.get("content")
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_events: List[Dict[str, Any]] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
                elif kind == "think":
                    think = item.get("think")
                    if isinstance(think, str):
                        reasoning_parts.append(think)
        tool_calls = rec.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue
                args_raw = fn.get("arguments")
                suffix = ""
                if isinstance(args_raw, str):
                    try:
                        parsed = json.loads(args_raw)
                    except (ValueError, RecursionError, TypeError):
                        parsed = None
                    if isinstance(parsed, dict):
                        suffix = render_tool_arguments(parsed, is_secret_field_name)
                text_parts.append(f"[Tool: {name}]{suffix}")
                call_id = call.get("id")
                event: Dict[str, Any] = {
                    "name": name,
                    "status": "completed",  # patched to failed when the result arrives
                    "duration_ms": None,
                    "is_error": False,
                }
                tool_events.append(event)
                if isinstance(call_id, str) and len(pending_events) < _MAX_MESSAGES:
                    pending_events[call_id] = event
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        if reasoning and len(reasoning) > _MAX_REASONING_CHARS:
            reasoning = reasoning[:_MAX_REASONING_CHARS]
        return "\n".join(text_parts), reasoning, tool_events
