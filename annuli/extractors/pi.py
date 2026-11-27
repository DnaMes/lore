"""Pi coding agent extractor (version 3 tree JSONL, issue #197 / task B2.1).

Extracts conversation sessions from Pi (and sibling harnesses OMP and Prime):
- ~/.pi/agent/sessions/--<cwd-slug>--/<ts>_<uuid>.jsonl
- ~/.omp/agent/sessions/--<cwd-slug>--/<ts>_<uuid>.jsonl
- ~/.prime/agent/sessions/--<cwd-slug>--/<ts>_<uuid>.jsonl
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_TOOL_ARG_CHARS = 200
_MAX_PATH_CHARS = 4096


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    """Parse a timestamp safely from untrusted input; fallback if invalid or out of range."""
    try:
        # Every timestamp in a session must be naive so entries can be compared.
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError):
        return fallback


def _clean_path(value: Any) -> Optional[str]:
    """Return value if it is a plain absolute path without '..' or control characters."""
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
    """Return non-empty string or None."""
    if isinstance(value, str) and value:
        return value
    return None


class PiExtractor(BaseExtractor):
    """Extracts chat history from Pi coding agent and sibling v3 tree JSONL transcripts."""

    tool = Tool.PI
    PARSER_VERSION = 1

    def __init__(self, base_paths: Optional[List[Path]] = None) -> None:
        if base_paths is not None:
            self.base_paths = base_paths
        else:
            self.base_paths = self._discover_base_paths()
        self.record_counts: Dict[str, int] = {}

    def _discover_base_paths(self) -> List[Path]:
        """Discover sessions directories for Pi and sibling harnesses (~/.pi, ~/.omp, ~/.prime)."""
        roots = [
            ".pi/agent/sessions",
            ".omp/agent/sessions",
            ".prime/agent/sessions",
        ]
        discovered: List[Path] = []
        # discover_home_marker_paths resolves symlinks, so a symlinked sessions root
        # is only visible on the declared default location: exclude its target.
        symlink_targets: set[Path] = set()
        for rel in roots:
            declared = Path.home() / rel
            try:
                if declared.is_symlink():
                    symlink_targets.add(declared.resolve())
            except OSError:
                continue
        for rel in roots:
            for candidate in discover_home_marker_paths(rel):
                if candidate in symlink_targets:
                    continue
                if candidate.is_dir() and candidate not in discovered:
                    discovered.append(candidate)
        return discovered

    def source_description(self) -> str:
        return (
            "~/.pi/agent/sessions/--*--/*.jsonl, "
            "~/.omp/agent/sessions/--*--/*.jsonl, "
            "~/.prime/agent/sessions/--*--/*.jsonl"
        )

    def is_available(self) -> bool:
        return len(self.base_paths) > 0

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n
        if key == "unknown_records":
            self._count("records_unknown", n)

    @staticmethod
    def _is_plain_file(path: Path) -> bool:
        """Regular file, not a symlink."""
        try:
            return not path.is_symlink() and path.is_file()
        except OSError:
            return False

    def _session_files(self) -> Iterator[Path]:
        for base_path in self.base_paths:
            if not base_path.is_dir():
                continue
            for project_dir in sorted(base_path.iterdir()):
                if not project_dir.is_dir() or project_dir.is_symlink():
                    continue
                for session_file in sorted(project_dir.glob("*.jsonl")):
                    if session_file.parent.name == "subagent-artifacts":
                        continue
                    if self._is_plain_file(session_file):
                        self._count("files_matched")
                        yield session_file

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        for session_file in self._session_files():
            if self._source_unchanged(session_file):
                continue
            try:
                if session_file.stat().st_size > _MAX_FILE_BYTES:
                    self._count_record("skipped_oversize")
                    continue
                session = self._parse_session_file(session_file)
                if session is None:
                    continue
                self._count("files_parsed")
                if self.should_import_session(session):
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

    def _parse_session_file(self, session_file: Path) -> Optional[UnifiedSession]:
        file_mtime = datetime.fromtimestamp(session_file.stat().st_mtime)

        header: Dict[str, Any] = {}
        entries: List[Dict[str, Any]] = []

        with open(session_file, "r", encoding="utf-8", errors="replace") as f:
            while True:
                line = f.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                    self._count_record("malformed_lines")
                    while line and not line.endswith("\n"):
                        line = f.readline(_MAX_LINE_CHARS)
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

                # Normal tree entries have id (8 hex or str) and timestamp
                rec_id = rec.get("id")
                if not isinstance(rec_id, str):
                    self._count_record("unknown_records")
                    continue

                entries.append(rec)

        raw_session_id = _clean_str(header.get("id")) or session_file.stem
        raw_cwd = _clean_path(header.get("cwd"))
        created_at = _safe_ts(header.get("timestamp"), file_mtime)
        parent_session_raw = _clean_str(header.get("parentSession"))

        parent_session_id: Optional[str] = None
        relation: Optional[str] = None
        if parent_session_raw:
            parent_session_id = Path(parent_session_raw).stem
            relation = "fork"

        source_version = str(header.get("version")) if header.get("version") is not None else "3"

        return self._build_session(
            session_file=session_file,
            session_id=raw_session_id,
            project_path=raw_cwd,
            created_at=created_at,
            parent_session_id=parent_session_id,
            relation=relation,
            source_version=source_version,
            entries=entries,
            fallback_time=file_mtime,
        )

    def _build_session(
        self,
        session_file: Path,
        session_id: str,
        project_path: Optional[str],
        created_at: datetime,
        parent_session_id: Optional[str],
        relation: Optional[str],
        source_version: str,
        entries: List[Dict[str, Any]],
        fallback_time: datetime,
    ) -> UnifiedSession:
        # 1. Map entries by id (duplicate ids: last wins). Track last message-bearing leaf.
        # Unknown record types counted as unknown_records.
        known_entries: Dict[str, Dict[str, Any]] = {}
        message_bearing_types = {
            "message",
            "compaction",
            "branch_summary",
            "custom_message",
            "bashExecution",
        }
        recognized_types = message_bearing_types | {
            "model_change",
            "thinking_level_change",
            "context_edit",
            "custom",
            "label",
            "session_info",
            "usage",
        }

        last_leaf_id: Optional[str] = None
        for entry in entries:
            eid = entry["id"]
            etype = entry.get("type")
            if etype not in recognized_types:
                self._count_record("unknown_records")

            known_entries[eid] = entry
            # Pi appends every record (messages, edits, titles, labels) to the current
            # leaf, so the last record in file order is the terminal entry of the active
            # branch. Walking from it keeps trailing context_edit / session_info records
            # on the branch; only message-bearing entries are emitted later.
            last_leaf_id = eid

        # 2. Walk active branch from last_leaf_id back to root
        active_branch_ids: List[str] = []
        visited = set()
        curr_id = last_leaf_id
        while curr_id is not None and curr_id in known_entries:
            if curr_id in visited:
                # cycle: stop
                break
            visited.add(curr_id)
            active_branch_ids.append(curr_id)
            parent = known_entries[curr_id].get("parentId")
            if not isinstance(parent, str):
                break
            curr_id = parent

        # Root to leaf order
        active_branch_ids.reverse()
        active_ids_set = set(active_branch_ids)

        # Count abandoned branch entries in record_counts / discovery
        for eid, entry in known_entries.items():
            if eid not in active_ids_set:
                self._count_record("entries_off_branch")

        # 3. Handle context edits on active branch (and any context_edit in known_entries?)
        # "context_edit {targetId, replacement}: with replacement null the target message is removed from model context: keep the message in the archive but set active=False."
        # Context edits can appear anywhere on active branch.
        deactivated_ids = set()
        session_title: Optional[str] = None

        # Also collect tool results by toolCallId to correlate tool_events
        # toolResult: toolCallId -> {status, duration_ms, is_error}
        tool_results_by_call_id: Dict[str, Dict[str, Any]] = {}

        for eid in active_branch_ids:
            entry = known_entries[eid]
            etype = entry.get("type")
            if etype == "context_edit":
                target_id = entry.get("targetId")
                replacement = entry.get("replacement")
                if isinstance(target_id, str) and target_id and replacement is None:
                    deactivated_ids.add(target_id)
            elif etype == "session_info":
                name = entry.get("name")
                if isinstance(name, str) and name.strip():
                    session_title = name.strip()
            elif etype == "message":
                msg = entry.get("message")
                if isinstance(msg, dict) and msg.get("role") == "toolResult":
                    call_id = msg.get("toolCallId")
                    if isinstance(call_id, str):
                        is_error = bool(msg.get("isError", False))
                        status = "failed" if is_error else "completed"
                        duration_ms = msg.get("durationMs")
                        tool_results_by_call_id[call_id] = {
                            "status": status,
                            "duration_ms": duration_ms
                            if isinstance(duration_ms, (int, float))
                            else None,
                            "is_error": is_error,
                        }

        # 4. Construct UnifiedMessage list for active branch
        messages: List[UnifiedMessage] = []
        last_updated = created_at

        for eid in active_branch_ids:
            entry = known_entries[eid]
            etype = entry.get("type")
            ts = _safe_ts(entry.get("timestamp"), fallback_time)
            if ts > last_updated:
                last_updated = ts

            parent_id = entry.get("parentId")
            parent_id_str = parent_id if isinstance(parent_id, str) else None
            is_active = eid not in deactivated_ids

            if etype == "message":
                msg_obj = entry.get("message")
                if not isinstance(msg_obj, dict):
                    continue
                role_str = msg_obj.get("role")
                if role_str == "system":
                    # Skip system prompt sections
                    continue

                if role_str == "user":
                    content_str = self._format_user_content(msg_obj.get("content"))
                    messages.append(
                        UnifiedMessage(
                            role=Role.USER,
                            content=content_str,
                            timestamp=ts,
                            message_id=eid,
                            kind="text",
                            parent_id=parent_id_str,
                            active=is_active,
                        )
                    )
                elif role_str == "assistant":
                    content_text, reasoning, tool_events = self._format_assistant_message(
                        msg_obj, tool_results_by_call_id
                    )
                    tokens = self._format_usage(msg_obj.get("usage"))
                    model = _clean_str(msg_obj.get("model"))
                    messages.append(
                        UnifiedMessage(
                            role=Role.ASSISTANT,
                            content=content_text,
                            timestamp=ts,
                            message_id=eid,
                            model=model,
                            tokens=tokens,
                            reasoning=reasoning,
                            kind="text",
                            parent_id=parent_id_str,
                            active=is_active,
                            tool_events=tool_events,
                        )
                    )
                elif role_str == "toolResult":
                    content_str = self._format_tool_result_content(msg_obj.get("content"))
                    messages.append(
                        UnifiedMessage(
                            role=Role.TOOL,
                            content=content_str,
                            timestamp=ts,
                            message_id=eid,
                            kind="text",
                            parent_id=parent_id_str,
                            active=is_active,
                        )
                    )
                elif role_str == "bashExecution":
                    # Documented bash execution message
                    cmd = msg_obj.get("command", "")
                    output = msg_obj.get("output", "")
                    content_str = f"[Tool: Bash]\n```bash\n{cmd}\n```\n{output}"
                    messages.append(
                        UnifiedMessage(
                            role=Role.TOOL,
                            content=content_str,
                            timestamp=ts,
                            message_id=eid,
                            kind="text",
                            parent_id=parent_id_str,
                            active=is_active,
                        )
                    )
                else:
                    self._count_record("unknown_records")

            elif etype == "compaction":
                summary = entry.get("summary", "")
                if not isinstance(summary, str):
                    summary = ""
                messages.append(
                    UnifiedMessage(
                        role=Role.INFO,
                        content=summary,
                        timestamp=ts,
                        message_id=eid,
                        kind="compaction_summary",
                        parent_id=parent_id_str,
                        active=is_active,
                    )
                )

            elif etype == "branch_summary":
                summary = entry.get("summary", "")
                if not isinstance(summary, str):
                    summary = ""
                messages.append(
                    UnifiedMessage(
                        role=Role.INFO,
                        content=summary,
                        timestamp=ts,
                        message_id=eid,
                        kind="compaction_summary",
                        parent_id=parent_id_str,
                        active=is_active,
                    )
                )

            elif etype == "custom_message":
                content = entry.get("content", "")
                if not isinstance(content, str):
                    content = str(content) if content is not None else ""
                messages.append(
                    UnifiedMessage(
                        role=Role.INFO,
                        content=content,
                        timestamp=ts,
                        message_id=eid,
                        kind="injected_context",
                        parent_id=parent_id_str,
                        active=is_active,
                    )
                )

        thread_id = None
        if project_path:
            thread_id = make_thread_id(project_path=project_path)

        session = UnifiedSession(
            tool=Tool.PI,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=session_title,
            title_source=TitleSource.NATIVE if session_title else None,
            parent_session_id=parent_session_id,
            relation=relation,
            source_version=source_version,
            source_path=str(session_file),
            parser_version=self.PARSER_VERSION,
        )
        return session

    @staticmethod
    def _format_user_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, str):
                    parts.append(b)
                elif isinstance(b, dict):
                    if b.get("type") == "text" and isinstance(b.get("text"), str):
                        parts.append(b["text"])
                    elif isinstance(b.get("content"), str):
                        parts.append(b["content"])
            return "\n".join(parts)
        return "" if content is None else str(content)

    @staticmethod
    def _format_tool_result_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, str):
                    parts.append(b)
                elif isinstance(b, dict):
                    b_type = b.get("type")
                    if b_type == "text":
                        text = b.get("text")
                        parts.append(text if isinstance(text, str) else "")
                    elif b_type == "image":
                        parts.append("[image]")
                    elif "text" in b:
                        parts.append(str(b["text"]))
            return "\n".join(parts)
        return "" if content is None else str(content)

    @classmethod
    def _format_assistant_message(
        cls, msg: Dict[str, Any], tool_results: Dict[str, Dict[str, Any]]
    ) -> tuple[str, Optional[str], List[Dict[str, Any]]]:
        raw_content = msg.get("content")
        blocks: List[Any] = []
        if isinstance(raw_content, list):
            blocks = raw_content
        elif isinstance(raw_content, dict):
            blocks = [raw_content]
        elif isinstance(raw_content, str):
            blocks = [{"type": "text", "text": raw_content}]

        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_events: List[Dict[str, Any]] = []

        for block in blocks:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    text_parts.append(block)
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
            elif b_type == "toolCall":
                tool_call_id = block.get("id")
                tool_name = block.get("name", "tool")
                args = block.get("arguments", {})

                # Render inline tool call [Tool: <name>] capped at 200 chars per argument value
                rendered_args = []
                if isinstance(args, dict):
                    for k, v in args.items():
                        v_str = str(v)
                        if len(v_str) > _MAX_TOOL_ARG_CHARS:
                            v_str = v_str[:_MAX_TOOL_ARG_CHARS]
                        rendered_args.append(f"{k}={v_str}")
                args_suffix = (" " + " ".join(rendered_args)) if rendered_args else ""
                text_parts.append(f"[Tool: {tool_name}]{args_suffix}")

                # Tool event status from matching toolResult
                result_info = (
                    tool_results.get(tool_call_id)
                    if isinstance(tool_call_id, str) and tool_call_id
                    else None
                )
                if result_info:
                    status = result_info["status"]
                    duration_ms = result_info["duration_ms"]
                    is_error = result_info["is_error"]
                else:
                    status = "completed"
                    duration_ms = None
                    is_error = False

                tool_events.append(
                    {
                        "name": tool_name,
                        "status": status,
                        "duration_ms": duration_ms,
                        "is_error": is_error,
                    }
                )

        content_text = "\n".join(text_parts)
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        return content_text, reasoning, tool_events

    @staticmethod
    def _format_usage(raw_usage: Any) -> Optional[Dict[str, int]]:
        if not isinstance(raw_usage, dict):
            return None
        # usage: {input, output, cacheRead, cacheWrite, totalTokens, ...}
        tokens: Dict[str, int] = {}
        if "input" in raw_usage and isinstance(raw_usage["input"], (int, float)):
            tokens["input"] = int(raw_usage["input"])
        if "output" in raw_usage and isinstance(raw_usage["output"], (int, float)):
            tokens["output"] = int(raw_usage["output"])
        if "cacheRead" in raw_usage and isinstance(raw_usage["cacheRead"], (int, float)):
            tokens["cache_read"] = int(raw_usage["cacheRead"])
        if "cacheWrite" in raw_usage and isinstance(raw_usage["cacheWrite"], (int, float)):
            tokens["cache_write"] = int(raw_usage["cacheWrite"])
        if "totalTokens" in raw_usage and isinstance(raw_usage["totalTokens"], (int, float)):
            tokens["total"] = int(raw_usage["totalTokens"])
        return tokens or None
