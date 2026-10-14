"""Command Code extractor (v3 tree and flat JSONL, checkpoints — issue #197 / B2.4)."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_META_BYTES = 64 * 1024
_MAX_TOOL_ARG_CHARS = 200
_MAX_PATH_CHARS = 4096


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    try:
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError):
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


def _conversation_id_from_name(name: str) -> Optional[Tuple[str, str]]:
    if name.endswith(".checkpoints.jsonl"):
        return name[: -len(".checkpoints.jsonl")], "checkpoints"
    if name.endswith(".meta.json"):
        return name[: -len(".meta.json")], "meta"
    if name.endswith(".jsonl"):
        return name[: -len(".jsonl")], "transcript"
    return None


class CommandCodeExtractor(BaseExtractor):
    """Extracts chat history from Command Code project transcripts under ~/.commandcode."""

    tool = Tool.COMMAND_CODE
    PARSER_VERSION = 1
    AUTOMATED_BYPASSES_PROMPT_FLOOR = True

    def __init__(self, projects_root: Optional[Path] = None) -> None:
        self.projects_root = projects_root
        self.record_counts: Dict[str, int] = {}

    def source_description(self) -> str:
        return "~/.commandcode/projects/<slug>/*.{jsonl,meta.json,checkpoints.jsonl}"

    def is_available(self) -> bool:
        root = self._resolve_projects_root()
        return root is not None

    def _resolve_projects_root(self) -> Optional[Path]:
        if self.projects_root is not None:
            root = self.projects_root
        else:
            root = Path.home() / ".commandcode" / "projects"
        try:
            if root.is_symlink():
                return None
            if root.is_dir():
                return root
        except OSError:
            return None
        return None

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n
        if key == "unknown_records":
            self._count("records_unknown", n)

    @staticmethod
    def _is_plain_file(path: Path) -> bool:
        try:
            return not path.is_symlink() and path.is_file()
        except OSError:
            return False

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        root = self._resolve_projects_root()
        if root is None:
            return

        seen_ids: set[str] = set()
        for slug_dir in sorted(root.iterdir()):
            try:
                if not slug_dir.is_dir() or slug_dir.is_symlink():
                    continue
            except OSError:
                continue
            grouped = self._group_conversation_files(slug_dir)
            for conv_id, files in sorted(grouped.items()):
                transcript = files.get("transcript")
                checkpoints = files.get("checkpoints")
                meta_path = files.get("meta")
                if transcript is None and checkpoints is None:
                    continue
                source_path = transcript or checkpoints
                assert source_path is not None
                if not self._is_plain_file(source_path):
                    continue
                if self._source_unchanged(source_path):
                    continue
                self._count("files_matched")
                meta = self._read_meta(meta_path) if meta_path else {}
                try:
                    if source_path.stat().st_size > _MAX_FILE_BYTES:
                        self._count_record("skipped_oversize")
                        continue
                    if transcript is not None:
                        session = self._parse_transcript_file(transcript, meta, conv_id)
                    elif checkpoints is not None:
                        session = self._parse_checkpoints_file(checkpoints, meta, conv_id)
                    else:
                        continue
                    if session is None:
                        continue
                    self._count("files_parsed")
                    if session.session_id in seen_ids:
                        # The same id in two project directories is two different
                        # conversations: qualify the later one with its directory.
                        session.session_id = f"{slug_dir.name}:{session.session_id}"
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
                ) as exc:
                    logger.warning("Failed to parse %s: %s", source_path, type(exc).__name__)

    def _group_conversation_files(self, slug_dir: Path) -> Dict[str, Dict[str, Path]]:
        grouped: Dict[str, Dict[str, Path]] = {}
        try:
            names = sorted(slug_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return grouped
        for path in names:
            parsed = _conversation_id_from_name(path.name)
            if parsed is None:
                continue
            conv_id, kind = parsed
            if not self._is_plain_file(path):
                continue
            bucket = grouped.setdefault(conv_id, {})
            bucket[kind] = path
        return grouped

    def _read_meta(self, path: Optional[Path]) -> Dict[str, Any]:
        if path is None or not self._is_plain_file(path):
            return {}
        try:
            size = path.stat().st_size
        except OSError:
            return {}
        if size > _MAX_META_BYTES:
            return {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read(_MAX_META_BYTES + 1)
        except OSError:
            return {}
        if len(raw) > _MAX_META_BYTES:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    @staticmethod
    def _origin_from_meta(meta: Dict[str, Any], has_transcript: bool) -> str:
        entrypoint = meta.get("entrypoint")
        if entrypoint == "print":
            return "exec"
        if has_transcript:
            return "interactive"
        return "unknown"

    @staticmethod
    def _project_path_from_meta(meta: Dict[str, Any], header_cwd: Optional[str]) -> Optional[str]:
        if header_cwd:
            return header_cwd
        for key in ("projectPath", "cwd"):
            cleaned = _clean_path(meta.get(key))
            if cleaned:
                return cleaned
        return None

    def _parse_transcript_file(
        self,
        session_file: Path,
        meta: Dict[str, Any],
        conv_id: str,
    ) -> Optional[UnifiedSession]:
        file_mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
        header: Dict[str, Any] = {}
        v3_entries: List[Dict[str, Any]] = []
        flat_entries: List[Dict[str, Any]] = []
        flat_session_id: Optional[str] = None
        git_branch: Optional[str] = None

        with open(session_file, "r", encoding="utf-8", errors="replace") as handle:
            while True:
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
                except (ValueError, RecursionError):
                    self._count_record("malformed_lines")
                    continue
                if not isinstance(rec, dict):
                    self._count_record("malformed_lines")
                    continue

                rec_type = rec.get("type")
                if rec_type == "session" and not header:
                    header = rec
                    continue
                if rec_type == "message":
                    rec_id = rec.get("id")
                    if not isinstance(rec_id, str):
                        self._count_record("unknown_records")
                        continue
                    v3_entries.append(rec)
                    continue
                role = rec.get("role")
                if isinstance(role, str):
                    rec_id = rec.get("id")
                    if not isinstance(rec_id, str):
                        self._count_record("unknown_records")
                        continue
                    flat_entries.append(rec)
                    sid = _clean_str(rec.get("sessionId"))
                    if sid:
                        flat_session_id = sid
                    branch = _clean_str(rec.get("gitBranch"))
                    if branch:
                        git_branch = branch
                    continue
                self._count_record("unknown_records")

        if flat_entries and not v3_entries:
            return self._build_flat_session(
                session_file=session_file,
                conv_id=flat_session_id or conv_id,
                meta=meta,
                entries=flat_entries,
                fallback_time=file_mtime,
                git_branch=git_branch,
            )

        if not v3_entries:
            return None

        raw_session_id = _clean_str(header.get("id")) or conv_id
        project_path = self._project_path_from_meta(meta, _clean_path(header.get("cwd")))
        created_at = _safe_ts(header.get("timestamp"), file_mtime)
        source_version = str(header.get("version")) if header.get("version") is not None else None
        origin = self._origin_from_meta(meta, has_transcript=True)
        title = _clean_str(meta.get("title"))

        return self._build_v3_session(
            session_file=session_file,
            session_id=raw_session_id,
            project_path=project_path,
            created_at=created_at,
            source_version=source_version,
            origin=origin,
            title=title,
            entries=v3_entries,
            fallback_time=file_mtime,
            git_branch=git_branch,
        )

    def _parse_checkpoints_file(
        self,
        path: Path,
        meta: Dict[str, Any],
        conv_id: str,
    ) -> Optional[UnifiedSession]:
        file_mtime = datetime.fromtimestamp(path.stat().st_mtime)
        turns: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            while True:
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
                except (ValueError, RecursionError):
                    self._count_record("malformed_lines")
                    continue
                if not isinstance(rec, dict):
                    self._count_record("malformed_lines")
                    continue
                turns.append(rec)

        if not turns:
            return None

        def _turn_number(row: Dict[str, Any]) -> int:
            value = row.get("turnNumber")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0
            try:
                return int(value) if math.isfinite(value) else 0
            except (OverflowError, ValueError):
                return 0

        ordered = sorted(turns, key=_turn_number)
        seen_turns: set[Any] = set()
        messages: List[UnifiedMessage] = []
        last_updated = file_mtime
        for row in ordered:
            prompt = row.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            # Two distinct turns may carry the same prompt text: a re-emitted
            # checkpoint is recognised by its own id (or message id), never by content.
            row_id = row.get("id")
            message_ref = row.get("messageId")
            key: Any = (
                ("id", row_id)
                if isinstance(row_id, str) and row_id
                else ("message", message_ref)
                if isinstance(message_ref, str) and message_ref
                else ("turn", _turn_number(row), prompt)
            )
            if key in seen_turns:
                continue
            seen_turns.add(key)
            ts = _safe_ts(row.get("createdAt"), file_mtime)
            if ts > last_updated:
                last_updated = ts
            messages.append(
                UnifiedMessage(role=Role.USER, content=prompt, timestamp=ts, kind="text")
            )

        if not messages:
            return None

        project_path = self._project_path_from_meta(meta, None)
        origin = self._origin_from_meta(meta, has_transcript=False)
        title = _clean_str(meta.get("title"))
        thread_id = make_thread_id(project_path=project_path) if project_path else None
        session = UnifiedSession(
            tool=Tool.COMMAND_CODE,
            session_id=conv_id,
            created_at=messages[0].timestamp,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            origin=origin,
            source_path=str(path),
            parser_version=self.PARSER_VERSION,
        )
        return session

    def _build_flat_session(
        self,
        session_file: Path,
        conv_id: str,
        meta: Dict[str, Any],
        entries: List[Dict[str, Any]],
        fallback_time: datetime,
        git_branch: Optional[str],
    ) -> UnifiedSession:
        messages: List[UnifiedMessage] = []
        last_updated = fallback_time
        project_path = self._project_path_from_meta(meta, None)
        for entry in entries:
            role_str = entry.get("role")
            ts = _safe_ts(entry.get("timestamp"), fallback_time)
            if ts > last_updated:
                last_updated = ts
            cwd = _clean_path(entry.get("cwd"))
            if cwd and not project_path:
                project_path = cwd
            msg = self._message_from_payload(
                role_str=role_str,
                blocks=entry.get("content"),
                entry_id=_clean_str(entry.get("id")),
                parent_id=None,
                timestamp=ts,
                model=_clean_str(entry.get("model")),
                usage=entry.get("usage") if isinstance(entry.get("usage"), dict) else None,
                tool_results={},
            )
            if msg is not None:
                messages.append(msg)

        title = _clean_str(meta.get("title"))
        origin = self._origin_from_meta(meta, has_transcript=True)
        created_at = messages[0].timestamp if messages else fallback_time
        thread_id = make_thread_id(project_path=project_path) if project_path else None
        return UnifiedSession(
            tool=Tool.COMMAND_CODE,
            session_id=conv_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            git_branch=git_branch,
            origin=origin,
            source_path=str(session_file),
            parser_version=self.PARSER_VERSION,
        )

    def _build_v3_session(
        self,
        session_file: Path,
        session_id: str,
        project_path: Optional[str],
        created_at: datetime,
        source_version: Optional[str],
        origin: str,
        title: Optional[str],
        entries: List[Dict[str, Any]],
        fallback_time: datetime,
        git_branch: Optional[str],
    ) -> UnifiedSession:
        known_entries: Dict[str, Dict[str, Any]] = {}
        last_leaf_id: Optional[str] = None
        for entry in entries:
            eid = entry["id"]
            known_entries[eid] = entry
            last_leaf_id = eid

        active_branch_ids: List[str] = []
        visited: set[str] = set()
        curr_id = last_leaf_id
        while curr_id is not None and curr_id in known_entries:
            if curr_id in visited:
                break
            visited.add(curr_id)
            active_branch_ids.append(curr_id)
            parent = known_entries[curr_id].get("parentId")
            if not isinstance(parent, str):
                break
            curr_id = parent
        active_branch_ids.reverse()
        active_ids_set = set(active_branch_ids)
        for eid in known_entries:
            if eid not in active_ids_set:
                self._count_record("entries_off_branch")

        tool_results_by_use_id: Dict[str, bool] = {}
        for eid in active_branch_ids:
            entry = known_entries[eid]
            msg_obj = entry.get("message")
            if not isinstance(msg_obj, dict):
                continue
            for block in self._content_blocks(msg_obj.get("content")):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    use_id = block.get("tool_use_id")
                    if isinstance(use_id, str):
                        tool_results_by_use_id[use_id] = block.get("is_error") is True

        messages: List[UnifiedMessage] = []
        last_updated = created_at
        for eid in active_branch_ids:
            entry = known_entries[eid]
            ts = _safe_ts(entry.get("timestamp"), fallback_time)
            if ts > last_updated:
                last_updated = ts
            parent_id = entry.get("parentId")
            parent_id_str = parent_id if isinstance(parent_id, str) else None
            msg_obj = entry.get("message")
            if not isinstance(msg_obj, dict):
                continue
            role_str = msg_obj.get("role")
            model = _clean_str(entry.get("model"))
            usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else None
            msg = self._message_from_payload(
                role_str=role_str,
                blocks=msg_obj.get("content"),
                entry_id=eid,
                parent_id=parent_id_str,
                timestamp=ts,
                model=model,
                usage=usage,
                tool_results=tool_results_by_use_id,
            )
            if msg is not None:
                messages.append(msg)

        thread_id = make_thread_id(project_path=project_path) if project_path else None
        return UnifiedSession(
            tool=Tool.COMMAND_CODE,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            git_branch=git_branch,
            origin=origin,
            source_version=source_version,
            source_path=str(session_file),
            parser_version=self.PARSER_VERSION,
        )

    def _message_from_payload(
        self,
        role_str: Any,
        blocks: Any,
        entry_id: Optional[str],
        parent_id: Optional[str],
        timestamp: datetime,
        model: Optional[str],
        usage: Optional[Dict[str, Any]],
        tool_results: Dict[str, bool],
    ) -> Optional[UnifiedMessage]:
        if role_str == "user":
            if self._is_tool_result_only(blocks):
                content = self._format_tool_result_blocks(blocks)
                return UnifiedMessage(
                    role=Role.TOOL,
                    content=content,
                    timestamp=timestamp,
                    message_id=entry_id,
                    kind="text",
                    parent_id=parent_id,
                )
            content_str = self._format_user_blocks(blocks)
            return UnifiedMessage(
                role=Role.USER,
                content=content_str,
                timestamp=timestamp,
                message_id=entry_id,
                kind="text",
                parent_id=parent_id,
            )
        if role_str == "assistant":
            content_text, reasoning, tool_events = self._format_assistant_blocks(
                blocks, tool_results
            )
            tokens = self._format_usage(usage)
            return UnifiedMessage(
                role=Role.ASSISTANT,
                content=content_text,
                timestamp=timestamp,
                message_id=entry_id,
                model=model,
                tokens=tokens,
                reasoning=reasoning,
                kind="text",
                parent_id=parent_id,
                tool_events=tool_events,
            )
        if role_str in ("tool", "toolResult"):
            return UnifiedMessage(
                role=Role.TOOL,
                content=self._format_tool_result_blocks(blocks),
                timestamp=timestamp,
                message_id=entry_id,
                kind="text",
                parent_id=parent_id,
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
            elif b_type == "thinking":
                thinking = block.get("thinking", "")
                if isinstance(thinking, str) and thinking:
                    reasoning_parts.append(thinking)
            elif b_type == "tool_use":
                tool_name = block.get("name", "tool")
                if not isinstance(tool_name, str):
                    tool_name = "tool"
                tool_input = block.get("input")
                rendered_args: List[str] = []
                if isinstance(tool_input, dict):
                    for key, value in tool_input.items():
                        v_str = str(value)
                        if len(v_str) > _MAX_TOOL_ARG_CHARS:
                            v_str = v_str[:_MAX_TOOL_ARG_CHARS]
                        rendered_args.append(f"{key}={v_str}")
                elif tool_input is not None:
                    v_str = str(tool_input)
                    if len(v_str) > _MAX_TOOL_ARG_CHARS:
                        v_str = v_str[:_MAX_TOOL_ARG_CHARS]
                    rendered_args.append(v_str)
                suffix = (" " + " ".join(rendered_args)) if rendered_args else ""
                text_parts.append(f"[Tool: {tool_name}]{suffix}")
                use_id = block.get("id")
                is_error = bool(tool_results.get(use_id)) if isinstance(use_id, str) else False
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

    @staticmethod
    def _format_usage(raw_usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
        if not isinstance(raw_usage, dict):
            return None
        tokens: Dict[str, int] = {}
        mapping = (
            ("inputTokens", "input"),
            ("outputTokens", "output"),
            ("cacheReadTokens", "cache_read"),
            ("cacheWriteTokens", "cache_write"),
        )
        for src, dst in mapping:
            val = raw_usage.get(src)
            if isinstance(val, (int, float)):
                tokens[dst] = int(val)
        if tokens:
            total = tokens.get("input", 0) + tokens.get("output", 0)
            tokens["total"] = total
        return tokens or None
