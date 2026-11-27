"""Qwen Code extractor — tree-structured ``chats/*.jsonl`` (issue #197 / B2.8d)."""

from __future__ import annotations

import json
import logging
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
_MAX_RECORDS = 100_000
_MAX_TOTAL_CHARS = 64 * 1024 * 1024
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 20_000
_MAX_REASONING_CHARS = 20_000
_MAX_COMPACTION_CHARS = 100_000
_MAX_CONTENT_CHARS = 100_000
_MAX_MESSAGES = 20_000
_MAX_PATH_CHARS = 4096
_SESSION_FILE_RE = re.compile(r"^[0-9a-fA-F-]{32,36}\.jsonl$")
_MESSAGE_TYPES = frozenset({"user", "assistant", "tool_result"})
_INJECTED_USER_SUBTYPES = frozenset(
    {
        "agent_mention",
        "agent_message",
        "notification",
        "cron",
        "background_task_completed",
        "slash_command",
        "at_command",
    }
)


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


def _clean_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


class QwenExtractor(BaseExtractor):
    """Reads Qwen Code chat JSONL under ``~/.qwen/projects`` and legacy ``~/.qwen/tmp``."""

    tool = Tool.QWEN
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, qwen_root: Optional[Path] = None) -> None:
        self.qwen_root = qwen_root
        self.record_counts: Dict[str, int] = {}
        self._unknown_types: Set[str] = set()

    def source_description(self) -> str:
        return (
            "~/.qwen/projects/<slug>/chats/<session-id>.jsonl, "
            "~/.qwen/tmp/<project-id>/chats/<session-id>.jsonl"
        )

    def is_available(self) -> bool:
        return bool(self._chat_roots())

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n
        if key == "unknown_records":
            self._count("records_unknown", n)

    def _qwen_home(self) -> Path:
        if self.qwen_root is not None:
            return self.qwen_root
        return Path.home() / ".qwen"

    def _chat_roots(self) -> List[Path]:
        home = self._qwen_home()
        roots: List[Path] = []
        for rel in ("projects", "tmp"):
            base = home / rel
            try:
                if base.is_symlink() or not base.is_dir():
                    continue
            except OSError:
                continue
            try:
                for child in sorted(base.iterdir(), key=lambda p: p.name):
                    chats = child / "chats"
                    try:
                        if chats.is_symlink() or not chats.is_dir():
                            continue
                    except OSError:
                        continue
                    roots.append(chats)
            except OSError:
                continue
        return roots

    @staticmethod
    def _is_session_file(path: Path) -> bool:
        name = path.name
        if name.endswith(".ledger.jsonl"):
            return False
        return bool(_SESSION_FILE_RE.match(name))

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        for chats_dir in self._chat_roots():
            try:
                files = sorted(chats_dir.iterdir(), key=lambda p: p.name)
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

    def _parse_file(self, path: Path) -> Optional[UnifiedSession]:
        opened = _open_plain_file(path)
        if opened is None:
            return None
        stream, file_mtime = opened
        records: List[Dict[str, Any]] = []
        known: Dict[str, Dict[str, Any]] = {}
        read_chars = 0
        with stream as handle:
            while True:
                if len(records) >= _MAX_RECORDS or read_chars > _MAX_TOTAL_CHARS:
                    self._count_record("size_limit")  # the tree walk needs every kept record
                    break
                line = handle.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                read_chars += len(line)
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
                uid = rec.get("uuid")
                if not isinstance(uid, str):
                    self._count_record("malformed_lines")
                    continue
                known[uid] = rec
                records.append(rec)

        if not records:
            return None

        last_leaf: Optional[str] = None
        for rec in records:
            rtype = rec.get("type")
            if rtype in _MESSAGE_TYPES:
                last_leaf = rec["uuid"]

        if last_leaf is None:
            return None

        active_ids: List[str] = []
        visited: Set[str] = set()
        curr: Optional[str] = last_leaf
        while curr is not None and curr in known:
            if curr in visited:
                break
            visited.add(curr)
            active_ids.append(curr)
            parent = known[curr].get("parentUuid")
            if parent is None:
                break
            if not isinstance(parent, str):
                break
            curr = parent
        active_ids.reverse()
        active_set = set(active_ids)
        for uid in known:
            if uid not in active_set:
                self._count_record("entries_off_branch")

        first = records[0]
        project_path = _clean_path(first.get("cwd"))
        source_version = str(first.get("version")) if first.get("version") is not None else None
        session_id = path.stem
        title: Optional[str] = None
        parent_session_id: Optional[str] = None
        relation: Optional[str] = None
        origin = "interactive"

        tool_results: Dict[str, Dict[str, Any]] = {}
        for uid in active_ids:
            rec = known[uid]
            if rec.get("type") != "tool_result":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            for part in self._parts(msg.get("parts")):
                if not isinstance(part, dict):
                    continue
                fr = part.get("functionResponse")
                if isinstance(fr, dict):
                    fid = fr.get("id")
                    if isinstance(fid, str):
                        tool_results[fid] = rec

        messages: List[UnifiedMessage] = []
        created_at = file_mtime
        last_updated = file_mtime

        for uid in active_ids:
            if len(messages) >= _MAX_MESSAGES:
                self._count_record("message_limit")
                break
            rec = known[uid]
            ts = _safe_ts(rec.get("timestamp"), file_mtime)
            if ts < created_at:
                created_at = ts
            if ts > last_updated:
                last_updated = ts
            parent_uuid = rec.get("parentUuid")
            parent_id = parent_uuid if isinstance(parent_uuid, str) else None
            rtype = rec.get("type")
            subtype = rec.get("subtype")

            if rtype == "system":
                if subtype == "custom_title":
                    payload = rec.get("systemPayload")
                    if isinstance(payload, dict):
                        ct = payload.get("customTitle")
                        if isinstance(ct, str) and ct.strip():
                            title = ct.strip()[:_MAX_TITLE_CHARS]
                elif subtype == "chat_compression":
                    payload = rec.get("systemPayload")
                    summary = self._compaction_summary(payload)
                    if summary:
                        messages.append(
                            UnifiedMessage(
                                role=Role.INFO,
                                content=summary,
                                timestamp=ts,
                                message_id=uid,
                                kind="compaction_summary",
                                parent_id=parent_id,
                            )
                        )
                elif subtype == "parent_session":
                    payload = rec.get("systemPayload")
                    if isinstance(payload, dict):
                        psid = payload.get("parentSessionId")
                        if isinstance(psid, str) and psid.strip():
                            parent_session_id = psid.strip()
                            relation = "subagent"
                            origin = "subagent"
                continue

            if rtype == "user":
                if subtype in _INJECTED_USER_SUBTYPES:
                    text = self._user_text(rec)
                    if text:
                        messages.append(
                            UnifiedMessage(
                                role=Role.INFO,
                                content=text,
                                timestamp=ts,
                                message_id=uid,
                                kind="injected_context",
                                parent_id=parent_id,
                            )
                        )
                    continue
                if subtype is not None and subtype != "mid_turn_user_message":
                    if isinstance(subtype, str) and subtype not in self._unknown_types:
                        self._unknown_types.add(subtype)
                        self._count_record("unknown_records")
                    continue
                text = cap_text(self._user_text(rec), _MAX_CONTENT_CHARS)
                if text:
                    messages.append(
                        UnifiedMessage(
                            role=Role.USER,
                            content=text,
                            timestamp=ts,
                            message_id=uid,
                            kind="text",
                            parent_id=parent_id,
                        )
                    )
            elif rtype == "assistant":
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                text, reasoning, tool_events = self._format_assistant(msg, rec, tool_results)
                content = cap_text(text, _MAX_CONTENT_CHARS)
                tokens = self._usage_tokens(rec.get("usageMetadata"))
                model = _clean_str(rec.get("model"))
                if content or reasoning or tool_events:
                    messages.append(
                        UnifiedMessage(
                            role=Role.ASSISTANT,
                            content=content,
                            timestamp=ts,
                            message_id=uid,
                            model=model,
                            tokens=tokens,
                            reasoning=reasoning,
                            tool_events=tool_events,
                            kind="text",
                            parent_id=parent_id,
                        )
                    )
            elif rtype == "tool_result":
                content = cap_text(self._tool_result_text(rec), _MAX_BODY_CHARS)
                if content:
                    messages.append(
                        UnifiedMessage(
                            role=Role.TOOL,
                            content=content,
                            timestamp=ts,
                            message_id=uid,
                            kind="text",
                            parent_id=parent_id,
                        )
                    )
            else:
                if isinstance(rtype, str) and rtype not in self._unknown_types:
                    self._unknown_types.add(rtype)
                    self._count_record("unknown_records")

        if not messages:
            return None

        thread_id = make_thread_id(project_path=project_path) if project_path else None
        return UnifiedSession(
            tool=Tool.QWEN,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            parent_session_id=parent_session_id,
            relation=relation,
            origin=origin,
            source_version=source_version,
            source_path=str(path),
            parser_version=self.PARSER_VERSION,
        )

    @staticmethod
    def _parts(raw: Any) -> List[Any]:
        if isinstance(raw, list):
            return raw
        return []

    def _user_text(self, rec: Dict[str, Any]) -> str:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return ""
        parts: List[str] = []
        for part in self._parts(msg.get("parts")):
            if isinstance(part, dict) and part.get("text") and not part.get("thought"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    def _format_assistant(
        self,
        msg: Dict[str, Any],
        rec: Dict[str, Any],
        tool_results: Dict[str, Dict[str, Any]],
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_events: List[Dict[str, Any]] = []
        for part in self._parts(msg.get("parts")):
            if not isinstance(part, dict):
                continue
            if part.get("thought") is True:
                text = part.get("text")
                if isinstance(text, str):
                    reasoning_parts.append(text)
                continue
            if "text" in part and "functionCall" not in part:
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                name = fc.get("name")
                if not isinstance(name, str) or not name:
                    continue
                args = fc.get("args")
                suffix = ""
                if isinstance(args, dict):
                    suffix = render_tool_arguments(args, is_secret_field_name)
                text_parts.append(f"[Tool: {name}]{suffix}")
                call_id = fc.get("id")
                status = "completed"
                is_error = False
                result_rec = tool_results.get(call_id) if isinstance(call_id, str) else None
                if result_rec is not None:
                    tcr = result_rec.get("toolCallResult")
                    if isinstance(tcr, dict):
                        st = tcr.get("status")
                        status, is_error = tool_result_status(st, st == "error")
                tool_events.append(
                    {
                        "name": name,
                        "status": status,
                        "duration_ms": None,
                        "is_error": is_error,
                    }
                )
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        if reasoning and len(reasoning) > _MAX_REASONING_CHARS:
            reasoning = reasoning[:_MAX_REASONING_CHARS]
        return "\n".join(text_parts), reasoning, tool_events

    def _tool_result_text(self, rec: Dict[str, Any]) -> str:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return ""
        for part in self._parts(msg.get("parts")):
            if not isinstance(part, dict):
                continue
            fr = part.get("functionResponse")
            if not isinstance(fr, dict):
                continue
            response = fr.get("response")
            if isinstance(response, dict):
                for key in ("output", "error"):
                    val = response.get(key)
                    if isinstance(val, str) and val:
                        return val
                return "<json>"
            if isinstance(response, str) and response:
                return response
        return ""

    @staticmethod
    def _usage_tokens(raw: Any) -> Optional[Dict[str, int]]:
        if not isinstance(raw, dict):
            return None
        mapping = (
            ("promptTokenCount", "input"),
            ("candidatesTokenCount", "output"),
            ("totalTokenCount", "total"),
            ("cachedContentTokenCount", "cache_read"),
            ("thoughtsTokenCount", "reasoning"),
        )
        out: Dict[str, int] = {}
        for src, dst in mapping:
            val = raw.get(src)
            if isinstance(val, int) and not isinstance(val, bool):
                out[dst] = val
        return out or None

    def _compaction_summary(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        history = payload.get("compressedHistory")
        if not isinstance(history, list) or not history:
            return ""
        last = history[-1]
        if not isinstance(last, dict):
            return ""
        msg = last.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "model":
            return ""
        parts = self._parts(msg.get("parts"))
        for part in reversed(parts):
            if isinstance(part, dict) and part.get("text") and not part.get("thought"):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    text = text.strip()
                    if len(text) > _MAX_COMPACTION_CHARS:
                        text = text[:_MAX_COMPACTION_CHARS]
                    return text
        return ""
