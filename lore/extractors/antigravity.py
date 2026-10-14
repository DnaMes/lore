"""Antigravity AI chat history extractor.

Reads plaintext JSONL transcripts under brain/<id>/.system_generated/logs/
and enriches from conversation_summaries.db. Falls back to task.md / walkthrough.md.
Does not decode conversations/*.pb or protobuf payloads.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set
from urllib.parse import unquote, urlparse

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_TOOL_ARG_CHARS = 200
_MAX_TOOL_OUTPUT_CHARS = 20_000
_MAX_PATH_CHARS = 4096
_MAX_SQLITE_BYTES = 512 * 1024 * 1024
_SQLITE_TIMEOUT = 2.0
_MAX_SUMMARY_ROWS = 20_000
_MAX_SUMMARY_TEXT_CHARS = 200
_SQLITE_QUERY_SECONDS = 5.0

_GEMINI_VARIANTS = ("antigravity", "antigravity-cli", "antigravity-ide")


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    try:
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError, RecursionError):
        return fallback


def _valid_session_id(session_id: str) -> bool:
    if not session_id or len(session_id) > 256:
        return False
    return "/" not in session_id and "\\" not in session_id and "\x00" not in session_id


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _status_is_error(status: Any) -> bool:
    if not isinstance(status, str):
        return False
    low = status.lower()
    return "error" in low or "fail" in low


def _workspace_from_uris(value: Any) -> Optional[str]:
    uris: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            uris = json.loads(text)
        except json.JSONDecodeError:
            uris = [text]
    if not isinstance(uris, list):
        return None
    for entry in uris:
        if not isinstance(entry, str) or not entry.startswith("file://"):
            continue
        parsed = urlparse(entry)
        path = unquote(parsed.path)
        if not path or ".." in Path(path).parts:
            continue
        if len(path) > _MAX_PATH_CHARS:
            continue
        if path.startswith("/"):
            return path
    return None


@dataclass
class _ConversationMeta:
    title: Optional[str]
    project_path: Optional[str]
    parent_conversation_id: Optional[str]


def _load_conversation_summaries(
    antigravity_base: Path, max_bytes: int
) -> Dict[str, _ConversationMeta]:
    """Read titles and workspace from a scratch copy of conversation_summaries.db."""
    db_path = antigravity_base / "conversation_summaries.db"
    result: Dict[str, _ConversationMeta] = {}
    rows: List[tuple] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), max_bytes)
            if copy_path is None:
                return {}
            conn = sqlite3.connect(
                f"file:{copy_path}?mode=ro",
                uri=True,
                timeout=_SQLITE_TIMEOUT,
            )
            try:
                deadline = time.monotonic() + _SQLITE_QUERY_SECONDS
                conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
                cursor = conn.execute(
                    "SELECT conversation_id, substr(title, 1, ?), substr(workspace_uris, 1, ?), "
                    "substr(parent_conversation_id, 1, ?) FROM conversation_summaries LIMIT ?",
                    (
                        _MAX_SUMMARY_TEXT_CHARS,
                        _MAX_SUMMARY_TEXT_CHARS,
                        _MAX_SUMMARY_TEXT_CHARS,
                        _MAX_SUMMARY_ROWS,
                    ),
                )
                while len(rows) < _MAX_SUMMARY_ROWS:
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
        conv_id, title, workspace_uris, parent_id = row[0], row[1], row[2], row[3]
        if not isinstance(conv_id, str) or not conv_id:
            continue
        native_title = title.strip() if isinstance(title, str) and title.strip() else None
        parent = parent_id.strip() if isinstance(parent_id, str) and parent_id.strip() else None
        project = _workspace_from_uris(workspace_uris)
        result[conv_id] = _ConversationMeta(
            title=native_title,
            project_path=project,
            parent_conversation_id=parent,
        )
    return result


class AntigravityExtractor(BaseExtractor):
    """Extract chat history from Antigravity AI."""

    PARSER_VERSION = 2

    def __init__(self) -> None:
        self.base_paths = self._discover_base_paths()

    def _discover_base_paths(self) -> List[Path]:
        paths: List[Path] = []
        for variant in _GEMINI_VARIANTS:
            candidate = Path.home() / ".gemini" / variant
            if candidate.exists() and candidate not in paths:
                paths.append(candidate)
        for candidate in discover_home_marker_paths(".gemini/antigravity"):
            parent = candidate
            if parent.name == "antigravity" and parent not in paths:
                paths.append(parent)
        return paths

    @property
    def tool(self) -> Tool:
        return Tool.ANTIGRAVITY

    def is_available(self) -> bool:
        return any((path / "brain").exists() for path in self.base_paths)

    def source_description(self) -> str:
        return (
            "~/.gemini/antigravity{,-cli,-ide}/brain/*/"
            ".system_generated/logs/transcript_full.jsonl (or transcript.jsonl); "
            "conversation_summaries.db; fallback task.md / walkthrough.md"
        )

    @staticmethod
    def _is_plain_file(path: Path) -> bool:
        try:
            return not path.is_symlink() and path.is_file()
        except OSError:
            return False

    def _transcript_path(self, session_dir: Path) -> Optional[Path]:
        logs = session_dir / ".system_generated" / "logs"
        full = logs / "transcript_full.jsonl"
        short = logs / "transcript.jsonl"
        if self._is_plain_file(full):
            return full
        if self._is_plain_file(short):
            return short
        return None

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        summaries_base = Path.home() / ".gemini" / "antigravity"
        summaries = _load_conversation_summaries(summaries_base, _MAX_SQLITE_BYTES)

        seen_ids: set[str] = set()
        for base_path in self.base_paths:
            brain_path = base_path / "brain"
            try:
                # A symlinked brain directory would redirect the listing outside the tool's
                # data directory: never follow it.
                if brain_path.is_symlink() or not brain_path.is_dir():
                    continue
            except OSError:
                continue
            try:
                children = list(brain_path.iterdir())
            except OSError:
                continue
            for session_dir in children:
                if not session_dir.is_dir() or session_dir.is_symlink():
                    continue
                if not _valid_session_id(session_dir.name):
                    continue
                if session_dir.name in seen_ids:
                    # The same conversation id in two variant roots (antigravity,
                    # -cli, -ide) is one conversation: the first root wins.
                    self._count("duplicate_sessions")
                    continue

                self._count("files_matched")
                try:
                    session = self._parse_session_dir(session_dir, summaries)
                except (
                    OverflowError,
                    RecursionError,
                    ValueError,
                    TypeError,
                    OSError,
                ) as e:
                    logger.warning(
                        "Failed to parse Antigravity session %s: %s",
                        session_dir,
                        type(e).__name__,
                    )
                    self._record_skip("parse_error")
                    continue

                if session is None:
                    if (
                        self._transcript_path(session_dir) is None
                        and not (session_dir / "task.md").exists()
                    ):
                        self._record_skip("no_task_md")
                    continue
                self._count("files_parsed")
                if self.should_import_session(session):
                    seen_ids.add(session_dir.name)
                    yield session

    def _parse_session_dir(
        self,
        session_dir: Path,
        summaries: Dict[str, _ConversationMeta],
    ) -> Optional[UnifiedSession]:
        transcript = self._transcript_path(session_dir)
        if transcript is not None:
            if self._source_unchanged(transcript):
                return None
            return self._parse_transcript_session(session_dir, transcript, summaries)

        return self._parse_task_fallback(session_dir)

    def _apply_summary_meta(
        self,
        session: UnifiedSession,
        summaries: Dict[str, _ConversationMeta],
    ) -> None:
        meta = summaries.get(session.session_id)
        if meta is None:
            session.origin = "unknown"
            return
        if meta.title:
            session.title = meta.title
            session.title_source = TitleSource.NATIVE
        if meta.project_path and not session.project_path:
            session.project_path = meta.project_path
            session.thread_id = make_thread_id(project_path=meta.project_path)
        if meta.parent_conversation_id:
            session.parent_session_id = meta.parent_conversation_id
            session.relation = "subagent"
            session.origin = "subagent"
        else:
            session.origin = "unknown"

    def _parse_transcript_session(
        self,
        session_dir: Path,
        transcript_path: Path,
        summaries: Dict[str, _ConversationMeta],
    ) -> Optional[UnifiedSession]:
        session_id = session_dir.name
        try:
            if transcript_path.stat().st_size > _MAX_FILE_BYTES:
                return None
        except OSError:
            return None

        file_mtime = datetime.fromtimestamp(transcript_path.stat().st_mtime)
        messages: List[UnifiedMessage] = []
        created_at: Optional[datetime] = None
        last_updated: Optional[datetime] = None
        unknown_seen: Set[str] = set()

        def touch_ts(ts: datetime) -> None:
            nonlocal created_at, last_updated
            if created_at is None or ts < created_at:
                created_at = ts
            if last_updated is None or ts > last_updated:
                last_updated = ts

        def mark_unknown(kind: str) -> None:
            if kind in unknown_seen:
                return
            unknown_seen.add(kind)
            self._count("records_unknown")

        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
                while True:
                    line = handle.readline(_MAX_LINE_CHARS)
                    if not line:
                        break
                    if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                        mark_unknown("malformed_line")
                        while line and not line.endswith("\n"):
                            line = handle.readline(_MAX_LINE_CHARS)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, RecursionError):
                        mark_unknown("malformed_line")
                        continue
                    if not isinstance(record, dict):
                        mark_unknown("malformed_line")
                        continue

                    ts = _safe_ts(record.get("created_at"), file_mtime)
                    touch_ts(ts)
                    rec_type = record.get("type")
                    source = record.get("source")

                    if rec_type == "USER_INPUT" and source == "USER_EXPLICIT":
                        content = _coerce_str(record.get("content"))
                        messages.append(
                            UnifiedMessage(
                                role=Role.USER,
                                content=content,
                                timestamp=ts,
                                message_id=f"{session_id}_u{record.get('step_index', len(messages))}",
                            )
                        )
                    elif rec_type == "PLANNER_RESPONSE":
                        content_parts: List[str] = []
                        thinking = record.get("thinking")
                        reasoning = thinking if isinstance(thinking, str) and thinking else None
                        assistant_text = _coerce_str(record.get("content"))
                        if assistant_text:
                            content_parts.append(assistant_text)
                        tool_events: List[Dict[str, Any]] = []
                        tool_calls = record.get("tool_calls")
                        if isinstance(tool_calls, list):
                            for call in tool_calls:
                                if not isinstance(call, dict):
                                    continue
                                name = call.get("name")
                                tool_name = name if isinstance(name, str) and name else "tool"
                                args = call.get("args")
                                args_suffix = ""
                                if isinstance(args, dict):
                                    rendered = []
                                    for key, val in args.items():
                                        v_str = str(val)
                                        if len(v_str) > _MAX_TOOL_ARG_CHARS:
                                            v_str = v_str[:_MAX_TOOL_ARG_CHARS]
                                        rendered.append(f"{key}={v_str}")
                                    if rendered:
                                        args_suffix = " " + " ".join(rendered)
                                content_parts.append(f"[Tool: {tool_name}]{args_suffix}")
                                is_error = _status_is_error(record.get("status"))
                                tool_events.append(
                                    {
                                        "name": tool_name,
                                        "status": "failed" if is_error else "completed",
                                        "duration_ms": None,
                                        "is_error": is_error,
                                    }
                                )
                        content = "\n".join(content_parts)
                        msg = UnifiedMessage(
                            role=Role.ASSISTANT,
                            content=content,
                            timestamp=ts,
                            reasoning=reasoning,
                            tool_events=tool_events,
                            message_id=f"{session_id}_a{record.get('step_index', len(messages))}",
                        )
                        messages.append(msg)
                    elif rec_type == "GENERIC":
                        content = _coerce_str(record.get("content"))
                        if content:
                            if len(content) > _MAX_TOOL_OUTPUT_CHARS:
                                content = content[:_MAX_TOOL_OUTPUT_CHARS]
                            messages.append(
                                UnifiedMessage(
                                    role=Role.TOOL,
                                    content=content,
                                    timestamp=ts,
                                    message_id=f"{session_id}_t{record.get('step_index', len(messages))}",
                                )
                            )
                    elif rec_type in ("SYSTEM_MESSAGE", "CHECKPOINT"):
                        content = _coerce_str(record.get("content"))
                        messages.append(
                            UnifiedMessage(
                                role=Role.INFO,
                                content=content,
                                timestamp=ts,
                                kind="injected_context",
                                message_id=f"{session_id}_i{record.get('step_index', len(messages))}",
                            )
                        )
                    elif rec_type == "ERROR_MESSAGE":
                        content = _coerce_str(record.get("content"))
                        messages.append(
                            UnifiedMessage(
                                role=Role.INFO,
                                content=content,
                                timestamp=ts,
                                message_id=f"{session_id}_e{record.get('step_index', len(messages))}",
                            )
                        )
                    elif isinstance(rec_type, str):
                        mark_unknown(rec_type)
                    else:
                        mark_unknown("missing_type")
        except (OverflowError, RecursionError, ValueError, TypeError, OSError):
            return None

        if not messages:
            return None

        if created_at is None:
            created_at = file_mtime
        if last_updated is None:
            last_updated = file_mtime

        session = UnifiedSession(
            tool=Tool.ANTIGRAVITY,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            source_path=str(transcript_path),
            origin="unknown",
        )
        self._apply_summary_meta(session, summaries)
        return session

    def _parse_task_fallback(self, session_dir: Path) -> Optional[UnifiedSession]:
        session_id = session_dir.name
        task_md = session_dir / "task.md"
        task_metadata = session_dir / "task.md.metadata.json"
        walkthrough_md = session_dir / "walkthrough.md"
        walkthrough_metadata = session_dir / "walkthrough.md.metadata.json"

        if not task_md.exists():
            return None

        title = None
        summary = None
        created_at = datetime.now()
        last_updated = datetime.now()
        messages: List[UnifiedMessage] = []

        if task_metadata.exists():
            try:
                with open(task_metadata, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    summary = meta.get("summary")
                    if meta.get("updatedAt"):
                        last_updated = parse_timestamp(meta.get("updatedAt"))
                        created_at = last_updated
            except (json.JSONDecodeError, OSError):
                pass

        task_content = self._read_file(task_md)
        if task_content:
            title = self._extract_title_from_markdown(task_content)
            messages.append(
                UnifiedMessage(
                    role=Role.USER,
                    content=task_content,
                    timestamp=created_at,
                    message_id=f"{session_id}_task",
                )
            )

        if walkthrough_md.exists():
            walkthrough_content = self._read_file(walkthrough_md)
            if walkthrough_content:
                walkthrough_time = last_updated
                if walkthrough_metadata.exists():
                    try:
                        with open(walkthrough_metadata, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                            if meta.get("updatedAt"):
                                walkthrough_time = parse_timestamp(meta.get("updatedAt"))
                                if walkthrough_time > last_updated:
                                    last_updated = walkthrough_time
                    except (json.JSONDecodeError, OSError):
                        pass

                messages.append(
                    UnifiedMessage(
                        role=Role.ASSISTANT,
                        content=walkthrough_content,
                        timestamp=walkthrough_time,
                        message_id=f"{session_id}_walkthrough",
                    )
                )

        walkthrough_text = ""
        if walkthrough_md.exists():
            walkthrough_text = self._read_file(walkthrough_md) or ""

        project_path = self._extract_project_path(task_content or "", walkthrough_text)

        return UnifiedSession(
            tool=Tool.ANTIGRAVITY,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=(make_thread_id(project_path=project_path) if project_path else None),
            title=title,
            summary=summary,
            source_path=str(session_dir),
            origin="unknown",
        )

    def _read_file(self, path: Path) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    def _extract_title_from_markdown(self, content: str) -> Optional[str]:
        lines = content.strip().split("\n")
        for line in lines[:5]:
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return None

    def _extract_project_path(self, task_content: str, walkthrough_content: str) -> Optional[str]:
        combined = task_content + "\n" + walkthrough_content
        file_links = re.findall(r"file://([^\s\)]+)", combined)

        if file_links:
            first_path = file_links[0]
            parts = first_path.split("/")
            if len(parts) >= 4:
                for i, part in enumerate(parts):
                    if part in ("home", "Users"):
                        if i + 2 < len(parts):
                            potential_project = "/".join(parts[: i + 4])
                            if Path(potential_project).exists():
                                return potential_project
        return None
