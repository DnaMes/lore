import json
import logging
import math
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from ..utils.text_processing import decode_hex_or_str
from .base import BaseExtractor
from .cursor_agent import agent_transcripts_available, extract_agent_transcript_sessions

logger = logging.getLogger(__name__)

_MAX_SQLITE_BYTES = 512 * 1024 * 1024
_SQLITE_TIMEOUT = 2.0
_SQLITE_QUERY_SECONDS = 10.0
_MAX_BUBBLES_PER_COMPOSER = 5000
_MAX_CONTENT_CHARS = 100_000
_MAX_REASONING_CHARS = 20_000
_MAX_TOOL_ARG_CHARS = 200

_PARSE_ERRORS = (
    json.JSONDecodeError,
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    OverflowError,
    RecursionError,
)
_MAX_TITLE_CHARS = 200
_SECRET_NAME_PARTS = ("key", "secret", "token", "encryption", "blob", "password")


def _is_secret_composer_field(name: str) -> bool:
    lower = name.lower()
    return any(part in lower for part in _SECRET_NAME_PARTS)


def _filter_composer_data(data: Any) -> dict:
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not _is_secret_composer_field(k)}


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, (int, float)) and not math.isfinite(value):
        return fallback
    try:
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError, RecursionError):
        return fallback


class CursorExtractor(BaseExtractor):
    """Extract chat history from Cursor IDE."""

    PARSER_VERSION = 3

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
        return len(self.db_paths) > 0 or agent_transcripts_available()

    def source_description(self) -> str:
        return (
            "~/.config/Cursor/User/globalStorage/state.vscdb "
            "(cursorDiskKV composerData:* rows and the sidebar chat data); "
            "~/.cursor/projects/*/agent-transcripts/**/*.jsonl"
        )

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
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    work_db_path = copy_sqlite_with_sidecars(db_path, Path(tmp), _MAX_SQLITE_BYTES)
                    if work_db_path is None:
                        continue

                    try:
                        conn = sqlite3.connect(
                            f"file:{work_db_path}?mode=ro",
                            uri=True,
                            timeout=_SQLITE_TIMEOUT,
                        )
                        conn.text_factory = lambda b: b.decode(errors="ignore")
                    except sqlite3.Error:
                        continue

                    try:
                        deadline = time.monotonic() + _SQLITE_QUERY_SECONDS
                        conn.set_progress_handler(
                            lambda: 1 if time.monotonic() > deadline else 0, 10_000
                        )
                        header_rows = self._load_composer_headers(conn)
                        yield from self._extract_ide_sessions(conn, header_rows)
                    finally:
                        conn.close()
            except OSError:
                continue
        self.db_path = original_db_path
        yield from extract_agent_transcript_sessions(self)

    def _load_composer_headers(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        try:
            info = conn.execute("PRAGMA table_info(composerHeaders)").fetchall()
        except sqlite3.Error:
            return {}
        col_names = {row[1] for row in info}
        if "composerId" not in col_names:
            return {}
        wanted = ["composerId", "createdAt", "lastUpdatedAt", "isArchived", "isSubagent"]
        select_cols = [c for c in wanted if c in col_names]
        if len(select_cols) < 2:
            return {}
        try:
            rows = conn.execute(f"SELECT {', '.join(select_cols)} FROM composerHeaders").fetchall()
        except sqlite3.Error:
            return {}
        col_index = {name: select_cols.index(name) for name in select_cols}
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            cid = row[col_index["composerId"]]
            if not isinstance(cid, str) or not cid:
                continue
            entry: Dict[str, Any] = {}
            if "createdAt" in col_index:
                entry["createdAt"] = row[col_index["createdAt"]]
            if "lastUpdatedAt" in col_index:
                entry["lastUpdatedAt"] = row[col_index["lastUpdatedAt"]]
            if "isArchived" in col_index:
                entry["isArchived"] = row[col_index["isArchived"]]
            if "isSubagent" in col_index:
                entry["isSubagent"] = row[col_index["isSubagent"]]
            result[cid] = entry
        return result

    def _extract_ide_sessions(
        self, conn: sqlite3.Connection, header_rows: Dict[str, Dict[str, Any]]
    ) -> Iterator[UnifiedSession]:
        sessions_in_order: List[UnifiedSession] = []
        subagent_children: Dict[str, List[str]] = {}

        try:
            cursor = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            )
            for key, value in cursor:
                self._count("files_matched")
                try:
                    decoded = self._decode_blob(value)
                    raw = json.loads(decoded)
                    data = _filter_composer_data(raw)
                    composer_id = key.split(":")[-1]
                    session, child_ids = self._parse_composer(
                        composer_id, data, conn, header_rows.get(composer_id)
                    )
                    if session is not None:
                        self._count("files_parsed")
                    if child_ids:
                        subagent_children[composer_id] = child_ids
                    if (
                        session
                        and (session.messages or session.title)
                        and self.should_import_session(session)
                    ):
                        sessions_in_order.append(session)
                except _PARSE_ERRORS as e:
                    logger.debug(f"Failed to parse composer {key}: {e}")
                    self._count("records_unknown")
                    continue
        except sqlite3.Error as e:
            logger.warning(f"Failed to query composerData: {e}")

        session_by_id = {s.session_id: s for s in sessions_in_order}
        for parent_id, child_ids in subagent_children.items():
            if parent_id not in session_by_id:
                continue
            for child_id in child_ids:
                if not isinstance(child_id, str):
                    continue
                child = session_by_id.get(child_id)
                if child is None:
                    continue
                child.parent_session_id = parent_id
                child.relation = "subagent"
                child.origin = "subagent"

        for session in sessions_in_order:
            yield session

        try:
            cursor = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = 'workbench.panel.aichat.view.aichat.chatdata'"
            )
            for row in cursor:
                try:
                    decoded = self._decode_blob(row[0])
                    data = json.loads(decoded)
                    if isinstance(data, dict) and "tabs" in data:
                        tabs = data.get("tabs")
                        for tab in tabs if isinstance(tabs, list) else []:
                            self._count("files_matched")
                            if not isinstance(tab, dict):
                                self._count("records_unknown")
                                continue
                            session = self._parse_sidebar_tab(tab)
                            if session is not None:
                                self._count("files_parsed")
                            if session and self.should_import_session(session):
                                yield session
                except _PARSE_ERRORS as e:
                    logger.debug(f"Failed to parse sidebar chat: {e}")
                    self._count("records_unknown")
        except sqlite3.Error as e:
            logger.warning(f"Failed to query sidebar chat: {e}")

    def _parse_composer(
        self,
        composer_id: str,
        data: dict,
        conn: sqlite3.Connection,
        header_row: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[UnifiedSession], List[str]]:
        created_at = _safe_ts(data.get("createdAt", 0), datetime.fromtimestamp(0))
        last_updated = _safe_ts(data.get("lastUpdatedAt", 0), created_at)
        origin = "unknown"
        if header_row:
            if header_row.get("createdAt") is not None:
                created_at = _safe_ts(header_row["createdAt"], created_at)
            if header_row.get("lastUpdatedAt") is not None:
                last_updated = _safe_ts(header_row["lastUpdatedAt"], last_updated)
            if header_row.get("isSubagent"):
                origin = "subagent"

        messages: List[UnifiedMessage] = []
        project_path = self._infer_project_path(data)

        raw_msgs = data.get("conversation", [])
        if isinstance(raw_msgs, list):
            for msg in raw_msgs:
                if not isinstance(msg, dict):
                    continue
                role = Role.USER if msg.get("type") == 1 else Role.ASSISTANT
                text = msg.get("text", "")
                if isinstance(text, str) and text:
                    messages.append(
                        UnifiedMessage(
                            role=role,
                            content=text[:_MAX_CONTENT_CHARS],
                            timestamp=_safe_ts(msg.get("timestamp", 0), created_at),
                        )
                    )

        headers = data.get("fullConversationHeadersOnly", [])
        bubble_count = 0
        if isinstance(headers, list):
            for header in headers:
                if bubble_count >= _MAX_BUBBLES_PER_COMPOSER:
                    self._count("records_unknown")
                    break
                if not isinstance(header, dict):
                    continue
                bubble_id = header.get("bubbleId")
                if not bubble_id:
                    continue
                role = Role.USER if header.get("type") == 1 else Role.ASSISTANT
                key = f"bubbleId:{composer_id}:{bubble_id}"
                try:
                    row = conn.execute(
                        "SELECT value FROM cursorDiskKV WHERE key=?", (key,)
                    ).fetchone()
                    if not row:
                        continue
                    val = self._decode_blob(row[0])
                    bdata = json.loads(val)
                    bubble_msg = self._parse_bubble(bdata, role, created_at)
                    if bubble_msg is not None:
                        messages.append(bubble_msg)
                        bubble_count += 1
                    elif bdata is not None:
                        self._count("records_unknown")
                except (json.JSONDecodeError, sqlite3.Error) as e:
                    logger.debug(f"Failed to fetch bubble {bubble_id}: {e}")
                    self._count("records_unknown")

        if not messages and isinstance(data.get("text"), str) and data.get("text", "").strip():
            messages.append(
                UnifiedMessage(
                    role=Role.USER,
                    content=str(data.get("text", ""))[:_MAX_CONTENT_CHARS],
                    timestamp=created_at,
                )
            )

        raw_text = data.get("text")
        title = data.get("name") or (
            raw_text[:50] if messages and isinstance(raw_text, str) else "Composer Session"
        )
        if not isinstance(title, str):
            title = "Composer Session"
        title = title[:_MAX_TITLE_CHARS]

        child_ids: List[str] = []
        raw_children = data.get("subagentComposerIds")
        if isinstance(raw_children, list):
            child_ids = [c for c in raw_children if isinstance(c, str)]

        session = UnifiedSession(
            tool=Tool.CURSOR,
            session_id=composer_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            title=title,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            source_path=str(self.db_path),
            origin=origin,
        )
        return session, child_ids

    def _parse_bubble(
        self, bdata: Any, role: Role, session_fallback: datetime
    ) -> Optional[UnifiedMessage]:
        if not isinstance(bdata, dict):
            return None

        ts = session_fallback
        if bdata.get("createdAt") is not None:
            ts = _safe_ts(bdata.get("createdAt"), session_fallback)
        elif bdata.get("startedAtMs") is not None:
            ts = _safe_ts(bdata.get("startedAtMs"), session_fallback)

        text = bdata.get("text", "")
        if not isinstance(text, str):
            text = ""
        text = text[:_MAX_CONTENT_CHARS]

        reasoning_parts: List[str] = []
        thinking = bdata.get("thinking")
        if isinstance(thinking, str) and thinking:
            reasoning_parts.append(thinking)
        elif isinstance(thinking, dict):
            ttext = thinking.get("text")
            if isinstance(ttext, str) and ttext:
                reasoning_parts.append(ttext)
        blocks = bdata.get("allThinkingBlocks")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict):
                    btext = block.get("text")
                    if isinstance(btext, str) and btext:
                        reasoning_parts.append(btext)
        reasoning: Optional[str] = None
        if reasoning_parts:
            reasoning = "\n".join(reasoning_parts)[:_MAX_REASONING_CHARS]

        tool_events: List[Dict[str, Any]] = []
        tool_data = bdata.get("toolFormerData")
        if isinstance(tool_data, dict):
            tool_name = tool_data.get("name", "tool")
            if not isinstance(tool_name, str):
                tool_name = "tool"
            inline = self._render_tool_inline(tool_name, tool_data.get("params"))
            if inline:
                text = f"{text}\n{inline}".strip() if text else inline
            status_raw = tool_data.get("status")
            status_str = str(status_raw).lower() if status_raw is not None else ""
            is_error = "error" in status_str or "fail" in status_str
            event_status = "failed" if is_error else "completed"
            duration_ms = self._tool_duration_ms(tool_data)
            tool_events.append(
                {
                    "name": tool_name,
                    "status": event_status,
                    "duration_ms": duration_ms,
                    "is_error": is_error,
                }
            )

        model: Optional[str] = None
        model_info = bdata.get("modelInfo")
        if isinstance(model_info, dict):
            mname = model_info.get("modelName")
            if isinstance(mname, str) and mname:
                model = mname

        tokens = self._format_token_count(bdata.get("tokenCount"))

        if not text and not reasoning and not tool_events:
            return None

        return UnifiedMessage(
            role=role,
            content=text,
            timestamp=ts,
            model=model,
            tokens=tokens,
            reasoning=reasoning,
            tool_events=tool_events,
        )

    @staticmethod
    def _format_token_count(raw: Any) -> Optional[Dict[str, int]]:
        if not isinstance(raw, dict):
            return None
        inp = raw.get("inputTokens")
        out = raw.get("outputTokens")
        if isinstance(inp, int) and isinstance(out, int):
            return {"input": inp, "output": out, "total": inp + out}
        return None

    @staticmethod
    def _tool_duration_ms(tool_data: dict) -> Optional[int]:
        start = tool_data.get("startedAtMs")
        end = tool_data.get("completedAtMs")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            return None
        if not math.isfinite(start) or not math.isfinite(end):
            return None
        delta = end - start
        if delta < 0:
            return None
        return int(delta)

    @staticmethod
    def _render_tool_inline(tool_name: str, params: Any) -> str:
        rendered: List[str] = []
        if isinstance(params, dict):
            for key, value in params.items():
                if not isinstance(key, str) or _is_secret_composer_field(key):
                    continue
                if isinstance(value, (dict, list)):
                    rendered.append(f"{key}=<{type(value).__name__}>")  # may hold nested secrets
                    continue
                v_str = str(value)
                if len(v_str) > _MAX_TOOL_ARG_CHARS:
                    v_str = v_str[:_MAX_TOOL_ARG_CHARS]
                rendered.append(f"{key}={v_str}")
        suffix = (" " + " ".join(rendered)) if rendered else ""
        return f"[Tool: {tool_name}]{suffix}"

    def _parse_sidebar_tab(self, tab: dict) -> Optional[UnifiedSession]:
        tab_id = tab.get("tabId", "unknown")
        title = tab.get("chatTitle", "")
        title = title[:_MAX_TITLE_CHARS] if isinstance(title, str) else ""
        created_at = _safe_ts(tab.get("timestamp", 0), datetime.now())
        if created_at.year < 2000:
            created_at = datetime.now()

        messages = []
        bubbles = tab.get("bubbles")
        for bubble in bubbles if isinstance(bubbles, list) else []:
            if not isinstance(bubble, dict):
                continue
            role = Role.USER if bubble.get("type") == "user" else Role.ASSISTANT
            text = bubble.get("text", "") or bubble.get("richText", "")
            if text:
                messages.append(
                    UnifiedMessage(
                        role=role,
                        content=str(text)[:_MAX_CONTENT_CHARS],
                        timestamp=created_at,
                    )
                )

        if not messages:
            return None
        project_path = self._infer_project_path(tab)
        return UnifiedSession(
            tool=Tool.CURSOR,
            session_id=f"sidebar-{str(tab_id)[:200]}",
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
                for k, v in obj.items():
                    if _is_secret_composer_field(k):
                        continue
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
