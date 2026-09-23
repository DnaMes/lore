"""OpenCode chat history extractor.

Extracts sessions from OpenCode's hierarchical storage:
- ~/.local/share/opencode/storage/session/ (session metadata)
- ~/.local/share/opencode/storage/message/ (message metadata)
- ~/.local/share/opencode/storage/part/ (message content parts)
"""

import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Optional, TypedDict

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import lore_home, make_thread_id
from ..utils.security import sanitize_path
from .base import BaseExtractor

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


# Shared between the light pass-1 scan and the legacy full loader so the
# column list can never drift between the two (#104).
_SQLITE_SESSION_SELECT = """
    SELECT id, project_id, directory, title, version, time_created, time_updated
    FROM session
    ORDER BY time_updated DESC, time_created DESC
"""


@dataclass
class _PendingSession:
    """Pass-1 winner record: session metadata only, no messages (#104).

    The heavy parse (messages + parts) is deferred to pass 2 and runs only
    for the last-write-wins winner of each id, keeping extraction memory
    bounded instead of holding every session's full transcript at once.
    Exactly one source kind is set: the file fields for a storage-root
    winner, the sqlite fields for a database winner.
    """

    session_id: str
    created_at: datetime
    updated_at: datetime
    updated_raw: int  # raw time.updated value, for the state cache
    # File source:
    root: Optional[Path] = None
    session_file: Optional[Path] = None
    session_data: Optional[Dict] = None
    # SQLite source:
    db_path: Optional[Path] = None
    row: Optional[sqlite3.Row] = None


@lru_cache(maxsize=4)
def _discover_home_subfolder_roots(home_path: str, max_depth: int, max_hits: int) -> List[Path]:
    """Find nested .opencode/storage roots under home with bounded traversal."""
    home = Path(home_path).expanduser()
    if not home.exists() or max_depth < 1 or max_hits < 1:
        return []

    roots: List[Path] = []
    skip_dirnames = {
        ".cache",
        ".local",
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "Library",
        "Downloads",
    }

    def walk(current: Path, depth: int) -> None:
        if len(roots) >= max_hits or depth > max_depth:
            return
        try:
            entries = list(current.iterdir())
        except OSError:
            return

        for entry in entries:
            if len(roots) >= max_hits:
                return
            if not entry.is_dir():
                continue

            name = entry.name
            if name in skip_dirnames:
                continue

            if name == ".opencode":
                storage = entry / "storage"
                if (storage / "session").exists() and (storage / "message").exists():
                    try:
                        roots.append(storage.resolve())
                    except Exception:
                        roots.append(storage)
                continue

            walk(entry, depth + 1)

    walk(home, 0)
    unique: List[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


# TypedDict definitions for JSON structures
class TimeDict(TypedDict, total=False):
    """Timestamp structure in OpenCode JSON."""

    created: int  # milliseconds
    updated: int
    start: int
    end: int
    completed: int


class ModelDict(TypedDict, total=False):
    """Model information in messages."""

    providerID: str
    modelID: str


class TokensDict(TypedDict, total=False):
    """Token usage information."""

    input: int
    output: int
    reasoning: int
    cache: Dict[str, int]


class SessionDict(TypedDict, total=False):
    """Session JSON structure."""

    id: str
    version: str
    projectID: str
    directory: str
    title: str
    time: TimeDict
    summary: Dict[str, int]


class MessageDict(TypedDict, total=False):
    """Message JSON structure."""

    id: str
    sessionID: str
    role: str
    time: TimeDict
    agent: str
    model: ModelDict
    modelID: str
    providerID: str
    tokens: TokensDict
    cost: float
    finish: str
    parentID: str
    mode: str


class PartDict(TypedDict, total=False):
    """Part JSON structure."""

    id: str
    sessionID: str
    messageID: str
    type: str
    text: str
    tool: str
    callID: str
    state: Dict
    time: TimeDict
    reason: str
    metadata: Dict
    synthetic: bool


@dataclass
class LoadStats:
    """Statistics for load operations with error tracking."""

    sessions_found: int = 0
    sessions_loaded: int = 0
    sessions_skipped: int = 0
    messages_found: int = 0
    messages_loaded: int = 0
    parts_found: int = 0
    parts_loaded: int = 0
    errors_file_not_found: int = 0
    errors_permission: int = 0
    errors_json_decode: int = 0
    errors_other: int = 0

    def print_summary(self) -> None:
        """Print summary statistics to stderr."""
        print("\n=== OpenCode Extraction Summary ===", file=sys.stderr)
        print(
            f"Sessions: {self.sessions_loaded}/{self.sessions_found} loaded "
            f"({self.sessions_skipped} skipped)",
            file=sys.stderr,
        )
        print(
            f"Messages: {self.messages_loaded}/{self.messages_found} loaded",
            file=sys.stderr,
        )
        print(f"Parts: {self.parts_loaded}/{self.parts_found} loaded", file=sys.stderr)

        if (
            self.errors_file_not_found
            or self.errors_permission
            or self.errors_json_decode
            or self.errors_other
        ):
            print(
                f"Errors: FileNotFound={self.errors_file_not_found}, "
                f"Permission={self.errors_permission}, "
                f"JSONDecode={self.errors_json_decode}, "
                f"Other={self.errors_other}",
                file=sys.stderr,
            )


class OpenCodeExtractor(BaseExtractor):
    """Extract chat history from OpenCode."""

    # Sanity cap per tool output — only guards against pathological multi-MB dumps;
    # raised from 50k so normal tool results are kept in full, matching the claude
    # extractor's full-result behaviour (#54). When the cap does fire, the tool_call
    # carries truncated=True so the UI can badge it.
    MAX_TOOL_PART_CHARS = 2_000_000

    def _clamp_tool_output(self, tool_output):
        """Return (output, truncated) — clamp only pathological outputs (#54)."""
        if isinstance(tool_output, str) and len(tool_output) > self.MAX_TOOL_PART_CHARS:
            clamped = (
                tool_output[: self.MAX_TOOL_PART_CHARS]
                + f"\n... (truncated, {len(tool_output)} chars total)"
            )
            return clamped, True
        return tool_output, False

    def __init__(self, force_full=True):
        self.storage_path = Path.home() / ".local" / "share" / "opencode" / "storage"
        self.session_path = self.storage_path / "session"
        self.message_path = self.storage_path / "message"
        self.part_path = self.storage_path / "part"
        self.cache_path = lore_home() / "cache"
        self.state_file = self.cache_path / "opencode_state.json"
        self.force_full = force_full

        self.stats = LoadStats()

        self._state_cache: Optional[Dict[str, int]] = None
        self.storage_roots = self._discover_storage_roots()
        self.sqlite_db_paths = self._discover_sqlite_db_paths()

    def _discover_sqlite_db_paths(self) -> List[Path]:
        db_paths: List[Path] = []

        def add_db(candidate: Path):
            try:
                resolved = candidate.expanduser().resolve()
            except Exception:
                resolved = candidate.expanduser()
            if resolved.exists() and resolved.is_file() and resolved not in db_paths:
                db_paths.append(resolved)

        add_db(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
        add_db(Path.home() / ".opencode" / "opencode.db")

        home_scan_depth = _env_int("OPENCODE_HOME_SCAN_DEPTH", 3)
        home_scan_limit = _env_int("OPENCODE_HOME_SCAN_LIMIT", 16)
        for discovered in discover_home_marker_paths(
            ".local/share/opencode/opencode.db",
            default_depth=home_scan_depth,
            default_limit=home_scan_limit,
        ):
            add_db(discovered)

        for discovered in discover_home_marker_paths(
            ".opencode/opencode.db",
            default_depth=home_scan_depth,
            default_limit=home_scan_limit,
        ):
            add_db(discovered)

        extra = os.environ.get("OPENCODE_SQLITE_PATHS", "").strip()
        if extra:
            for raw in extra.split(os.pathsep):
                value = raw.strip()
                if not value:
                    continue
                try:
                    add_db(sanitize_path(value, Path.home(), allow_absolute=True))
                except ValueError as exc:
                    logger.warning(
                        "Ignoring unsafe OPENCODE_SQLITE_PATHS entry '%s': %s",
                        value,
                        exc,
                    )

        return db_paths

    def _discover_storage_roots(self) -> List[Path]:
        roots: List[Path] = []

        def add_root(candidate: Path):
            try:
                resolved = candidate.expanduser().resolve()
            except Exception:
                resolved = candidate.expanduser()
            if not resolved.exists():
                return
            if (resolved / "session").exists() and (resolved / "message").exists():
                if resolved not in roots:
                    roots.append(resolved)

        add_root(self.storage_path)
        add_root(Path.home() / ".opencode" / "storage")
        add_root(Path.home() / ".opencode")

        home_scan_depth = _env_int("OPENCODE_HOME_SCAN_DEPTH", 3)
        home_scan_limit = _env_int("OPENCODE_HOME_SCAN_LIMIT", 16)
        if home_scan_depth > 0 and home_scan_limit > 0:
            for discovered in _discover_home_subfolder_roots(
                str(Path.home()), home_scan_depth, home_scan_limit
            ):
                add_root(discovered)

        for discovered in discover_home_marker_paths(
            ".local/share/opencode/storage",
            default_depth=home_scan_depth,
            default_limit=home_scan_limit,
        ):
            add_root(discovered)

        add_root(Path("/var/lib/opencode/storage"))
        add_root(Path("/etc/opencode/storage"))
        add_root(Path("/opt/opencode/storage"))

        extra = os.environ.get("OPENCODE_STORAGE_PATHS", "").strip()
        if extra:
            for raw in extra.split(os.pathsep):
                value = raw.strip()
                if not value:
                    continue
                try:
                    add_root(sanitize_path(value, Path.home(), allow_absolute=True))
                except ValueError as exc:
                    logger.warning(
                        "Ignoring unsafe OPENCODE_STORAGE_PATHS entry '%s': %s",
                        value,
                        exc,
                    )

        return roots

    @property
    def tool(self) -> Tool:
        return Tool.OPENCODE

    def is_available(self) -> bool:
        """Check if any OpenCode storage root is available."""
        return len(self.storage_roots) > 0 or len(self.sqlite_db_paths) > 0

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        """Extract all sessions from OpenCode storage roots.

        Two-pass to bound memory (#104): pass 1 reads only session metadata
        and decides the last-write-wins winner per id across all sources;
        pass 2 fully parses (messages + parts) only the winners and yields
        them in created_at order. Output is identical to the previous
        materialise-then-sort approach, without holding every session's
        transcript in memory at once (measured ~425 MB transient peak at
        259 sessions before this change).
        """
        if not self.is_available():
            return

        if not self.force_full:
            self._load_state_cache()

        winners: Dict[str, _PendingSession] = {}

        original_paths = (self.session_path, self.message_path, self.part_path)
        try:
            for root in self.storage_roots:
                session_path = root / "session"
                message_path = root / "message"
                part_path = root / "part"
                if not session_path.exists() or not message_path.exists():
                    continue

                self.session_path = session_path
                self.message_path = message_path
                self.part_path = part_path

                for session_file in session_path.rglob("ses_*.json"):
                    self.stats.sessions_found += 1
                    try:
                        session_data = self._safe_load_json(session_file)
                        if not session_data:
                            continue

                        session_id = session_data.get("id")
                        if not session_id:
                            continue

                        time_data = session_data.get("time", {})
                        updated_timestamp = time_data.get("updated", 0)
                        if not self.force_full and self._should_skip_session(
                            session_id, updated_timestamp
                        ):
                            self.stats.sessions_skipped += 1
                            continue

                        pending = _PendingSession(
                            session_id=session_id,
                            created_at=parse_timestamp(time_data.get("created", 0)),
                            updated_at=parse_timestamp(updated_timestamp),
                            updated_raw=int(updated_timestamp),
                            root=root,
                            session_file=session_file,
                            session_data=session_data,
                        )
                        existing = winners.get(session_id)
                        if existing is None or pending.updated_at >= existing.updated_at:
                            winners[session_id] = pending
                    except Exception as e:
                        logger.debug(f"Failed to parse session {session_file}: {e}")
                        self.stats.errors_other += 1
                        continue
        finally:
            self.session_path, self.message_path, self.part_path = original_paths

        for db_path in self.sqlite_db_paths:
            for pending in self._pending_sessions_from_sqlite(db_path):
                existing = winners.get(pending.session_id)
                if existing is None or pending.updated_at >= existing.updated_at:
                    winners[pending.session_id] = pending

        # Pass 2: yield winners oldest-first, parsing one session at a time.
        sqlite_conns: Dict[Path, sqlite3.Connection] = {}
        try:
            for pending in sorted(winners.values(), key=lambda p: p.created_at):
                session: Optional[UnifiedSession] = None
                if (
                    pending.session_file is not None
                    and pending.root is not None
                    and (pending.session_data is not None)
                ):
                    # Point the message/part loaders at the winning root.
                    self.session_path = pending.root / "session"
                    self.message_path = pending.root / "message"
                    self.part_path = pending.root / "part"
                    try:
                        session = self._parse_session(pending.session_file, pending.session_data)
                    except Exception as e:
                        logger.debug(f"Failed to parse session {pending.session_file}: {e}")
                        self.stats.errors_other += 1
                        continue
                elif pending.db_path is not None and pending.row is not None:
                    conn = sqlite_conns.get(pending.db_path)
                    if conn is None:
                        try:
                            conn = sqlite3.connect(f"file:{pending.db_path}?mode=ro", uri=True)
                            conn.row_factory = sqlite3.Row
                        except sqlite3.Error:
                            continue
                        sqlite_conns[pending.db_path] = conn
                    session = self._session_from_sqlite_row(
                        conn,
                        pending.session_id,
                        pending.row,
                        pending.db_path,
                        pending.created_at,
                        pending.updated_at,
                    )
                if session is None or not self.should_import_session(session):
                    continue

                if not self.force_full:
                    self._update_state_cache(pending.session_id, pending.updated_raw)
                self.stats.sessions_loaded += 1
                yield session
        finally:
            for conn in sqlite_conns.values():
                conn.close()
            self.session_path, self.message_path, self.part_path = original_paths

        if not self.force_full:
            # State updates happen during pass 2, so the cache is persisted
            # once the consumer has exhausted the generator.
            self._save_state_cache()
        self.stats.print_summary()

    def _pending_sessions_from_sqlite(self, db_path: Path) -> List[_PendingSession]:
        """Pass 1 for one sqlite source: metadata-only winner candidates (#104)."""
        pending: List[_PendingSession] = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return pending

        try:
            rows = conn.execute(_SQLITE_SESSION_SELECT).fetchall()
        except sqlite3.Error:
            conn.close()
            return pending
        finally:
            conn.close()

        for row in rows:
            self.stats.sessions_found += 1
            session_id = str(row["id"] or "")
            if not session_id:
                continue

            updated_raw = int(row["time_updated"] or 0)
            if not self.force_full and self._should_skip_session(session_id, updated_raw):
                self.stats.sessions_skipped += 1
                continue

            pending.append(
                _PendingSession(
                    session_id=session_id,
                    created_at=parse_timestamp(int(row["time_created"] or 0)),
                    updated_at=parse_timestamp(updated_raw),
                    updated_raw=updated_raw,
                    db_path=db_path,
                    row=row,
                )
            )
        return pending

    def _session_from_sqlite_row(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        row: sqlite3.Row,
        db_path: Path,
        created_at: datetime,
        updated_at: datetime,
    ) -> UnifiedSession:
        """Pass 2 for one sqlite winner: load messages and build the session."""
        messages = self._load_messages_from_sqlite(conn, session_id)
        return UnifiedSession(
            tool=Tool.OPENCODE,
            session_id=session_id,
            created_at=created_at,
            last_updated=updated_at,
            messages=messages,
            project_path=str(row["directory"] or "") or None,
            project_hash=str(row["project_id"] or "") or None,
            thread_id=make_thread_id(project_path=str(row["directory"] or "") or None),
            title=str(row["title"] or "") or None,
            cli_version=str(row["version"] or "") or None,
            source_path=str(db_path),
        )

    def _extract_sessions_from_sqlite(self, db_path: Path) -> Dict[str, UnifiedSession]:
        sessions_by_id: Dict[str, UnifiedSession] = {}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return sessions_by_id

        try:
            rows = conn.execute(_SQLITE_SESSION_SELECT).fetchall()
        except sqlite3.Error:
            conn.close()
            return sessions_by_id

        for row in rows:
            self.stats.sessions_found += 1
            session_id = str(row["id"] or "")
            if not session_id:
                continue

            created_raw = int(row["time_created"] or 0)
            updated_raw = int(row["time_updated"] or 0)
            if not self.force_full and self._should_skip_session(session_id, updated_raw):
                self.stats.sessions_skipped += 1
                continue

            messages = self._load_messages_from_sqlite(conn, session_id)
            session = UnifiedSession(
                tool=Tool.OPENCODE,
                session_id=session_id,
                created_at=parse_timestamp(created_raw),
                last_updated=parse_timestamp(updated_raw),
                messages=messages,
                project_path=str(row["directory"] or "") or None,
                project_hash=str(row["project_id"] or "") or None,
                thread_id=make_thread_id(project_path=str(row["directory"] or "") or None),
                title=str(row["title"] or "") or None,
                cli_version=str(row["version"] or "") or None,
                source_path=str(db_path),
            )

            if not self.should_import_session(session):
                continue

            sessions_by_id[session_id] = session
            self.stats.sessions_loaded += 1
            if not self.force_full:
                self._update_state_cache(session_id, updated_raw)

        conn.close()
        return sessions_by_id

    def _load_messages_from_sqlite(
        self, conn: sqlite3.Connection, session_id: str
    ) -> List[UnifiedMessage]:
        messages: List[UnifiedMessage] = []
        try:
            rows = conn.execute(
                """
                SELECT id, time_created, data
                FROM message
                WHERE session_id = ?
                ORDER BY time_created
                """,
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return messages

        self.stats.messages_found += len(rows)
        parts_by_message = self._load_parts_for_session_sqlite(conn, session_id)
        for row in rows:
            message_id = str(row["id"] or "")
            if not message_id:
                continue
            try:
                payload = json.loads(str(row["data"] or "{}"))
            except json.JSONDecodeError:
                self.stats.errors_json_decode += 1
                continue
            if not isinstance(payload, dict):
                continue

            role_str = str(payload.get("role") or "user")
            role = Role.USER if role_str == "user" else Role.ASSISTANT
            created_ms = int((payload.get("time") or {}).get("created") or row["time_created"] or 0)
            timestamp = parse_timestamp(created_ms)

            content, reasoning, tool_calls = self._assemble_message_content_from_parts(
                parts_by_message.get(message_id, []),
            )
            model = None
            model_dict = payload.get("model")
            if isinstance(model_dict, dict):
                provider = str(model_dict.get("providerID") or "")
                model_id = str(model_dict.get("modelID") or "")
                if provider and model_id:
                    model = f"{provider}/{model_id}"

            tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else None

            messages.append(
                UnifiedMessage(
                    role=role,
                    content=content,
                    timestamp=timestamp,
                    message_id=message_id,
                    model=model,
                    tokens=tokens,
                    reasoning=reasoning,
                    tool_calls=tool_calls,
                )
            )
            self.stats.messages_loaded += 1

        messages.sort(key=lambda message: message.timestamp)
        return messages

    def _load_parts_for_session_sqlite(
        self, conn: sqlite3.Connection, session_id: str
    ) -> Dict[str, List[Dict]]:
        parts_by_message: Dict[str, List[Dict]] = {}
        try:
            # Stream the cursor: fetchall() would hold every part's raw JSON at
            # once, and one real session carries ~350 MB of it.
            rows = conn.execute(
                """
                SELECT message_id, data
                FROM part
                WHERE session_id = ?
                ORDER BY message_id, time_created
                """,
                (session_id,),
            )
            for row in rows:
                self.stats.parts_found += 1
                try:
                    payload = json.loads(str(row["data"] or "{}"))
                except json.JSONDecodeError:
                    self.stats.errors_json_decode += 1
                    continue
                if isinstance(payload, dict):
                    message_id = str(row["message_id"] or "")
                    if not message_id:
                        continue
                    parts_by_message.setdefault(message_id, []).append(self._slim_part(payload))
                    self.stats.parts_loaded += 1
        except sqlite3.Error:
            return {}

        return parts_by_message

    @staticmethod
    def _slim_part(part: Dict) -> Dict:
        """Keep only the fields ``_assemble_message_content_from_parts`` reads.

        Tool parts carry large payloads the transcript never uses
        (``state.metadata``, attachments, diffs); dropping them here keeps a
        session's parts from pinning hundreds of MB until assembly.
        """
        slim = {key: part[key] for key in ("type", "text", "tool", "callID") if key in part}
        state = part.get("state")
        if isinstance(state, dict):
            slim["state"] = {
                key: state[key] for key in ("input", "output", "status") if key in state
            }
        return slim

    def _assemble_message_content_from_parts(
        self, parts: List[Dict]
    ) -> tuple[str, Optional[str], List[Dict]]:

        content_parts: List[str] = []
        reasoning = None
        tool_calls: List[Dict] = []

        for part in parts:
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text", "")
                if text:
                    content_parts.append(str(text))
            elif part_type == "reasoning":
                reasoning = str(part.get("text", "") or "")
            elif part_type == "tool":
                tool_name = str(part.get("tool") or "unknown")
                call_id = part.get("callID")
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                tool_input = state.get("input", {}) if isinstance(state, dict) else {}
                tool_output = state.get("output", "") if isinstance(state, dict) else ""
                status = state.get("status", "unknown") if isinstance(state, dict) else "unknown"

                tool_output, was_truncated = self._clamp_tool_output(tool_output)

                tool_calls.append(
                    {
                        "id": call_id,
                        "tool": tool_name,
                        "status": status,
                        "input": tool_input,
                        "output": tool_output,
                        "truncated": was_truncated,
                    }
                )

                if isinstance(tool_input, dict) and tool_input:
                    input_summary = ", ".join(
                        f"{key}={value}" for key, value in list(tool_input.items())[:3]
                    )
                    if len(tool_input) > 3:
                        input_summary += ", ..."
                    content_parts.append(f"[Tool: {tool_name}] {input_summary}")
                else:
                    content_parts.append(f"[Tool: {tool_name}]")

        content = "\n".join(content_parts) if content_parts else ""
        return content, reasoning, tool_calls

    def _parse_session(self, session_file: Path, session_data: Dict) -> Optional[UnifiedSession]:
        """Parse a single session with all its messages."""
        session_id = session_data.get("id")
        if not session_id:
            return None

        # Parse timestamps (milliseconds)
        time_data = session_data.get("time", {})
        created_at = parse_timestamp(time_data.get("created", 0))
        last_updated = parse_timestamp(time_data.get("updated", 0))

        # Session metadata
        title = session_data.get("title")
        project_path = session_data.get("directory")
        cli_version = session_data.get("version")
        project_hash = session_data.get("projectID")

        # Load messages
        messages = self._load_messages(session_id)

        return UnifiedSession(
            tool=Tool.OPENCODE,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            project_hash=project_hash,
            title=title,
            cli_version=cli_version,
            source_path=str(session_file),
            thread_id=make_thread_id(project_path=project_path),
        )

    def _load_messages(self, session_id: str) -> List[UnifiedMessage]:
        """Load all messages for a session."""
        messages = []
        message_dir = self.message_path / session_id

        if not message_dir.exists():
            return messages

        # Find all message files
        message_files = list(message_dir.glob("msg_*.json"))
        self.stats.messages_found += len(message_files)

        # Load every message's JSON up front (one pass), then batch-load every
        # part file for those message ids in one more pass, instead of a
        # separate glob()+open() per message inside the per-message loop
        # (#N+1 I/O). The part directory is keyed by the message's own "id"
        # field, not by its filename, so message content must be read first.
        loaded_message_data = []
        for message_file in message_files:
            try:
                message_data = self._safe_load_json(message_file)
                if message_data:
                    loaded_message_data.append(message_data)
            except Exception as e:
                logger.debug(f"Failed to parse message {message_file}: {e}")
                self.stats.errors_other += 1
                continue

        parts_by_message = self._load_all_parts_for_messages(
            message_data.get("id") for message_data in loaded_message_data if message_data.get("id")
        )

        for message_data in loaded_message_data:
            try:
                message = self._parse_message(message_data, parts_by_message)
                if message:
                    messages.append(message)
                    self.stats.messages_loaded += 1
            except Exception as e:
                logger.debug(f"Failed to parse message {message_data.get('id')}: {e}")
                self.stats.errors_other += 1
                continue

        # Sort messages by timestamp
        messages.sort(key=lambda m: m.timestamp)

        return messages

    def _load_all_parts_for_messages(self, message_ids) -> Dict[str, List[PartDict]]:
        """Batch-load and parse every part file for a set of message ids.

        Avoids the previous per-message glob()+open() call inside
        ``_assemble_message_content`` by loading every message's parts up
        front, once per message id, in a single pass over the session.
        """
        result: Dict[str, List[PartDict]] = {}
        for message_id in message_ids:
            parts_dir = self.part_path / message_id
            if not parts_dir.exists():
                continue

            part_files = list(parts_dir.glob("prt_*.json"))
            self.stats.parts_found += len(part_files)

            parts = []
            for part_file in part_files:
                try:
                    part_data = self._safe_load_json(part_file)
                    if part_data:
                        parts.append(part_data)
                        self.stats.parts_loaded += 1
                except Exception as e:
                    logger.debug(f"Failed to load part {part_file}: {e}")
                    self.stats.errors_other += 1
                    continue

            result[message_id] = parts

        return result

    def _parse_message(
        self,
        message_data: Dict,
        parts_by_message: Optional[Dict[str, List[PartDict]]] = None,
    ) -> Optional[UnifiedMessage]:
        """Parse a single message with all its parts."""
        message_id = message_data.get("id")
        if not message_id:
            return None

        # Parse role
        role_str = message_data.get("role", "user")
        try:
            role = Role.USER if role_str == "user" else Role.ASSISTANT
        except ValueError:
            role = Role.USER

        # Parse timestamp (milliseconds)
        time_data = message_data.get("time", {})
        created = time_data.get("created", 0)
        timestamp = parse_timestamp(created)

        # Model information
        model = None
        model_dict = message_data.get("model")
        if model_dict:
            provider = model_dict.get("providerID", "")
            model_id = model_dict.get("modelID", "")
            if provider and model_id:
                model = f"{provider}/{model_id}"
        elif message_data.get("providerID") and message_data.get("modelID"):
            model = f"{message_data.get('providerID')}/{message_data.get('modelID')}"

        # Token usage
        tokens = None
        tokens_dict = message_data.get("tokens")
        if tokens_dict:
            tokens = {
                "input": tokens_dict.get("input", 0),
                "output": tokens_dict.get("output", 0),
                "total": (tokens_dict.get("input", 0) + tokens_dict.get("output", 0)),
            }
            reasoning_tokens = tokens_dict.get("reasoning")
            if reasoning_tokens:
                tokens["reasoning"] = reasoning_tokens
        # Assemble content from parts (already batch-loaded by _load_messages)
        content, reasoning, tool_calls = self._assemble_message_content(
            (parts_by_message or {}).get(message_id, [])
        )

        return UnifiedMessage(
            role=role,
            content=content,
            timestamp=timestamp,
            message_id=message_id,
            model=model,
            tokens=tokens,
            reasoning=reasoning,
            tool_calls=tool_calls,
        )

    def _assemble_message_content(
        self, parts: List[PartDict]
    ) -> tuple[str, Optional[str], List[Dict]]:
        """Assemble message content from already-loaded parts.

        Returns:
            tuple of (content, reasoning, tool_calls)
        """
        if not parts:
            return "", None, []

        # Sort parts by timestamp (start time)
        def get_part_time(part: PartDict) -> int:
            time_data = part.get("time", {})
            return time_data.get("start", 0) or time_data.get("created", 0) or 0

        parts.sort(key=get_part_time)

        # Assemble content
        content_parts = []
        reasoning = None
        tool_calls = []

        for part in parts:
            part_type = part.get("type")

            if part_type == "text":
                # Regular text content
                text = part.get("text", "")
                if text:
                    content_parts.append(text)

            elif part_type == "reasoning":
                # Extended thinking/reasoning
                reasoning = part.get("text", "")

            elif part_type == "tool":
                # Tool call
                tool_name = part.get("tool", "unknown")
                call_id = part.get("callID")
                state = part.get("state", {})

                # Extract tool input/output
                tool_input = state.get("input", {})
                tool_output = state.get("output", "")
                status = state.get("status", "unknown")

                # Clamp only pathological multi-MB dumps (#54).
                tool_output, was_truncated = self._clamp_tool_output(tool_output)

                # Add to tool_calls list
                tool_calls.append(
                    {
                        "id": call_id,
                        "tool": tool_name,
                        "status": status,
                        "input": tool_input,
                        "output": tool_output,
                        "truncated": was_truncated,
                    }
                )

                # Add summary to content
                if tool_input and isinstance(tool_input, dict):
                    # Format tool input nicely
                    input_summary = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:3])
                    if len(tool_input) > 3:
                        input_summary += ", ..."
                    content_parts.append(f"[Tool: {tool_name}] {input_summary}")
                else:
                    content_parts.append(f"[Tool: {tool_name}]")

            elif part_type in ("step-start", "step-finish"):
                # Skip step markers (internal state)
                pass

            else:
                # Unknown part type - include text if present
                text = part.get("text", "")
                if text:
                    content_parts.append(f"[{part_type}] {text}")

        # Join content parts
        content = "\n".join(content_parts) if content_parts else ""

        return content, reasoning, tool_calls

    def _safe_load_json(self, path: Path) -> Optional[Dict]:
        """Safely load JSON file with error tracking."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            self.stats.errors_file_not_found += 1
            return None
        except PermissionError:
            self.stats.errors_permission += 1
            return None
        except json.JSONDecodeError:
            # Can happen with concurrent writes
            self.stats.errors_json_decode += 1
            return None
        except Exception as e:
            logger.debug(f"Unexpected error loading {path}: {e}")
            self.stats.errors_other += 1
            return None

    def _load_state_cache(self) -> None:
        """Load state cache for incremental extraction."""
        try:
            if self.state_file.exists():
                with open(self.state_file, "r") as f:
                    self._state_cache = json.load(f)
            else:
                self._state_cache = {}
        except (json.JSONDecodeError, PermissionError, OSError) as e:
            logger.debug(f"Failed to load state cache: {e}")
            self._state_cache = {}

    def _save_state_cache(self) -> None:
        """Save state cache for incremental extraction."""
        if self._state_cache is None:
            return

        try:
            # Ensure cache directory exists
            self.cache_path.mkdir(parents=True, exist_ok=True)

            # Write state cache
            with open(self.state_file, "w") as f:
                json.dump(self._state_cache, f)
        except (PermissionError, OSError) as e:
            logger.debug(f"Failed to save state cache: {e}")

    def _should_skip_session(self, session_id: str, updated_timestamp: int) -> bool:
        """Check if session should be skipped based on cache."""
        if self._state_cache is None:
            return False

        cached_timestamp = self._state_cache.get(session_id, 0)
        return updated_timestamp <= cached_timestamp

    def _update_state_cache(self, session_id: str, updated_timestamp: int) -> None:
        """Update state cache with session timestamp."""
        if self._state_cache is not None:
            self._state_cache[session_id] = updated_timestamp
