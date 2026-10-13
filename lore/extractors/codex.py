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


class CodexExtractor(BaseExtractor):
    """Extract chat history from OpenAI Codex CLI."""

    def __init__(self):
        self.base_path = Path.home() / ".codex"
        self.session_roots = self._discover_session_roots()

    def _discover_session_roots(self):
        roots = [self.base_path / "sessions"]
        for candidate in discover_home_marker_paths(".codex/sessions"):
            if candidate not in roots:
                roots.append(candidate)
        return [root for root in roots if root.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.CODEX

    def is_available(self) -> bool:
        return len(self.session_roots) > 0

    def source_description(self) -> str:
        return "~/.codex/sessions/*/*/*/rollout-*.jsonl"

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        for sessions_dir in self.session_roots:
            for year_dir in sessions_dir.iterdir():
                if not year_dir.is_dir():
                    continue
                for month_dir in year_dir.iterdir():
                    if not month_dir.is_dir():
                        continue
                    for day_dir in month_dir.iterdir():
                        if not day_dir.is_dir():
                            continue
                        for session_file in day_dir.glob("rollout-*.jsonl"):
                            self._count("files_matched")
                            try:
                                session = self._parse_session_file(session_file)
                                self._count("files_parsed")

                                if self.should_import_session(session):
                                    yield session
                            except Exception as e:
                                logger.warning("Failed to parse %s: %s", session_file, e)

    def _parse_session_file(self, path: Path) -> UnifiedSession:
        """Parse a single Codex session file."""
        messages = []
        metadata = {}
        created_at = None
        last_updated = None

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type")
                timestamp = parse_timestamp(record.get("timestamp", ""))

                if created_at is None or timestamp < created_at:
                    created_at = timestamp
                if last_updated is None or timestamp > last_updated:
                    last_updated = timestamp

                if record_type == "session_meta":
                    metadata = record.get("payload", {})
                elif record_type == "message":
                    payload = record.get("payload", {})
                    role_str = payload.get("role", "user")
                    role = Role.USER if role_str == "user" else Role.ASSISTANT

                    content = payload.get("content", "")
                    if isinstance(content, list):
                        text_parts = []
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                        content = "\n".join(text_parts)

                    messages.append(
                        UnifiedMessage(
                            role=role,
                            content=content,
                            timestamp=timestamp,
                            message_id=payload.get("id"),
                        )
                    )
                elif record_type == "response_item":
                    payload = record.get("payload", {})
                    payload_type = payload.get("type", "")

                    if payload_type == "message":
                        role_str = payload.get("role", "assistant")
                        role = Role.USER if role_str == "user" else Role.ASSISTANT

                        content_list = payload.get("content", [])
                        text_parts = []
                        for item in content_list:
                            if isinstance(item, dict):
                                item_type = item.get("type", "")
                                if item_type == "output_text":
                                    text_parts.append(item.get("text", ""))
                                elif item_type == "input_text":
                                    text_parts.append(item.get("text", ""))
                        content = "\n".join(text_parts)

                        if content:
                            messages.append(
                                UnifiedMessage(
                                    role=role,
                                    content=content,
                                    timestamp=timestamp,
                                    message_id=payload.get("id"),
                                )
                            )

                    elif payload_type == "reasoning":
                        # Codex reasoning/thinking block
                        summary = payload.get("summary", [])
                        summary_text = ""
                        for s in summary:
                            if isinstance(s, dict) and s.get("type") == "summary_text":
                                summary_text = s.get("text", "")
                                break

                        if summary_text:
                            messages.append(
                                UnifiedMessage(
                                    role=Role.ASSISTANT,
                                    content=f"💭 {summary_text}",
                                    timestamp=timestamp,
                                    reasoning=summary_text,
                                )
                            )

                    elif payload_type == "function_call":
                        # Codex tool call
                        func_name = payload.get("name", "unknown")
                        func_args_str = payload.get("arguments", "{{}}")
                        try:
                            func_args = json.loads(func_args_str)
                        except json.JSONDecodeError:
                            func_args = {"raw": func_args_str}

                        # Format the tool call nicely
                        if func_name == "shell_command":
                            cmd = func_args.get("command", "")
                            content = f"[Tool: Bash]\n```bash\n{cmd}\n```"
                        elif func_name in ("read_file", "str_replace_editor"):
                            file_path = func_args.get("path", func_args.get("file_path", ""))
                            content = f"[Tool: {func_name}] {file_path}"
                        elif func_name == "write_file":
                            file_path = func_args.get("path", "")
                            content = f"[Tool: Write] {file_path}"
                        else:
                            args_preview = ", ".join(
                                f"{k}={str(v)[:50]}" for k, v in list(func_args.items())[:3]
                            )
                            content = f"[Tool: {func_name}] {args_preview}"

                        messages.append(
                            UnifiedMessage(
                                role=Role.ASSISTANT,
                                content=content,
                                timestamp=timestamp,
                                message_id=payload.get("call_id"),
                            )
                        )
                    else:
                        self._count("records_unknown")
                else:
                    self._count("records_unknown")

        if created_at is None:
            created_at = datetime.fromtimestamp(path.stat().st_mtime)
        if last_updated is None:
            last_updated = datetime.fromtimestamp(path.stat().st_mtime)

        return UnifiedSession(
            tool=Tool.CODEX,
            session_id=metadata.get("id", path.stem),
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=metadata.get("cwd"),
            thread_id=make_thread_id(project_path=metadata.get("cwd")),
            cli_version=metadata.get("cli_version"),
            source_path=str(path),
        )
