"""Extractor for the Aider AI pair-programming tool.

Aider writes one ``.aider.chat.history.md`` file per project, in that
project's working directory. The file accumulates every chat session for
that project, with this Markdown structure::

    # aider chat started at 2024-01-15 14:30:45

    #### a user prompt line
    #### continued on the next line

    an assistant response, written with no prefix

    > a tool / error / info message (blockquote)

- ``# aider chat started at ...`` (H1) delimits a new session.
- ``#### `` (H4) prefixes user input; multi-line input repeats the prefix.
- Un-prefixed text is assistant output.
- ``> `` prefixes tool/info/error messages.

We treat each ``# aider chat started at`` block as one UnifiedSession and
group consecutive same-role lines into a single UnifiedMessage.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)

HISTORY_FILENAME = ".aider.chat.history.md"

# "# aider chat started at 2024-01-15 14:30:45"
_SESSION_HEADER = re.compile(r"^#\s+aider chat started at\s+(.+?)\s*$")
# "#### user prompt text"  (H4 — distinct from markdown headings in replies)
_USER_PREFIX = "#### "
# "> tool / info message"
_TOOL_PREFIX = "> "


class AiderExtractor(BaseExtractor):
    """Extract chat history from Aider's per-project Markdown logs."""

    def __init__(self) -> None:
        self.history_files = self._discover_history_files()

    def _discover_history_files(self) -> List[Path]:
        """Find every ``.aider.chat.history.md`` under the user's home."""
        files: List[Path] = []
        # discover_home_marker_paths returns the directory + marker joined.
        for candidate in discover_home_marker_paths(HISTORY_FILENAME):
            if candidate.is_file() and candidate not in files:
                files.append(candidate)
        return files

    @property
    def tool(self) -> Tool:
        return Tool.AIDER

    def is_available(self) -> bool:
        return len(self.history_files) > 0

    def source_description(self) -> str:
        return f"~/**/{HISTORY_FILENAME} (one file per project)"

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        for history_file in self.history_files:
            self._count("files_matched")
            try:
                yield from self._parse_history_file(history_file)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to parse %s: %s", history_file, exc)

    def _parse_history_file(self, path: Path) -> Iterator[UnifiedSession]:
        """Split one history file into its constituent sessions."""
        # The file lives in the project directory, so the parent is the project.
        project_path = str(path.parent)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Cannot read %s: %s", path, exc)
            return
        self._count("files_parsed")

        # Split on session headers, keeping the header line with its block.
        current_started: Optional[str] = None
        current_lines: List[str] = []
        block_index = 0

        def _flush() -> Optional[UnifiedSession]:
            nonlocal block_index
            if not current_lines:
                return None
            session = self._build_session(
                path=path,
                project_path=project_path,
                started_raw=current_started,
                lines=current_lines,
                block_index=block_index,
            )
            block_index += 1
            return session

        for line in text.splitlines():
            header_match = _SESSION_HEADER.match(line)
            if header_match:
                flushed = _flush()
                if flushed is not None and self.should_import_session(flushed):
                    yield flushed
                current_started = header_match.group(1)
                current_lines = []
            else:
                current_lines.append(line)

        flushed = _flush()
        if flushed is not None and self.should_import_session(flushed):
            yield flushed

    def _build_session(
        self,
        path: Path,
        project_path: str,
        started_raw: Optional[str],
        lines: List[str],
        block_index: int,
    ) -> UnifiedSession:
        """Turn one ``# aider chat started at`` block into a UnifiedSession."""
        if started_raw:
            try:
                started_at = parse_timestamp(started_raw)
            except Exception:
                started_at = datetime.fromtimestamp(path.stat().st_mtime)
        else:
            started_at = datetime.fromtimestamp(path.stat().st_mtime)

        messages = self._parse_messages(lines, started_at)
        last_updated = messages[-1].timestamp if messages else started_at

        # Aider has no native session IDs — derive a stable one from the
        # project path + this block's start time so re-imports are idempotent.
        seed = f"{project_path}|{started_raw or block_index}"
        # Not security-sensitive: just a stable, collision-tolerant id seed.
        session_id = (
            "aider-" + hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )

        return UnifiedSession(
            tool=Tool.AIDER,
            session_id=session_id,
            created_at=started_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            source_path=str(path),
        )

    def _parse_messages(self, lines: List[str], session_time: datetime) -> List[UnifiedMessage]:
        """Group consecutive same-role lines into UnifiedMessages.

        Aider's Markdown carries no per-message timestamps, so every message
        in a session shares the session start time.
        """
        messages: List[UnifiedMessage] = []
        current_role: Optional[Role] = None
        buffer: List[str] = []

        def _flush() -> None:
            if current_role is None:
                return
            content = "\n".join(buffer).strip()
            if content:
                messages.append(
                    UnifiedMessage(
                        role=current_role,
                        content=content,
                        timestamp=session_time,
                    )
                )

        for raw_line in lines:
            if raw_line.startswith(_USER_PREFIX):
                role: Role = Role.USER
                content_line = raw_line[len(_USER_PREFIX) :]
            elif raw_line == "####":
                # Aider writes a bare "####" for a blank user line.
                role = Role.USER
                content_line = ""
            elif raw_line.startswith(_TOOL_PREFIX):
                role = Role.TOOL
                content_line = raw_line[len(_TOOL_PREFIX) :]
            elif raw_line.strip() == "":
                # Blank line — keep it inside the current message for spacing.
                if current_role is not None:
                    buffer.append(raw_line)
                continue
            else:
                role = Role.ASSISTANT
                content_line = raw_line

            if role != current_role:
                _flush()
                current_role = role
                buffer = []
            buffer.append(content_line)

        _flush()
        return messages
