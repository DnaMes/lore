import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class VSCodeCopilotExtractor(BaseExtractor):
    """Extract chat history from VSCode Copilot (workspace chatSessions)."""

    # A single VSCode chat session file is parsed whole (json.load / readlines).
    # A degenerate multi-hundred-MB conversation spikes RSS ~10x its file size
    # during parse (a 139 MB file drove VmHWM to ~1.2 GB), which dominated the
    # reload peak (#96). Skip files above this cap — logged, never silent — so
    # peak memory stays bounded regardless of one runaway conversation.
    MAX_SESSION_FILE_BYTES = 25 * 1024 * 1024  # 25 MB

    def __init__(self):
        self.workspace_storage = Path.home() / ".config" / "Code" / "User" / "workspaceStorage"
        self.workspace_roots = self._discover_workspace_roots()
        self.MIN_MESSAGES = 1  # VSCode sessions often have only 1-2 exchanges
        self.MIN_CONTENT_SIZE = 50

    def _discover_workspace_roots(self):
        roots = [self.workspace_storage]
        for candidate in discover_home_marker_paths(".config/Code/User/workspaceStorage"):
            if candidate not in roots:
                roots.append(candidate)
        return [root for root in roots if root.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.VSCODE_COPILOT

    def is_available(self) -> bool:
        return len(self.workspace_roots) > 0

    def source_description(self) -> str:
        return (
            "~/.config/Code/User/workspaceStorage/*/chatSessions/*.json, "
            "~/.config/Code/User/workspaceStorage/*/chatSessions/*.jsonl"
        )

    def _get_workspace_project(self, workspace_dir: Path) -> Optional[str]:
        """Try to get project path from workspace.json."""
        workspace_json = workspace_dir / "workspace.json"
        if workspace_json.exists():
            try:
                with open(workspace_json, "r") as f:
                    data = json.load(f)
                    folder = data.get("folder")
                    if folder and folder.startswith("file://"):
                        return folder[7:]  # Remove file:// prefix
            except Exception:
                pass
        return None

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        for workspace_root in self.workspace_roots:
            for workspace_dir in workspace_root.iterdir():
                if not workspace_dir.is_dir():
                    continue

                chat_sessions_dir = workspace_dir / "chatSessions"
                if not chat_sessions_dir.exists():
                    continue

                project_path = self._get_workspace_project(workspace_dir)

                for session_file in list(chat_sessions_dir.glob("*.json")) + list(
                    chat_sessions_dir.glob("*.jsonl")
                ):
                    self._count("files_matched")
                    try:
                        size = session_file.stat().st_size
                    except OSError:
                        size = 0
                    if size > self.MAX_SESSION_FILE_BYTES:
                        # Parsing this whole file would spike RSS ~10x its size
                        # (#96). Drop it, loudly — not a silent filter.
                        logger.warning(
                            "Skipping oversized VSCode session %s (%.0f MB > %.0f MB cap)",
                            session_file.name,
                            size / 1024 / 1024,
                            self.MAX_SESSION_FILE_BYTES / 1024 / 1024,
                        )
                        self._record_skip("oversized_session_file")
                        continue
                    try:
                        session = self._parse_session(session_file, project_path)
                        self._count("files_parsed")

                        if self.should_import_session(session):
                            yield session
                    except Exception as e:
                        logger.warning("Failed to parse VSCode session %s: %s", session_file, e)

    def _parse_session(self, path: Path, project_path: Optional[str]) -> UnifiedSession:
        """Parse a VSCode Copilot chat session file (.json or .jsonl).

        .json  - legacy format: single JSON object with 'requests' array
        .jsonl - new format: one JSON object per line, kind=0 is header,
                 kind=2 lines are request/response parts
        """
        if path.suffix == ".jsonl":
            return self._parse_jsonl_session(path, project_path)
        return self._parse_json_session(path, project_path)

    def _parse_jsonl_session(self, path: Path, project_path: Optional[str]) -> UnifiedSession:
        """Parse new .jsonl format (VSCode >= 1.96).

        Format per line:
          kind=0: session header (creationDate, sessionId, ...)
          kind=2, v=[{requestId, message, ...}]: new user request
          kind=2, v=[{value, supportThemeIcons, ...}]: assistant text chunk
          kind=1: incremental state updates (ignored)
        """
        fallback_ts = datetime.fromtimestamp(path.stat().st_mtime)
        messages = []
        created_at = None
        last_updated = None
        session_id = path.stem
        title = None

        # Accumulate assistant text chunks between requests
        pending_assistant_parts: list = []
        current_req_ts = fallback_ts

        def flush_assistant(ts):
            nonlocal pending_assistant_parts
            text = "\n".join(p for p in pending_assistant_parts if p).strip()
            if text:
                messages.append(
                    UnifiedMessage(
                        role=Role.ASSISTANT,
                        content=text,
                        timestamp=ts,
                    )
                )
            pending_assistant_parts = []

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw_lines = f.readlines()
        except OSError:
            raw_lines = []

        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue

            kind = entry.get("kind")
            v = entry.get("v")
            if kind not in (0, 1, 2):
                self._count("records_unknown")

            if kind == 0 and isinstance(v, dict):
                # Session header
                ts_ms = v.get("creationDate")
                if ts_ms:
                    created_at = datetime.fromtimestamp(ts_ms / 1000)
                session_id = v.get("sessionId", session_id)
                title = v.get("customTitle") or v.get("title")
                continue

            if kind == 2 and isinstance(v, list):
                for part in v:
                    if not isinstance(part, dict):
                        continue

                    part_kind = part.get("kind")

                    # New user request: has 'requestId' and 'message'
                    if "requestId" in part and "message" in part:
                        # Flush previous assistant response first
                        flush_assistant(current_req_ts)

                        msg = part.get("message", {})
                        user_text = msg.get("text", "").strip()
                        ts_ms = part.get("timestamp")
                        current_req_ts = (
                            datetime.fromtimestamp(ts_ms / 1000) if ts_ms else fallback_ts
                        )

                        if user_text:
                            messages.append(
                                UnifiedMessage(
                                    role=Role.USER,
                                    content=user_text,
                                    timestamp=current_req_ts,
                                    message_id=part.get("requestId"),
                                )
                            )
                            if created_at is None:
                                created_at = current_req_ts
                            last_updated = current_req_ts
                        continue

                    # Assistant text chunk: has 'value' str, no 'kind' or kind=None
                    if part_kind is None and "value" in part:
                        text = part.get("value", "")
                        if isinstance(text, str) and text.strip():
                            pending_assistant_parts.append(text.strip())
                        continue

                    # thinking parts - skip (internal reasoning)
                    if part_kind == "thinking":
                        continue

                    self._count("records_unknown")

        # Flush any remaining assistant content
        flush_assistant(current_req_ts)

        if created_at is None:
            created_at = fallback_ts
        if last_updated is None:
            last_updated = fallback_ts

        return UnifiedSession(
            tool=Tool.VSCODE_COPILOT,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            title=title,
            source_path=str(path),
        )

    def _parse_json_session(self, path: Path, project_path: Optional[str]) -> UnifiedSession:
        """Parse legacy .json format with 'requests' array."""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)

        messages = []
        created_at = None
        last_updated = None
        fallback_timestamp = datetime.fromtimestamp(path.stat().st_mtime)

        def iter_response_content(payload):
            if payload is None:
                return
            if isinstance(payload, str):
                text = payload.strip()
                if text:
                    yield text
                return
            if isinstance(payload, list):
                for item in payload:
                    yield from iter_response_content(item)
                return
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, dict):
                    value = content.get("value")
                    if isinstance(value, str) and value.strip():
                        yield value
                elif isinstance(content, str) and content.strip():
                    yield content
                value = payload.get("value")
                if isinstance(value, str) and value.strip():
                    yield value
                markdown = payload.get("markdown")
                if isinstance(markdown, str) and markdown.strip():
                    yield markdown
                for key in ("parts", "response", "message", "items"):
                    if key in payload:
                        yield from iter_response_content(payload.get(key))
                return

        for request in data.get("requests", []):
            request_timestamp = fallback_timestamp
            msg_data = request.get("message", {})
            user_text = msg_data.get("text", "")

            if user_text:
                request_timestamp = fallback_timestamp
                messages.append(
                    UnifiedMessage(
                        role=Role.USER,
                        content=user_text,
                        timestamp=request_timestamp,
                        message_id=request.get("requestId"),
                    )
                )
                if created_at is None:
                    created_at = request_timestamp
                last_updated = request_timestamp

            response = request.get("response")
            for content in iter_response_content(response):
                messages.append(
                    UnifiedMessage(
                        role=Role.ASSISTANT,
                        content=content,
                        timestamp=request_timestamp,
                    )
                )
            if response is None:
                response = request.get("responseParts")
                for content in iter_response_content(response):
                    messages.append(
                        UnifiedMessage(
                            role=Role.ASSISTANT,
                            content=content,
                            timestamp=request_timestamp,
                        )
                    )

        if created_at is None:
            created_at = fallback_timestamp
        if last_updated is None:
            last_updated = fallback_timestamp

        return UnifiedSession(
            tool=Tool.VSCODE_COPILOT,
            session_id=path.stem,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            title=data.get("customTitle"),
            source_path=str(path),
        )
