import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id, safe_copy_db
from ..utils.text_processing import decode_hex_or_str
from .base import BaseExtractor

logger = logging.getLogger(__name__)


class CursorExtractor(BaseExtractor):
    """Extract chat history from Cursor IDE."""

    def __init__(self):
        self.db_path = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        self.db_paths = self._discover_db_paths()

    def _discover_db_paths(self):
        paths = [self.db_path]
        for candidate in discover_home_marker_paths(
            ".config/Cursor/User/globalStorage/state.vscdb"
        ):
            if candidate not in paths:
                paths.append(candidate)
        return [path for path in paths if path.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.CURSOR

    def is_available(self) -> bool:
        return len(self.db_paths) > 0

    def _decode_blob(self, value: Any) -> str:
        """Try to decode a blob that might be hex or just string.

        The heuristic lives in :func:`lore.utils.text_processing.decode_hex_or_str`
        so other SQLite-backed extractors can reuse it (#120).
        """
        return decode_hex_or_str(value)

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        original_db_path = self.db_path
        for db_path in self.db_paths:
            self.db_path = db_path
            work_db_path = safe_copy_db(db_path)

            try:
                conn = sqlite3.connect(f"file:{work_db_path}?mode=ro", uri=True)
                conn.text_factory = lambda b: b.decode(errors="ignore")
            except sqlite3.Error:
                continue

            try:
                try:
                    cursor = conn.execute(
                        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                    )
                    for key, value in cursor:
                        try:
                            decoded = self._decode_blob(value)
                            data = json.loads(decoded)
                            session = self._parse_composer(key.split(":")[-1], data, conn)
                            if (
                                session
                                and (session.messages or session.title)
                                and self.should_import_session(session)
                            ):
                                yield session
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.debug(f"Failed to parse composer {key}: {e}")
                            continue
                except sqlite3.Error as e:
                    logger.warning(f"Failed to query composerData: {e}")

                try:
                    cursor = conn.execute(
                        "SELECT value FROM cursorDiskKV WHERE key = 'workbench.panel.aichat.view.aichat.chatdata'"
                    )
                    for row in cursor:
                        try:
                            decoded = self._decode_blob(row[0])
                            data = json.loads(decoded)
                            if isinstance(data, dict) and "tabs" in data:
                                for tab in data.get("tabs", []):
                                    session = self._parse_sidebar_tab(tab)
                                    if session and self.should_import_session(session):
                                        yield session
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.debug(f"Failed to parse sidebar chat: {e}")
                except sqlite3.Error as e:
                    logger.warning(f"Failed to query sidebar chat: {e}")

            finally:
                conn.close()
                try:
                    if work_db_path != db_path:
                        work_db_path.unlink(missing_ok=True)
                        Path(str(work_db_path) + "-wal").unlink(missing_ok=True)
                        Path(str(work_db_path) + "-shm").unlink(missing_ok=True)
                except (OSError, PermissionError) as e:
                    logger.debug(f"Failed to cleanup temp db files: {e}")
        self.db_path = original_db_path

    def _parse_composer(
        self, composer_id: str, data: dict, conn: sqlite3.Connection
    ) -> UnifiedSession:
        created_at = parse_timestamp(data.get("createdAt", 0))
        last_updated = parse_timestamp(data.get("lastUpdatedAt", 0))
        messages = []
        project_path = self._infer_project_path(data)

        # 1. Try 'conversation' array (Old format)
        raw_msgs = data.get("conversation", [])

        for msg in raw_msgs:
            role = Role.USER if msg.get("type") == 1 else Role.ASSISTANT
            text = msg.get("text", "")
            if text:
                messages.append(
                    UnifiedMessage(
                        role=role,
                        content=text,
                        timestamp=parse_timestamp(msg.get("timestamp", 0)) or created_at,
                    )
                )

        # 2. Try 'fullConversationHeadersOnly' (New format - Linked Bubbles)
        headers = data.get("fullConversationHeadersOnly", [])
        for header in headers:
            bubble_id = header.get("bubbleId")
            if not bubble_id:
                continue

            # Type: 1=User, 2=AI
            role = Role.USER if header.get("type") == 1 else Role.ASSISTANT

            # Fetch bubble content
            key = f"bubbleId:{composer_id}:{bubble_id}"
            try:
                row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()
                if row:
                    val = self._decode_blob(row[0])
                    bdata = json.loads(val)
                    text = bdata.get("text", "")
                    if text:
                        messages.append(
                            UnifiedMessage(role=role, content=text, timestamp=created_at)
                        )
            except (json.JSONDecodeError, sqlite3.Error) as e:
                logger.debug(f"Failed to fetch bubble {bubble_id}: {e}")

        if not messages and data.get("text", "").strip():
            messages.append(
                UnifiedMessage(
                    role=Role.USER,
                    content=str(data.get("text", "")),
                    timestamp=created_at,
                )
            )

        title = data.get("name") or (data.get("text", "")[:50] if messages else "Composer Session")
        return UnifiedSession(
            tool=Tool.CURSOR,
            session_id=composer_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            title=title,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            source_path=str(self.db_path),
        )

    def _parse_sidebar_tab(self, tab: dict) -> Optional[UnifiedSession]:
        tab_id = tab.get("tabId", "unknown")
        title = tab.get("chatTitle", "")
        created_at = parse_timestamp(tab.get("timestamp", 0))
        if created_at.year < 2000:
            created_at = datetime.now()

        messages = []
        for bubble in tab.get("bubbles", []):
            role = Role.USER if bubble.get("type") == "user" else Role.ASSISTANT
            text = bubble.get("text", "") or bubble.get("richText", "")
            if text:
                messages.append(UnifiedMessage(role=role, content=str(text), timestamp=created_at))

        if not messages:
            return None
        project_path = self._infer_project_path(tab)
        return UnifiedSession(
            tool=Tool.CURSOR,
            session_id=f"sidebar-{tab_id}",
            created_at=created_at,
            last_updated=created_at,
            messages=messages,
            title=title,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            source_path=str(self.db_path),
        )

    def _infer_project_path(self, data: dict) -> Optional[str]:
        """Best-effort project path detection from Cursor payloads."""
        if not isinstance(data, dict):
            return None

        candidates = []
        preferred_keys = [
            "cwd",
            "projectPath",
            "workspacePath",
            "rootPath",
            "folder",
            "workspace",
        ]

        def add_candidate(value):
            if isinstance(value, str) and value.startswith("/") and len(value) > 3:
                candidates.append(value)

        for key in preferred_keys:
            val = data.get(key)
            if isinstance(val, str):
                add_candidate(val)

        def walk(obj, depth=0):
            if depth > 3:
                return
            if isinstance(obj, dict):
                for v in obj.values():
                    walk(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj[:50]:
                    walk(v, depth + 1)
            elif isinstance(obj, str):
                add_candidate(obj)

        walk(data)

        for path in candidates:
            if Path(path).exists():
                return path

        return candidates[0] if candidates else None
