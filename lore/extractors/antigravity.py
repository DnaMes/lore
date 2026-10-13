"""Antigravity AI chat history extractor.

Extracts sessions from Antigravity's storage:
- ~/.gemini/antigravity/brain/*/task.md (session goal/checklist)
- ~/.gemini/antigravity/brain/*/walkthrough.md (detailed solution)
- ~/.gemini/antigravity/brain/*/*.metadata.json (timestamps)
- ~/.gemini/antigravity/conversations/*.pb (protobuf - currently not decoded)
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class AntigravityExtractor(BaseExtractor):
    """Extract chat history from Antigravity AI."""

    def __init__(self):
        self.base_path = Path.home() / ".gemini" / "antigravity"
        self.base_paths = self._discover_base_paths()

    def _discover_base_paths(self):
        paths = [self.base_path]
        for candidate in discover_home_marker_paths(".gemini/antigravity"):
            if candidate not in paths:
                paths.append(candidate)
        return [path for path in paths if path.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.ANTIGRAVITY

    def is_available(self) -> bool:
        return any((path / "brain").exists() for path in self.base_paths)

    def source_description(self) -> str:
        return "~/.gemini/antigravity/brain/*/task.md (+ walkthrough.md and *.metadata.json)"

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        for base_path in self.base_paths:
            brain_path = base_path / "brain"
            if not brain_path.exists():
                continue
            for session_dir in brain_path.iterdir():
                if not session_dir.is_dir():
                    continue

                self._count("files_matched")
                try:
                    session = self._parse_session(session_dir)
                except Exception as e:
                    # Don't drop silently — surface as a skip reason (#69, Axis-1d).
                    logger.warning("Failed to parse Antigravity session %s: %s", session_dir, e)
                    self._record_skip("parse_error")
                    continue

                if session is None:
                    # A brain dir with no task.md is not a conversation we can import.
                    self._record_skip("no_task_md")
                    continue
                self._count("files_parsed")
                if self.should_import_session(session):
                    yield session

    def _parse_session(self, session_dir: Path) -> Optional[UnifiedSession]:
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
        messages = []

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
