"""Cursor agent CLI / background-agent transcripts under ~/.cursor/projects."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, TextIO, Tuple

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive
from ..utils.paths import make_thread_id

if TYPE_CHECKING:
    from .cursor import CursorExtractor

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_LEGACY_TEXT_BYTES = 2 * 1024 * 1024
_MAX_TOOL_ARG_CHARS = 200
_MAX_MESSAGES = 20_000

_USER_QUERY_RE = re.compile(
    r"^\s*<user_query>(.*)</user_query>\s*$",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class _TranscriptRef:
    slug: str
    session_id: str
    path: Path


def agent_transcripts_available() -> bool:
    root = _projects_root()
    if root is None:
        return False
    try:
        for slug_dir in root.iterdir():
            if not _is_real_dir(slug_dir):
                continue
            at_dir = slug_dir / "agent-transcripts"
            if not _is_real_dir(at_dir):
                continue
            try:
                if any(at_dir.iterdir()):
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def extract_agent_transcript_sessions(extractor: CursorExtractor) -> Iterator[UnifiedSession]:
    refs = _discover_transcript_refs()
    if not refs:
        return

    id_slugs: Dict[str, set[str]] = {}
    for ref in refs:
        id_slugs.setdefault(ref.session_id, set()).add(ref.slug)
    qualify_all = {sid for sid, slugs in id_slugs.items() if len(slugs) > 1}

    for ref in refs:
        session_id = (
            f"{ref.slug}:{ref.session_id}" if ref.session_id in qualify_all else ref.session_id
        )
        if not _is_plain_file(ref.path):
            continue
        if extractor._source_unchanged(ref.path):
            continue
        extractor._count("files_matched")
        try:
            session, unknown = _parse_transcript_file(ref.path, session_id)
            if unknown:
                extractor._count("records_unknown", unknown)
            if session is None:
                continue
            extractor._count("files_parsed")
            if session.messages and extractor.should_import_session(session):
                yield session
        except (
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as exc:
            logger.debug("Failed agent transcript %s: %s", ref.path, type(exc).__name__)


def _projects_root() -> Optional[Path]:
    root = Path.home() / ".cursor" / "projects"
    return root if _is_real_dir(root) else None


def _is_real_dir(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


def _is_plain_file(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _discover_transcript_refs() -> List[_TranscriptRef]:
    root = _projects_root()
    if root is None:
        return []
    refs: List[_TranscriptRef] = []
    try:
        slug_dirs = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for slug_dir in slug_dirs:
        if not _is_real_dir(slug_dir):
            continue
        at_dir = slug_dir / "agent-transcripts"
        if not _is_real_dir(at_dir):
            continue
        try:
            children = sorted(at_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        seen_ids: set[str] = set()
        for child in children:
            if _is_real_dir(child):
                nested = child / f"{child.name}.jsonl"
                if _is_plain_file(nested):
                    refs.append(
                        _TranscriptRef(slug=slug_dir.name, session_id=child.name, path=nested)
                    )
                    seen_ids.add(child.name)
            elif child.suffix == ".jsonl" and _is_plain_file(child):
                if child.stem in seen_ids or _is_real_dir(at_dir / child.stem):
                    continue  # nested form wins; two files must not share one session id
                refs.append(
                    _TranscriptRef(
                        slug=slug_dir.name,
                        session_id=child.stem,
                        path=child,
                    )
                )
    return refs


def _open_plain_file(path: Path) -> Optional[Tuple[TextIO, datetime]]:
    """Open without following symlinks; the checked descriptor is the one that is read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            os.close(fd)
            return None
        mtime = make_naive(datetime.fromtimestamp(info.st_mtime))
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace"), mtime
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _parse_transcript_file(path: Path, session_id: str) -> Tuple[Optional[UnifiedSession], int]:
    opened = _open_plain_file(path)
    if opened is None:
        return None, 0
    stream, file_mtime = opened
    try:
        with stream as handle:
            first = _read_first_nonempty_line(handle)
            if first is None:
                return None, 0
            handle.seek(0)
            if _looks_like_jsonl(first):
                messages, unknown = _parse_jsonl(handle, file_mtime)
            else:
                raw = handle.read(_MAX_LEGACY_TEXT_BYTES + 1)
                if len(raw) > _MAX_LEGACY_TEXT_BYTES:
                    raw = raw[:_MAX_LEGACY_TEXT_BYTES]
                messages = _parse_plain_text(raw, file_mtime)
                unknown = 0
    except OSError:
        return None, 0

    if not messages:
        return None, unknown

    session = UnifiedSession(
        tool=Tool.CURSOR,
        session_id=session_id,
        created_at=file_mtime,
        last_updated=file_mtime,
        messages=messages,
        project_path=None,
        thread_id=make_thread_id(project_path=None),
        source_path=str(path),
        origin="unknown",
        source_version=None,
    )
    return session, unknown


def _read_first_nonempty_line(handle) -> Optional[str]:
    while True:
        line = handle.readline(_MAX_LINE_CHARS)
        if not line:
            return None
        stripped = line.strip()
        if stripped:
            return stripped


def _looks_like_jsonl(first_line: str) -> bool:
    if not first_line.startswith("{"):
        return False
    if len(first_line) >= _MAX_LINE_CHARS:
        return True  # oversized first record: _parse_jsonl counts and skips it
    try:
        rec = json.loads(first_line)
    except (ValueError, RecursionError, OverflowError):
        return False
    return isinstance(rec, dict)


def _parse_jsonl(handle, file_mtime: datetime) -> Tuple[List[UnifiedMessage], int]:
    messages: List[UnifiedMessage] = []
    unknown = 0
    while True:
        line = handle.readline(_MAX_LINE_CHARS)
        if not line:
            break
        if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
            unknown += 1
            while line and not line.endswith("\n"):
                line = handle.readline(_MAX_LINE_CHARS)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError, OverflowError):
            unknown += 1
            continue
        if not isinstance(rec, dict):
            unknown += 1
            continue
        if rec.get("type") == "status":
            continue
        role_raw = rec.get("role")
        if role_raw not in ("user", "assistant"):
            unknown += 1
            continue
        message_obj = rec.get("message")
        if not isinstance(message_obj, dict):
            unknown += 1
            continue
        content, tool_events = _format_message_content(message_obj.get("content"))
        if not content and not tool_events:
            continue
        if len(messages) >= _MAX_MESSAGES:
            unknown += 1
            break
        role = Role.USER if role_raw == "user" else Role.ASSISTANT
        if role is Role.USER:
            content = _strip_user_query_wrapper(content)
        messages.append(
            UnifiedMessage(
                role=role,
                content=content,
                timestamp=file_mtime,
                tool_events=tool_events,
            )
        )
    return messages, unknown


def _parse_plain_text(raw: str, file_mtime: datetime) -> List[UnifiedMessage]:
    messages: List[UnifiedMessage] = []
    current_role: Optional[Role] = None
    parts: List[str] = []

    def flush() -> None:
        nonlocal current_role, parts
        if current_role is None:
            return
        text = "\n".join(parts).strip()
        if current_role is Role.USER:
            text = _strip_user_query_wrapper(text)
        if text:
            messages.append(UnifiedMessage(role=current_role, content=text, timestamp=file_mtime))
        parts = []

    for line in raw.splitlines():
        if line.startswith("user:"):
            flush()
            current_role = Role.USER
            parts = [line[5:].lstrip()]
        elif line.startswith("assistant:"):
            flush()
            current_role = Role.ASSISTANT
            parts = [line[10:].lstrip()]
        elif current_role is not None:
            parts.append(line)
    flush()
    return messages


def _strip_user_query_wrapper(text: str) -> str:
    if not text:
        return text
    match = _USER_QUERY_RE.match(text.strip())
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.lower().startswith("<user_query>") and "</user_query>" in stripped:
        start = stripped.lower().find("<user_query>") + len("<user_query>")
        end = stripped.lower().find("</user_query>")
        if end > start:
            return stripped[start:end].strip()
    return text


def _format_message_content(content: Any) -> Tuple[str, List[Dict[str, Any]]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    text_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        b_type = block.get("type")
        if b_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif b_type == "tool_use":
            tool_name = block.get("name", "tool")
            if not isinstance(tool_name, str):
                tool_name = "tool"
            tool_input = block.get("input")
            suffix = _render_tool_input(tool_input)
            text_parts.append(f"[Tool: {tool_name}]{suffix}")
            # No tool results in this format; "completed" is conventional.
            tool_events.append(
                {
                    "name": tool_name,
                    "status": "completed",
                    "duration_ms": None,
                    "is_error": False,
                }
            )
    return "\n".join(text_parts), tool_events


def _render_tool_input(tool_input: Any) -> str:
    rendered: List[str] = []
    if isinstance(tool_input, dict):
        for key, value in tool_input.items():
            v_str = str(value)
            if len(v_str) > _MAX_TOOL_ARG_CHARS:
                v_str = v_str[:_MAX_TOOL_ARG_CHARS]
            rendered.append(f"{key}={v_str}")
    elif tool_input is not None:
        v_str = str(tool_input)
        if len(v_str) > _MAX_TOOL_ARG_CHARS:
            v_str = v_str[:_MAX_TOOL_ARG_CHARS]
        rendered.append(v_str)
    return (" " + " ".join(rendered)) if rendered else ""
