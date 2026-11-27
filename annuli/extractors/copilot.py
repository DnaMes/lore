import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class CopilotCLIExtractor(BaseExtractor):
    """Extract chat history from GitHub Copilot CLI."""

    def __init__(self):
        self.base_path = Path.home() / ".copilot"
        self.session_state_dirs = self._discover_session_state_dirs()

    def _discover_session_state_dirs(self):
        dirs = [self.base_path / "session-state"]
        for candidate in discover_home_marker_paths(".copilot/session-state"):
            if candidate not in dirs:
                dirs.append(candidate)
        return [directory for directory in dirs if directory.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.COPILOT_CLI

    def is_available(self) -> bool:
        return len(self.session_state_dirs) > 0

    def source_description(self) -> str:
        return "~/.copilot/session-state/*.jsonl"

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        for session_state_dir in self.session_state_dirs:
            for session_file in session_state_dir.glob("*.jsonl"):
                self._count("files_matched")
                try:
                    session = self._parse_session(session_file)
                    self._count("files_parsed")

                    if self.should_import_session(session):
                        yield session
                except Exception as e:
                    # Surface as a skip reason instead of just logging (#69, Axis-1d).
                    logger.warning("Failed to parse Copilot CLI session %s: %s", session_file, e)
                    self._record_skip("parse_error")

    def _parse_session(self, path: Path) -> UnifiedSession:
        """Parse a Copilot CLI session file."""
        messages = []
        session_id = path.stem
        created_at = None
        last_updated = None
        project_path = None

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type", "")
                timestamp = parse_timestamp(record.get("timestamp", ""))

                if created_at is None or timestamp < created_at:
                    created_at = timestamp
                if last_updated is None or timestamp > last_updated:
                    last_updated = timestamp

                data = record.get("data", {})

                if record_type == "session.start":
                    session_id = data.get("sessionId", session_id)

                elif record_type == "session.info":
                    # Extract project path from folder_trust message
                    msg = data.get("message", "")
                    if "Folder" in msg and "has been added" in msg:
                        # Extract path from message like "Folder /path/to/project has been added..."
                        parts = msg.split(" ")
                        if len(parts) >= 2:
                            potential_path = parts[1]
                            if potential_path.startswith("/"):
                                project_path = potential_path

                elif record_type == "user.message":
                    content = data.get("content", "")
                    if content:
                        messages.append(
                            UnifiedMessage(
                                role=Role.USER,
                                content=content,
                                timestamp=timestamp,
                                message_id=record.get("id"),
                            )
                        )

                elif record_type == "assistant.message":
                    content = data.get("content", "")
                    tool_requests = data.get("toolRequests", [])

                    if content or tool_requests:
                        tool_calls = []
                        for tr in tool_requests:
                            tool_calls.append(
                                {
                                    "id": tr.get("toolCallId"),
                                    "name": tr.get("name"),
                                    "input": tr.get("arguments"),
                                }
                            )

                        messages.append(
                            UnifiedMessage(
                                role=Role.ASSISTANT,
                                content=content,
                                timestamp=timestamp,
                                message_id=data.get("messageId"),
                                tool_calls=tool_calls,
                            )
                        )

                else:
                    self._count("records_unknown")

        if created_at is None:
            created_at = datetime.fromtimestamp(path.stat().st_mtime)
        if last_updated is None:
            last_updated = datetime.fromtimestamp(path.stat().st_mtime)

        return UnifiedSession(
            tool=Tool.COPILOT_CLI,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            source_path=str(path),
        )
