"""Hermes agent extractor — per-profile ``state.db`` (issue #197 / B2.5)."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import make_naive, parse_timestamp
from ..utils.paths import make_thread_id
from ..utils.sqlite_copy import copy_sqlite_with_sidecars
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_MAX_SQLITE_BYTES = 512 * 1024 * 1024
_MAX_SESSIONS_PER_DB = 5000
_MAX_MESSAGES_PER_SESSION = 5000
_MAX_MESSAGES_PER_DB = 200_000
_MAX_CONTENT_CHARS = 100_000
_MAX_TOOL_ARG_CHARS = 200
_MAX_PATH_CHARS = 4096
_SQLITE_TIMEOUT = 2.0
_QUERY_SECONDS = 10.0

_INTERACTIVE_SOURCES = frozenset(
    {
        "cli",
        "telegram",
        "discord",
        "slack",
        "whatsapp",
        "signal",
        "web",
        "tui",
        "matrix",
        "irc",
    }
)

_SESSION_COLUMNS = (
    "id",
    "source",
    "title",
    "cwd",
    "git_branch",
    "model",
    "started_at",
    "ended_at",
    "last_activity_at",
    "parent_session_id",
    "end_reason",
)

_MESSAGE_COLUMNS = (
    "id",
    "session_id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "active",
    "compacted",
    "_compressed_summary",
    "display_kind",
)


def activity_fingerprint_ns(value: Any) -> Optional[int]:
    """Map Hermes ``last_activity_at`` (epoch seconds) to an ns fingerprint."""
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(float(value) * 1_000_000_000)


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    try:
        return make_naive(parse_timestamp(value))
    except (OverflowError, OSError, ValueError, TypeError, RecursionError):
        return fallback


def _clean_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_PATH_CHARS:
        return None
    if any(c in value for c in "\x00\r\n") or ".." in Path(value).parts:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return value


def _clean_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _map_origin(source: Any) -> str:
    if not isinstance(source, str) or not source.strip():
        return "unknown"
    key = source.strip().lower()
    if key == "cron":
        return "cron"
    if key == "oneshot":
        return "exec"
    if key in _INTERACTIVE_SOURCES:
        return "interactive"
    return "unknown"


def _relation_for_parent(end_reason: Any) -> str:
    if isinstance(end_reason, str):
        low = end_reason.lower()
        if "compress" in low or "compact" in low or low in {"continuation", "context_reset"}:
            return "continuation"
    return "subagent"


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows if len(row) > 1}


_MAX_TOOL_CALLS_JSON_CHARS = 64 * 1024
_MAX_REASONING_CHARS = 20_000
_MAX_SHORT_TEXT_CHARS = 512


def _select_expr(columns: Set[str], name: str) -> str:
    """Select a column with a SQL-side size bound so a hostile row cannot exhaust memory."""
    if name not in columns:
        return "NULL"
    if name in ("content", "_compressed_summary"):
        return f"substr({name}, 1, {_MAX_CONTENT_CHARS})"
    if name == "tool_calls":
        # Oversized tool-call JSON is dropped (NULL) instead of being parsed.
        return f"CASE WHEN length({name}) > {_MAX_TOOL_CALLS_JSON_CHARS} THEN NULL ELSE {name} END"
    if name in ("reasoning", "reasoning_content"):
        return f"substr({name}, 1, {_MAX_REASONING_CHARS})"
    if name in ("tool_call_id", "tool_name", "finish_reason", "display_kind", "role"):
        return f"substr({name}, 1, {_MAX_SHORT_TEXT_CHARS})"
    return name


class HermesExtractor(BaseExtractor):
    """Reads ``~/.hermes/profiles/*/state.db`` and ``~/.hermes/state.db`` via scratch copies."""

    tool = Tool.HERMES
    PARSER_VERSION = 1
    AUTOMATED_BYPASSES_PROMPT_FLOOR = True

    def __init__(self, hermes_root: Optional[Path] = None) -> None:
        self.hermes_root = hermes_root
        self.record_counts: Dict[str, int] = {}

    def source_description(self) -> str:
        return "~/.hermes/profiles/*/state.db, ~/.hermes/state.db"

    def is_available(self) -> bool:
        return bool(self._discover_databases())

    def _count_record(self, key: str, n: int = 1) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + n

    def _hermes_base(self) -> Path:
        if self.hermes_root is not None:
            return self.hermes_root
        return Path.home() / ".hermes"

    def _discover_databases(self) -> List[Tuple[Optional[str], Path]]:
        """Return ``(profile_name, db_path)`` pairs. Profile ``None`` is the root db."""
        base = self._hermes_base()
        found: List[Tuple[Optional[str], Path]] = []
        root_db = base / "state.db"
        if root_db.exists():
            if root_db.is_symlink():
                found.append((None, root_db))
            elif self._is_plain_db(root_db):
                found.append((None, root_db))
        profiles = base / "profiles"
        try:
            if profiles.is_dir() and not profiles.is_symlink():
                for entry in sorted(profiles.iterdir(), key=lambda p: p.name):
                    try:
                        if not entry.is_dir() or entry.is_symlink():
                            continue
                    except OSError:
                        continue
                    db_path = entry / "state.db"
                    if not db_path.exists():
                        continue
                    if db_path.is_symlink() or self._is_plain_db(db_path):
                        found.append((entry.name, db_path))
        except OSError:
            pass
        return found

    @staticmethod
    def _is_plain_db(path: Path) -> bool:
        try:
            return path.is_file() and not path.is_symlink()
        except OSError:
            return False

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        # Bounded SQLite sources: we do not call ``_source_unchanged`` (per-file mtime).
        # Incremental reuse keys off ``source_path#session`` plus ``source_mtime_ns``.
        databases = self._discover_databases()
        if not databases:
            return

        id_profiles: Dict[str, Set[Optional[str]]] = {}
        for profile, db_path in databases:
            for session_id in self._list_session_ids(db_path):
                if session_id:
                    id_profiles.setdefault(session_id, set()).add(profile)

        seen_ids: Set[str] = set()
        for profile, db_path in databases:
            if db_path.is_symlink():
                self._count_record("skipped_databases")
                continue
            for session in self._parse_database(db_path, profile, id_profiles):
                if session.session_id in seen_ids:
                    self._count("duplicate_sessions")
                    continue
                if self.should_import_session(session):
                    seen_ids.add(session.session_id)
                    yield session

    def _list_session_ids(self, db_path: Path) -> List[str]:
        ids: List[str] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), _MAX_SQLITE_BYTES)
                if copy_path is None:
                    return []
                conn = self._open_copy(copy_path)
                if conn is None:
                    return []
                try:
                    cols = _table_columns(conn, "sessions")
                    if "id" not in cols:
                        return []
                    cur = conn.execute(f"SELECT id FROM sessions LIMIT {_MAX_SESSIONS_PER_DB + 1}")
                    for row in cur:
                        sid = row[0]
                        if isinstance(sid, str) and sid:
                            ids.append(sid)
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError):
            return []
        return ids

    def _open_copy(self, copy_path: Path) -> Optional[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(
                f"file:{copy_path}?mode=ro",
                uri=True,
                timeout=_SQLITE_TIMEOUT,
            )
            deadline = time.monotonic() + _QUERY_SECONDS
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            return conn
        except sqlite3.Error:
            return None

    @staticmethod
    def _qualify_id(
        session_id: str, profile: Optional[str], id_profiles: Dict[str, Set[Optional[str]]]
    ) -> str:
        """Return a stable id: qualified with the profile for EVERY copy of a shared id.

        The result must not depend on which profile is scanned first, or a session could
        change its id when another profile appears or disappears.
        """
        if len(id_profiles.get(session_id, ())) > 1:
            return f"{profile or 'root'}:{session_id}"
        return session_id

    def _parse_database(
        self,
        db_path: Path,
        profile: Optional[str],
        id_profiles: Dict[str, Set[Optional[str]]],
    ) -> Iterator[UnifiedSession]:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                copy_path = copy_sqlite_with_sidecars(db_path, Path(tmp), _MAX_SQLITE_BYTES)
                if copy_path is None:
                    self._count_record("skipped_databases")
                    return
                conn = self._open_copy(copy_path)
                if conn is None:
                    self._count_record("skipped_databases")
                    return
                try:
                    session_cols = _table_columns(conn, "sessions")
                    if "id" not in session_cols:
                        self._count_record("skipped_databases")
                        return
                    message_cols = _table_columns(conn, "messages")
                    if not message_cols:
                        self._count_record("skipped_databases")
                        return
                    usage_cols = _table_columns(conn, "session_model_usage")
                    session_rows = self._load_sessions(conn, session_cols)
                    if session_rows is None:
                        self._count_record("skipped_databases")
                        return
                    self._count("files_parsed")
                    parent_reasons: Dict[str, Any] = {}
                    for srow in session_rows:
                        sid = srow[0]
                        end_reason = srow[10] if len(srow) > 10 else None
                        if isinstance(sid, str):
                            parent_reasons[sid] = end_reason
                    usage_totals = self._load_usage_totals(conn, usage_cols)
                    total_messages = 0
                    for row in session_rows:
                        if total_messages >= _MAX_MESSAGES_PER_DB:
                            # Whole-database budget: bounded memory regardless of how
                            # many sessions a hostile database claims to hold.
                            self._count_record("skipped_oversize")
                            break
                        session = self._build_session(
                            db_path=db_path,
                            profile=profile,
                            row=row,
                            conn=conn,
                            message_cols=message_cols,
                            parent_reasons=parent_reasons,
                            usage_total=usage_totals.get(row[0]),
                            id_profiles=id_profiles,
                        )
                        if session is not None:
                            self._count("files_matched")
                            total_messages += len(session.messages)
                            yield session
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError):
            self._count_record("skipped_databases")

    def _load_sessions(
        self, conn: sqlite3.Connection, columns: Set[str]
    ) -> Optional[List[Tuple[Any, ...]]]:
        select = ", ".join(_select_expr(columns, name) for name in _SESSION_COLUMNS)
        try:
            cur = conn.execute(
                f"SELECT {select} FROM sessions ORDER BY id LIMIT ?",
                (_MAX_SESSIONS_PER_DB + 1,),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            return None
        if len(rows) > _MAX_SESSIONS_PER_DB:
            self._count_record("skipped_oversize")
            rows = rows[:_MAX_SESSIONS_PER_DB]
        return [tuple(r) for r in rows]

    def _load_usage_totals(self, conn: sqlite3.Connection, columns: Set[str]) -> Dict[str, int]:
        if "session_id" not in columns:
            return {}
        totals: Dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT session_id, input_tokens, output_tokens, total_tokens "
                "FROM session_model_usage LIMIT 50000"
            ).fetchall()
        except sqlite3.Error:
            return {}
        for row in rows:
            if len(row) < 2:
                continue
            sid, inp, out, total = (row + (None, None, None))[:4]
            if not isinstance(sid, str):
                continue
            session_total = 0
            if isinstance(total, (int, float)) and total > 0:
                session_total = int(total)
            else:
                for part in (inp, out):
                    if isinstance(part, (int, float)):
                        session_total += int(part)
            if session_total > 0:
                totals[sid] = totals.get(sid, 0) + session_total
        return totals

    def _build_session(
        self,
        *,
        db_path: Path,
        profile: Optional[str],
        row: Tuple[Any, ...],
        conn: sqlite3.Connection,
        message_cols: Set[str],
        parent_reasons: Dict[str, Any],
        usage_total: Optional[int],
        id_profiles: Dict[str, Set[Optional[str]]],
    ) -> Optional[UnifiedSession]:
        data = dict(zip(_SESSION_COLUMNS, row, strict=False))
        raw_session_id = _clean_str(data.get("id"))
        if not raw_session_id:
            return None
        session_id = self._qualify_id(raw_session_id, profile, id_profiles)

        fallback = datetime.fromtimestamp(db_path.stat().st_mtime)
        started = _safe_ts(data.get("started_at"), fallback)
        ended = _safe_ts(data.get("ended_at"), started)
        last_activity = data.get("last_activity_at")
        last_updated = _safe_ts(last_activity, ended)

        messages, last_assistant_idx = self._load_messages(
            conn, message_cols, raw_session_id, fallback
        )
        if not messages:
            return None

        raw_parent_id = _clean_str(data.get("parent_session_id"))
        parent_id = self._qualify_id(raw_parent_id, profile, id_profiles) if raw_parent_id else None
        relation: Optional[str] = None
        if raw_parent_id:
            relation = _relation_for_parent(parent_reasons.get(raw_parent_id))

        project_path = _clean_path(data.get("cwd"))
        title = _clean_str(data.get("title"))

        session = UnifiedSession(
            tool=Tool.HERMES,
            session_id=session_id,
            created_at=started,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=make_thread_id(project_path=project_path),
            title=title,
            title_source=TitleSource.NATIVE if title else None,
            git_branch=_clean_str(data.get("git_branch")),
            source_path=f"{db_path}#{raw_session_id}",
            source_mtime_ns=activity_fingerprint_ns(last_activity),
            origin=_map_origin(data.get("source")),
            parent_session_id=parent_id,
            relation=relation,
        )

        if usage_total is not None and last_assistant_idx >= 0:
            tok = dict(session.messages[last_assistant_idx].tokens or {})
            tok["session_total"] = usage_total
            session.messages[last_assistant_idx].tokens = tok

        return session

    def _load_messages(
        self,
        conn: sqlite3.Connection,
        columns: Set[str],
        session_id: str,
        fallback: datetime,
    ) -> Tuple[List[UnifiedMessage], int]:
        select = ", ".join(_select_expr(columns, name) for name in _MESSAGE_COLUMNS)
        try:
            cur = conn.execute(
                f"SELECT {select} FROM messages WHERE session_id = ? ORDER BY id LIMIT ?",
                (session_id, _MAX_MESSAGES_PER_SESSION + 1),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            return [], -1
        if len(rows) > _MAX_MESSAGES_PER_SESSION:
            self._count_record("skipped_oversize")
            rows = rows[:_MAX_MESSAGES_PER_SESSION]

        messages: List[UnifiedMessage] = []
        pending_calls: Dict[str, List[int]] = {}
        last_assistant_idx = -1

        for raw in rows:
            data = dict(zip(_MESSAGE_COLUMNS, raw, strict=False))
            role_raw = data.get("role")
            if role_raw == "session_meta":
                continue
            if not isinstance(role_raw, str):
                self._count("records_unknown")
                continue

            ts = _safe_ts(data.get("timestamp"), fallback)
            active_val = data.get("active")
            is_active = True
            if active_val is not None and active_val in (0, False):
                is_active = False

            compacted = data.get("compacted")
            compressed = data.get("_compressed_summary")
            display_kind = data.get("display_kind")
            if (compacted not in (None, 0, False)) or (
                isinstance(compressed, str) and compressed.strip()
            ):
                text = (
                    compressed
                    if isinstance(compressed, str) and compressed.strip()
                    else (_clean_str(data.get("content")) or "")
                )
                messages.append(
                    UnifiedMessage(
                        role=Role.INFO,
                        content=text[:_MAX_CONTENT_CHARS],
                        timestamp=ts,
                        kind="compaction_summary",
                        active=is_active,
                    )
                )
                continue

            if display_kind in ("steer", "internal_notification"):
                content = _clean_str(data.get("content")) or ""
                messages.append(
                    UnifiedMessage(
                        role=Role.USER,
                        content=content[:_MAX_CONTENT_CHARS],
                        timestamp=ts,
                        kind="injected_context",
                        active=is_active,
                    )
                )
                continue

            if role_raw == "user":
                content = _clean_str(data.get("content")) or ""
                messages.append(
                    UnifiedMessage(
                        role=Role.USER,
                        content=content[:_MAX_CONTENT_CHARS],
                        timestamp=ts,
                        active=is_active,
                    )
                )
            elif role_raw == "assistant":
                content, reasoning, call_ids = self._format_assistant(data)
                msg = UnifiedMessage(
                    role=Role.ASSISTANT,
                    content=content[:_MAX_CONTENT_CHARS],
                    timestamp=ts,
                    reasoning=reasoning,
                    active=is_active,
                )
                token_count = data.get("token_count")
                if isinstance(token_count, (int, float)) and token_count > 0:
                    msg.tokens = {"total": int(token_count)}
                messages.append(msg)
                last_assistant_idx = len(messages) - 1
                for cid in call_ids:
                    pending_calls.setdefault(cid, []).append(last_assistant_idx)
            elif role_raw == "tool":
                content = _clean_str(data.get("content")) or ""
                messages.append(
                    UnifiedMessage(
                        role=Role.TOOL,
                        content=content[:_MAX_CONTENT_CHARS],
                        timestamp=ts,
                        active=is_active,
                    )
                )
                call_id = _clean_str(data.get("tool_call_id"))
                if call_id and call_id in pending_calls and pending_calls[call_id]:
                    idx = pending_calls[call_id].pop(0)
                    if 0 <= idx < len(messages):
                        owner = messages[idx]
                        if owner.role == Role.ASSISTANT:
                            status, is_error = self._tool_status(data, content)
                            name = _clean_str(data.get("tool_name")) or "tool"
                            owner.tool_events = list(owner.tool_events) + [
                                {
                                    "name": name,
                                    "status": status,
                                    "duration_ms": None,
                                    "is_error": is_error,
                                }
                            ]
            else:
                self._count("records_unknown")

        return messages, last_assistant_idx

    @staticmethod
    def _tool_status(data: Dict[str, Any], content: str) -> Tuple[str, bool]:
        finish = data.get("finish_reason")
        if isinstance(finish, str) and finish.lower() in {"error", "failed", "failure"}:
            return "failed", True
        low = content.lower()
        if "error" in low or "failed" in low:
            return "failed", True
        return "completed", False

    def _format_assistant(self, data: Dict[str, Any]) -> Tuple[str, Optional[str], List[str]]:
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        call_ids: List[str] = []

        base_content = _clean_str(data.get("content"))
        if base_content:
            text_parts.append(base_content)

        for key in ("reasoning", "reasoning_content"):
            part = _clean_str(data.get(key))
            if part:
                reasoning_parts.append(part)

        raw_calls = data.get("tool_calls")
        if isinstance(raw_calls, str) and raw_calls.strip():
            try:
                parsed = json.loads(raw_calls)
            except (json.JSONDecodeError, ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                for entry in parsed:
                    if not isinstance(entry, dict):
                        continue
                    fn = entry.get("function")
                    name = "tool"
                    args_text = ""
                    if isinstance(fn, dict):
                        if isinstance(fn.get("name"), str):
                            name = fn["name"]
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            args_text = args
                        elif args is not None:
                            try:
                                args_text = json.dumps(args, ensure_ascii=False, default=str)
                            except (TypeError, ValueError):
                                args_text = str(args)
                    if len(args_text) > _MAX_TOOL_ARG_CHARS:
                        args_text = args_text[:_MAX_TOOL_ARG_CHARS]
                    suffix = f" {args_text}" if args_text else ""
                    text_parts.append(f"[Tool: {name}]{suffix}")
                    call_id = entry.get("call_id") or entry.get("id")
                    if isinstance(call_id, str) and call_id:
                        call_ids.append(call_id)
        content = "\n".join(text_parts)
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        return content, reasoning, call_ids
