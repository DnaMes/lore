"""Cline-family VS Code task extractors (Cline, Roo Code, Kilo Code — issue #197 / B2.8a)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.paths import make_thread_id
from .base import BaseExtractor
from .cursor_agent import _open_plain_file, is_secret_field_name

logger = logging.getLogger(__name__)

PARSER_VERSION = 1
_MAX_FILE_BYTES = 64 * 1024 * 1024  # json.load materialises the file: ~10x in memory
_MAX_CONTENT_CHARS = 100_000
_MAX_TITLE_CHARS = 200
_MAX_TOOL_ARG_CHARS = 200
_MAX_PATH_CHARS = 4096
_MAX_BODY_CHARS = 20_000
_MAX_MESSAGES_PER_TASK = 20_000

_LINUX_EDITORS = ("Code", "Code - Insiders", "VSCodium", "Cursor")

_ENV_DETAILS_RE = re.compile(
    r"<environment_details>.*?(?:</environment_details>|\Z)",  # unclosed tag: drop the rest
    flags=re.DOTALL | re.IGNORECASE,
)
_EXTENSION_BY_TOOL = {
    Tool.CLINE: "saoudrizwan.claude-dev",
    Tool.ROO_CODE: "rooveterinaryinc.roo-cline",
    Tool.KILO_CODE: "kilocode.kilo-code",
}


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 1_000_000_000_000:
                return make_naive(datetime.fromtimestamp(float(value) / 1000.0))
            return make_naive(parse_timestamp(value))
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


def _cap_text(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _clean_user_text(text: str) -> str:
    text = _ENV_DETAILS_RE.sub("", text)
    text = re.sub(r"</?task>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _format_input_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return "<nested>"
    out = str(value)
    if len(out) > _MAX_TOOL_ARG_CHARS:
        out = out[:_MAX_TOOL_ARG_CHARS]
    return out


class ClineFamilyExtractor(BaseExtractor):
    """Shared parser for Cline-schema VS Code globalStorage tasks and Cline CLI sessions."""

    PARSER_VERSION = PARSER_VERSION

    def __init__(
        self,
        editor_roots: Optional[List[Path]] = None,
        cli_sessions_root: Optional[Path] = None,
    ) -> None:
        self._editor_roots_override = editor_roots
        self._cli_root_override = cli_sessions_root
        self.record_counts: Dict[str, int] = {}

    @property
    def extension_id(self) -> str:
        return _EXTENSION_BY_TOOL[self.tool]

    def source_description(self) -> str:
        parts = [
            f"~/.config/<Editor>/User/globalStorage/{self.extension_id}/tasks/*/api_conversation_history.json"
        ]
        if self.tool == Tool.CLINE:
            parts.append("~/.cline/data/sessions/*.json")
        return ", ".join(parts)

    def is_available(self) -> bool:
        return bool(self._task_roots()) or self._cli_root() is not None

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n
        if key == "unknown_records":
            self._count("records_unknown", n)

    @staticmethod
    def _is_plain_dir(path: Path) -> bool:
        try:
            return path.is_dir() and not path.is_symlink()
        except OSError:
            return False

    def _config_editor_tasks_dirs(self) -> List[Tuple[str, Path]]:
        found: List[Tuple[str, Path]] = []
        home = Path.home()
        for editor in _LINUX_EDITORS:
            tasks = (
                home / ".config" / editor / "User" / "globalStorage" / self.extension_id / "tasks"
            )
            if self._is_plain_dir(tasks):
                found.append((editor, tasks))
        return found

    def _task_roots(self) -> List[Tuple[str, Path]]:
        if self._editor_roots_override is not None:
            return [(self._editor_label(p), p) for p in self._editor_roots_override]
        return self._config_editor_tasks_dirs()

    @staticmethod
    def _editor_label(tasks_dir: Path) -> str:
        parts = tasks_dir.parts
        try:
            idx = parts.index(".config")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        except ValueError:
            pass
        return tasks_dir.name

    def _cli_root(self) -> Optional[Path]:
        if self._cli_root_override is not None:
            root = self._cli_root_override
        elif self.tool == Tool.CLINE:
            root = Path.home() / ".cline" / "data" / "sessions"
        else:
            return None
        try:
            if root.is_symlink():
                return None
            if root.is_dir():
                return root
        except OSError:
            return None
        return None

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        task_refs = self._collect_task_refs()
        id_counts: Dict[str, int] = {}
        for _editor, _tasks_dir, task_id, _task_dir, _hist in task_refs:
            id_counts[task_id] = id_counts.get(task_id, 0) + 1

        for editor, tasks_dir, task_id, task_dir, history_path in task_refs:
            if self._source_unchanged(history_path):
                continue
            self._count("files_matched")
            try:
                session = self._parse_task_dir(
                    editor,
                    task_id,
                    task_dir,
                    history_path,
                    qualify=id_counts.get(task_id, 0) > 1,
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
                logger.warning("Failed to parse %s: %s", history_path, type(exc).__name__)
                continue
            if session is None:
                continue
            self._count("files_parsed")
            if self.should_import_session(session):
                yield session

        cli_root = self._cli_root()
        if cli_root is None:
            return
        try:
            names = sorted(cli_root.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for path in names:
            if path.suffix != ".json" or path.name.endswith(".messages.json"):
                continue
            if not self._is_plain_file(path):
                continue
            if self._source_unchanged(path):
                continue
            self._count("files_matched")
            try:
                session = self._parse_cli_session(path)
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

    def _collect_task_refs(self) -> List[Tuple[str, Path, str, Path, Path]]:
        refs: List[Tuple[str, Path, str, Path, Path]] = []
        for editor, tasks_dir in self._task_roots():
            try:
                children = sorted(tasks_dir.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for task_dir in children:
                if not self._is_plain_dir(task_dir):
                    continue
                history = task_dir / "api_conversation_history.json"
                if self._is_plain_file(history):
                    refs.append((editor, tasks_dir, task_dir.name, task_dir, history))
        return refs

    @staticmethod
    def _is_plain_file(path: Path) -> bool:
        try:
            return path.is_file() and not path.is_symlink()
        except OSError:
            return False

    def _load_json(self, path: Path) -> Tuple[Any, Optional[datetime]]:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                self._count_record("skipped_oversize")
                return None, None
        except OSError:
            return None, None
        opened = _open_plain_file(path)
        if opened is None:
            return None, None
        stream, mtime = opened
        try:
            with stream as handle:
                data = json.load(handle)
        except (ValueError, RecursionError, TypeError, OverflowError):
            return None, mtime
        return data, mtime

    def _parse_task_dir(
        self,
        editor: str,
        task_id: str,
        task_dir: Path,
        history_path: Path,
        *,
        qualify: bool,
    ) -> Optional[UnifiedSession]:
        raw, file_mtime = self._load_json(history_path)
        if raw is None:
            return None
        if not isinstance(raw, list):
            self._count_record("unknown_records")
            return None
        if file_mtime is None:
            file_mtime = datetime.fromtimestamp(history_path.stat().st_mtime)

        ui_entries = self._read_ui_messages(task_dir)
        title = self._title_from_ui(ui_entries)
        project_path, native_title = self._read_history_item(task_dir)
        if native_title and not title:
            title = native_title

        if len(ui_entries) == len(raw):
            fallback_ts = make_naive(file_mtime)
            ui_ts = self._ui_timestamps(ui_entries, len(raw), fallback_ts)
        else:
            fallback_ts = make_naive(file_mtime)
            ui_ts = [fallback_ts] * len(raw)

        tool_results = self._collect_tool_results(raw)
        messages = self._messages_from_history(raw, ui_ts, fallback_ts, tool_results)
        if not messages:
            return None

        session_id = task_id
        if qualify:
            session_id = f"{self.tool.value}:{editor}:{task_id}"

        thread_id = make_thread_id(project_path=project_path) if project_path else None
        created_at = messages[0].timestamp
        last_updated = max(m.timestamp for m in messages)
        return UnifiedSession(
            tool=self.tool,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            origin="interactive",
            source_path=str(history_path),
            parser_version=self.PARSER_VERSION,
        )

    def _parse_cli_session(self, path: Path) -> Optional[UnifiedSession]:
        raw, file_mtime = self._load_json(path)
        messages_raw: Any = None
        if isinstance(raw, list):
            messages_raw = raw
        elif isinstance(raw, dict):
            candidate = raw.get("messages")
            if isinstance(candidate, list):
                messages_raw = candidate
            else:
                self._count_record("unknown_records")
                return None
        else:
            alt = path.with_name(f"{path.stem}.messages.json")
            if self._is_plain_file(alt):
                alt_raw, _ = self._load_json(alt)
                if isinstance(alt_raw, list):
                    messages_raw = alt_raw
                else:
                    self._count_record("unknown_records")
                    return None
            else:
                self._count_record("unknown_records")
                return None

        if file_mtime is None:
            file_mtime = datetime.fromtimestamp(path.stat().st_mtime)
        tool_results = self._collect_tool_results(messages_raw)
        fallback = file_mtime
        ui_ts = [fallback] * len(messages_raw)
        messages = self._messages_from_history(messages_raw, ui_ts, fallback, tool_results)
        if not messages:
            return None
        return UnifiedSession(
            tool=self.tool,
            session_id=path.stem,
            created_at=messages[0].timestamp,
            last_updated=max(m.timestamp for m in messages),
            messages=messages,
            origin="unknown",
            source_path=str(path),
            parser_version=self.PARSER_VERSION,
        )

    def _read_ui_messages(self, task_dir: Path) -> List[Dict[str, Any]]:
        path = task_dir / "ui_messages.json"
        if not self._is_plain_file(path):
            return []
        raw, _ = self._load_json(path)
        if not isinstance(raw, list):
            return []
        return [e for e in raw if isinstance(e, dict)]

    @staticmethod
    def _title_from_ui(entries: List[Dict[str, Any]]) -> Optional[str]:
        for entry in entries:
            if entry.get("type") != "say":
                continue
            kind = entry.get("say")
            if kind not in ("text", "task"):
                continue
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()[:_MAX_TITLE_CHARS]
        return None

    def _read_history_item(self, task_dir: Path) -> Tuple[Optional[str], Optional[str]]:
        path = task_dir / "history_item.json"
        if not self._is_plain_file(path):
            return None, None
        raw, _ = self._load_json(path)
        if not isinstance(raw, dict):
            return None, None
        workspace = _clean_path(raw.get("workspace"))
        title = _clean_str(raw.get("task"))
        return workspace, title[:_MAX_TITLE_CHARS] if title else None

    def _ui_timestamps(
        self, entries: List[Dict[str, Any]], history_len: int, fallback: datetime
    ) -> List[datetime]:
        if len(entries) != history_len:
            return [fallback] * history_len
        out: List[datetime] = []
        for entry in entries:
            out.append(_safe_ts(entry.get("ts"), fallback))
        return out

    def _collect_tool_results(self, history: List[Any]) -> Dict[str, bool]:
        mapping: Dict[str, bool] = {}
        for item in history:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            for block in self._content_blocks(item.get("content")):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    use_id = block.get("tool_use_id")
                    if isinstance(use_id, str):
                        mapping[use_id] = block.get("is_error") is True
        return mapping

    def _messages_from_history(
        self,
        history: List[Any],
        ui_ts: List[datetime],
        fallback: datetime,
        tool_results: Dict[str, bool],
    ) -> List[UnifiedMessage]:
        messages: List[UnifiedMessage] = []
        for index, item in enumerate(history):
            if len(messages) >= _MAX_MESSAGES_PER_TASK:
                break
            if not isinstance(item, dict):
                self._count_record("unknown_records")
                continue
            ts = ui_ts[index] if index < len(ui_ts) else fallback
            role_str = item.get("role")
            msg = self._message_from_payload(
                role_str=role_str,
                blocks=item.get("content"),
                timestamp=ts,
                tool_results=tool_results,
            )
            if msg is not None:
                messages.append(msg)
        return messages

    def _message_from_payload(
        self,
        role_str: Any,
        blocks: Any,
        timestamp: datetime,
        tool_results: Dict[str, bool],
    ) -> Optional[UnifiedMessage]:
        if role_str == "user":
            if self._is_tool_result_only(blocks):
                content = _cap_text(self._format_tool_result_blocks(blocks))
                return UnifiedMessage(role=Role.TOOL, content=content, timestamp=timestamp)
            content_str = _cap_text(
                _clean_user_text(self._format_user_blocks(blocks)), _MAX_CONTENT_CHARS
            )
            if not content_str:
                return None  # wrapper-only turn (task/environment tags): not a prompt
            return UnifiedMessage(role=Role.USER, content=content_str, timestamp=timestamp)
        if role_str == "assistant":
            content_text, reasoning, tool_events = self._format_assistant_blocks(
                blocks, tool_results
            )
            return UnifiedMessage(
                role=Role.ASSISTANT,
                content=_cap_text(content_text, _MAX_CONTENT_CHARS),
                timestamp=timestamp,
                reasoning=_cap_text(reasoning) if reasoning else None,
                tool_events=tool_events,
            )
        if role_str in ("tool", "toolResult"):
            return UnifiedMessage(
                role=Role.TOOL,
                content=_cap_text(self._format_tool_result_blocks(blocks)),
                timestamp=timestamp,
            )
        self._count_record("unknown_records")
        return None

    @staticmethod
    def _content_blocks(content: Any) -> List[Any]:
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if isinstance(content, dict):
            return [content]
        return []

    @classmethod
    def _is_tool_result_only(cls, blocks: Any) -> bool:
        items = cls._content_blocks(blocks)
        if not items:
            return False
        return all(isinstance(b, dict) and b.get("type") == "tool_result" for b in items)

    @classmethod
    def _format_user_blocks(cls, blocks: Any) -> str:
        parts: List[str] = []
        for block in cls._content_blocks(blocks):
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    @classmethod
    def _format_tool_result_blocks(cls, blocks: Any) -> str:
        parts: List[str] = []
        for block in cls._content_blocks(blocks):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.append(cls._format_user_blocks(content))
                elif content is not None:
                    parts.append(str(content))
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    @classmethod
    def _format_assistant_blocks(
        cls, blocks: Any, tool_results: Dict[str, bool]
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_events: List[Dict[str, Any]] = []
        for block in cls._content_blocks(blocks):
            if not isinstance(block, dict):
                continue
            b_type = block.get("type")
            if b_type == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif b_type in ("thinking", "reasoning"):
                chunk = block.get("thinking") or block.get("reasoning") or block.get("text")
                if isinstance(chunk, str) and chunk:
                    reasoning_parts.append(chunk)
            elif b_type == "tool_use":
                tool_name = block.get("name", "tool")
                if not isinstance(tool_name, str):
                    tool_name = "tool"
                tool_input = block.get("input")
                rendered_args: List[str] = []
                if isinstance(tool_input, dict):
                    for key, value in tool_input.items():
                        if not isinstance(key, str) or is_secret_field_name(key):
                            continue
                        rendered_args.append(f"{key[:100]}={_format_input_value(value)}")
                elif tool_input is not None:
                    rendered_args.append(_format_input_value(tool_input))
                suffix = (" " + " ".join(rendered_args)) if rendered_args else ""
                text_parts.append(f"[Tool: {tool_name}]{suffix}")
                use_id = block.get("id")
                is_error = bool(tool_results.get(use_id)) if isinstance(use_id, str) else False
                # No result recorded (interrupted task): "completed" by convention.
                status = "failed" if is_error else "completed"
                tool_events.append(
                    {
                        "name": tool_name,
                        "status": status,
                        "duration_ms": None,
                        "is_error": is_error,
                    }
                )
        content_text = "\n".join(text_parts)
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        return content_text, reasoning, tool_events


class ClineExtractor(ClineFamilyExtractor):
    tool = Tool.CLINE


class RooCodeExtractor(ClineFamilyExtractor):
    tool = Tool.ROO_CODE


class KiloCodeExtractor(ClineFamilyExtractor):
    tool = Tool.KILO_CODE
