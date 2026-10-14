import hashlib
import json
import logging
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from ..utils.security import sanitize_path
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_HEADER_KEYS = frozenset(
    {"sessionId", "projectHash", "startTime", "lastUpdated", "kind", "summary", "directories"}
    | {"memoryScratchpad"}
)
_PATCH_CONTROL_KEYS = frozenset({"id", "updates", "removeIds", "orderIds"})
_MAX_SMALL_FILE = 1_000_000
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_LINE_CHARS = 8 * 1024 * 1024
_MAX_PATH_CHARS = 4096
_MAX_TOOL_ARG_CHARS = 200
_MAX_TOOL_OUTPUT_CHARS = 4000
_PEEK_LINES = 50
_PEEK_LINE_CHARS = 64 * 1024


def _safe_ts(value: Any, fallback: datetime) -> datetime:
    """Parse a timestamp from an untrusted file; ``fallback`` when it is out of range."""
    try:
        return parse_timestamp(value)
    except (OverflowError, OSError, ValueError, TypeError):
        return fallback


def _clean_path(value: Any) -> Optional[str]:
    """Return ``value`` when it is a plain absolute path without ``..`` or control bytes."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_PATH_CHARS or any(c in value for c in "\x00\r\n"):
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return value


class _Replay:
    """Message state of one session file, replayed from its JSONL records.

    ``msgs`` is keyed by message id (last write wins, position kept). ``rank`` orders
    them: appends take increasing ranks, ``orderIds`` hands out ranks below every
    existing one, so no operation scans the whole list per record.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.msgs: Dict[Any, Dict[str, Any]] = {}
        self.rank: Dict[Any, int] = {}
        self.hi = 0
        self.lo = 0
        self.anon = 0

    def put(self, msg: Dict[str, Any]) -> None:
        key: Any = msg.get("id")
        if not isinstance(key, str):
            self.anon += 1
            key = ("anon", self.anon)
        self.msgs[key] = msg
        if key not in self.rank:
            self.rank[key] = self.hi
            self.hi += 1

    def drop(self, key: str) -> None:
        self.msgs.pop(key, None)
        self.rank.pop(key, None)

    def move_to_front(self, ids: List[Any]) -> None:
        wanted = [i for i in ids if isinstance(i, str) and i in self.msgs]
        # Duplicates: the last mention decides the position.
        unique = list(reversed(dict.fromkeys(reversed(wanted))))
        for offset, key in enumerate(unique):
            self.rank[key] = self.lo - len(unique) + offset
        self.lo -= len(unique)

    def rewind_to(self, key: str) -> bool:
        if key not in self.msgs:
            return False
        if self.lo < 0:  # reordered: restore insertion order == rank order first
            order = sorted(self.rank, key=self.rank.__getitem__)
            self.msgs = {k: self.msgs[k] for k in order}
            self.rank = {k: i for i, k in enumerate(order)}
            self.hi, self.lo = len(order), 0
        while self.msgs:
            last, _ = self.msgs.popitem()
            self.rank.pop(last, None)
            if last == key:
                break
        return True

    def ordered(self) -> List[Dict[str, Any]]:
        return [self.msgs[k] for k in sorted(self.msgs, key=self.rank.__getitem__)]


class GeminiCLIExtractor(BaseExtractor):
    """Extract chat history from Gemini CLI."""

    # 2: the jsonl replay (B0.2) changed what sessions carry; version-1 rows
    # were indexed from the old, mostly empty reader.
    # 3: subagent sessions carry ``origin="subagent"``. They keep the prompt floor
    # they always had (only sessions imported before are tagged, the index does not grow).
    # 4: subagent sessions under ``chats/<parent>/`` also carry ``parent_session_id`` and
    # ``relation="subagent"``.
    PARSER_VERSION = 4
    AUTOMATED_BYPASSES_PROMPT_FLOOR = False

    def __init__(self):
        self.base_path = Path.home() / ".gemini" / "tmp"
        self.base_paths = self._discover_base_paths()
        self._hash_to_path: Dict[str, str] = {}
        self._slug_maps: Dict[Path, Dict[str, str]] = {}
        self._seen_session_ids: set[str] = set()
        # Main session file -> (header sessionId or file stem, resolved session_id), this run.
        self._main_ids: Dict[Path, tuple[str, str]] = {}
        # Replay telemetry: malformed_lines, unknown_records, unmatched_ids.
        self.record_counts: Dict[str, int] = {}
        self._build_hash_map()

    def _discover_base_paths(self):
        paths = [self.base_path]
        for candidate in discover_home_marker_paths(".gemini/tmp"):
            if candidate not in paths:
                paths.append(candidate)
        return [path for path in paths if path.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.GEMINI_CLI

    def source_description(self) -> str:
        return (
            "~/.gemini/tmp/*/chats/session-*.jsonl, ~/.gemini/tmp/*/chats/session-*.json, "
            "~/.gemini/tmp/*/chats/*/*.jsonl"
        )

    def is_available(self) -> bool:
        return len(self.base_paths) > 0

    def _build_hash_map(self):
        """Build hash->path mapping by scanning known project locations."""
        project_roots = []
        env_roots = os.environ.get("GEMINI_PROJECT_ROOTS")
        if env_roots:
            for raw in env_roots.split(os.pathsep):
                value = raw.strip()
                if not value:
                    continue
                try:
                    project_roots.append(sanitize_path(value, Path.home(), allow_absolute=True))
                except ValueError as exc:
                    logger.warning("Ignoring unsafe GEMINI_PROJECT_ROOTS path '%s': %s", value, exc)

        project_roots.extend(
            [
                Path.home() / "projects",
                Path.home() / "work",
                Path.home() / "code",
                Path.home(),
            ]
        )

        for root in project_roots:
            if not root.exists():
                continue
            try:
                self._scan_root(root, max_depth=3, max_dirs=2000)
            except PermissionError:
                continue

    def _scan_root(self, root: Path, max_depth: int = 2, max_dirs: int = 1000) -> None:
        queue = deque([(root, 0)])
        seen = 0
        while queue:
            current, depth = queue.popleft()
            try:
                if current.is_dir():
                    path_str = str(current.absolute())
                    hash_val = hashlib.sha256(path_str.encode()).hexdigest()
                    self._hash_to_path[hash_val] = path_str
                    seen += 1
            except PermissionError:
                continue

            if depth >= max_depth or seen >= max_dirs:
                continue

            try:
                for item in current.iterdir():
                    if item.is_dir():
                        queue.append((item, depth + 1))
            except PermissionError:
                continue

    def _resolve_hash(self, hash_val: str) -> Optional[str]:
        """Resolve project hash to path."""
        return self._hash_to_path.get(hash_val)

    def _count_record(self, key: str) -> None:
        self.record_counts[key] = self.record_counts.get(key, 0) + 1
        if key == "unknown_records":
            self._count("records_unknown")

    def _read_project_root(self, project_dir: Path) -> Optional[str]:
        """Read ``<slug>/.project_root`` (a plain absolute path)."""
        root_file = project_dir / ".project_root"
        try:
            if root_file.is_symlink() or not root_file.is_file():
                return None
            with open(root_file, "r", encoding="utf-8", errors="replace") as f:
                value = f.read(_MAX_PATH_CHARS + 1)
        except OSError:
            return None
        return _clean_path(value)

    def _slug_path(self, base_path: Path, slug: str) -> Optional[str]:
        """Look a slug up in the ``projects.json`` next to ``base_path``."""
        mapping = self._slug_maps.get(base_path)
        if mapping is None:
            mapping = {}
            try:
                projects_file = base_path.parent / "projects.json"
                if projects_file.stat().st_size <= _MAX_SMALL_FILE:
                    data = json.loads(projects_file.read_text(encoding="utf-8", errors="replace"))
                    projects = data.get("projects") if isinstance(data, dict) else None
                    if isinstance(projects, dict):
                        for path, name in projects.items():
                            clean = _clean_path(path)
                            if clean is not None and isinstance(name, str):
                                mapping.setdefault(name, clean)
            except (OSError, ValueError, RecursionError):
                pass
            self._slug_maps[base_path] = mapping
        return mapping.get(slug)

    def _project_path_for(self, project_dir: Path) -> Optional[str]:
        project_path = self._read_project_root(project_dir) or self._slug_path(
            project_dir.parent, project_dir.name
        )
        if project_path is None and len(project_dir.name) == 64:
            project_path = self._resolve_hash(project_dir.name)
        return project_path

    def _session_files(self, chats_dir: Path) -> Iterator[tuple[Path, Optional[str]]]:
        """Yield ``(file, parent_session_id)``; subagents live at chats/<parent>/<sub>.jsonl."""
        for pattern in ("session-*.jsonl", "session-*.json"):
            for path in sorted(chats_dir.glob(pattern)):
                if self._is_plain_file(path):
                    self._count("files_matched")
                    yield path, None
        for path in sorted(chats_dir.glob("*/*.jsonl")):
            if self._is_plain_file(path) and not path.parent.is_symlink():
                self._count("files_matched")
                yield path, path.parent.name

    def _peek_session_id(self, path: Path) -> Optional[str]:
        """Header ``sessionId`` of a main session file, read without replaying it."""
        try:
            if path.suffix == ".jsonl":
                found: Optional[str] = None
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for _ in range(_PEEK_LINES):
                        line = f.readline(_PEEK_LINE_CHARS)
                        if not line:
                            break
                        try:
                            rec = json.loads(line)
                        except (ValueError, RecursionError):
                            continue
                        if not isinstance(rec, dict):
                            continue
                        if isinstance(rec.get("$set"), dict):
                            rec = rec["$set"]
                        if isinstance(rec.get("sessionId"), str) and rec["sessionId"]:
                            found = rec["sessionId"]
                return found
            if path.stat().st_size > _MAX_SMALL_FILE:
                return None
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, RecursionError):
            return None
        value = data.get("sessionId") if isinstance(data, dict) else None
        return value if isinstance(value, str) and value else None

    def _main_session_ids(self, chats_dir: Path) -> Dict[str, str]:
        """Directory name -> real ``session_id`` of the main session that owns it.

        Subagents live at ``chats/<header sessionId>/``; a main file without a
        ``sessionId`` is addressed by its file stem. Files parsed in this run give the
        resolved id (after dedupe); unparsed ones (unchanged) are peeked.
        """
        mapping: Dict[str, str] = {}
        for pattern in ("session-*.jsonl", "session-*.json"):
            for path in sorted(chats_dir.glob(pattern)):
                if not self._is_plain_file(path):
                    continue
                known = self._main_ids.get(path)
                if known is None:
                    key = self._peek_session_id(path) or path.stem
                    known = (key, path.stem if key in self._seen_session_ids else key)
                mapping.setdefault(known[0], known[1])
        return mapping

    @staticmethod
    def _is_plain_file(path: Path) -> bool:
        """Regular file, not a symlink (a FIFO would block the reader forever)."""
        try:
            return not path.is_symlink() and path.is_file()
        except OSError:
            return False

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        self._seen_session_ids = set()
        self._main_ids = {}
        for base_path in self.base_paths:
            for project_dir in sorted(base_path.iterdir()):
                if not project_dir.is_dir():
                    continue

                chats_dir = project_dir / "chats"
                if not chats_dir.is_dir():
                    continue

                project_path = self._project_path_for(project_dir)
                dir_hash = project_dir.name if len(project_dir.name) == 64 else None

                parents: Optional[Dict[str, str]] = None
                for session_file, parent_id in self._session_files(chats_dir):
                    parent_session_id: Optional[str] = None
                    if parent_id:
                        if parents is None:
                            parents = self._main_session_ids(chats_dir)
                        parent_session_id = parents.get(parent_id)
                    if self._source_unchanged(session_file):
                        continue
                    try:
                        if session_file.stat().st_size > _MAX_FILE_BYTES:
                            self._count_record("skipped_oversize")
                            continue
                        session = self._parse_session_file(
                            session_file,
                            project_path,
                            dir_hash,
                            parent_dir=parent_id,
                            parent_session_id=parent_session_id,
                        )
                        self._count("files_parsed")
                        if self.should_import_session(session):
                            yield session
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                        OverflowError,
                        RecursionError,
                    ) as e:
                        logger.warning("Failed to parse %s: %s", session_file, type(e).__name__)

    def _apply_patch(self, patch: Dict[str, Any], state: _Replay) -> None:
        targets: List[Dict[str, Any]] = []
        if "id" in patch:
            targets.append(patch)
        updates = patch.get("updates")
        if isinstance(updates, list):
            targets.extend(u for u in updates if isinstance(u, dict))

        for item in targets:
            item_id = item.get("id")
            if not isinstance(item_id, str):
                self._count_record("unknown_records")
                continue
            target = state.msgs.get(item_id)
            if target is None:
                self._count_record("unmatched_ids")
                continue
            for key, value in item.items():
                if key in _PATCH_CONTROL_KEYS:
                    continue
                existing = target.get("toolCalls")
                if key == "toolCalls" and isinstance(existing, list) and isinstance(value, list):
                    index = {
                        tc["id"]: tc
                        for tc in existing
                        if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                    }
                    for call in value:
                        call_id = call.get("id") if isinstance(call, dict) else None
                        known = index.get(call_id) if isinstance(call_id, str) else None
                        if known is not None:
                            known.update(call)
                        else:
                            existing.append(call)
                else:
                    target[key] = value

        remove = patch.get("removeIds")
        if isinstance(remove, list):
            for mid in remove:
                if isinstance(mid, str):
                    state.drop(mid)

        order = patch.get("orderIds")
        if isinstance(order, list):
            state.move_to_front(order)

    def _apply_record(self, rec: Dict[str, Any], header: Dict[str, Any], state: _Replay) -> None:
        if "$set" in rec and isinstance(rec["$set"], dict):
            for key, value in rec["$set"].items():
                if key == "messages":
                    if isinstance(value, list):
                        state.reset()
                        for m in value:
                            if isinstance(m, dict):
                                state.put(m)
                else:
                    header[key] = value
        elif "$patch" in rec and isinstance(rec["$patch"], dict):
            self._apply_patch(rec["$patch"], state)
        elif "$rewindTo" in rec:
            target = rec["$rewindTo"]
            if not isinstance(target, str):
                self._count_record("unknown_records")
            elif not state.rewind_to(target):
                self._count_record("unmatched_ids")
        elif "type" in rec and "id" in rec:
            if isinstance(rec["id"], str):
                state.put(rec)
            else:
                self._count_record("unknown_records")
        elif "type" not in rec and _HEADER_KEYS & rec.keys():
            header.update(rec)
        else:
            self._count_record("unknown_records")

    def _replay_jsonl(self, path: Path) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        header: Dict[str, Any] = {}
        state = _Replay()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            while True:
                line = f.readline(_MAX_LINE_CHARS)
                if not line:
                    break
                if len(line) >= _MAX_LINE_CHARS and not line.endswith("\n"):
                    self._count_record("malformed_lines")
                    # Drop the rest of the over-long line chunk by chunk, never holding it.
                    while line and not line.endswith("\n"):
                        line = f.readline(_MAX_LINE_CHARS)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    self._count_record("malformed_lines")
                    continue
                if not isinstance(rec, dict):
                    self._count_record("malformed_lines")
                    continue
                self._apply_record(rec, header, state)
        return header, state.ordered()

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            content = [content]
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "".join(parts)
        return ""

    @staticmethod
    def _format_tool_call(tc: Dict[str, Any]) -> str:
        tool_name = tc.get("name", tc.get("displayName", "Unknown"))
        tool_args = tc.get("args", {})
        if not isinstance(tool_args, dict):
            tool_args = {}

        if tool_name in ("read_file", "ReadFile"):
            return f"[Tool: Read] {tool_args.get('file_path', '')}"
        if tool_name in ("write_file", "WriteFile"):
            return f"[Tool: Write] {tool_args.get('file_path', '')}"
        if tool_name in ("run_terminal_cmd", "Bash"):
            cmd = tool_args.get("command", tool_args.get("cmd", ""))
            return f"[Tool: Bash]\n```bash\n{cmd}\n```"
        if tool_name in ("web_fetch", "WebFetch"):
            return f"[Tool: WebFetch] {str(tool_args.get('prompt', ''))[:200]}..."
        if tool_name in ("web_search", "WebSearch"):
            return f"[Tool: WebSearch] {tool_args.get('query', '')}"
        if tool_name == "edit_file":
            return f"[Tool: Edit] {tool_args.get('file_path', '')}"
        if tool_name == "list_dir":
            return f"[Tool: ListDir] {tool_args.get('dir_path', tool_args.get('path', '.'))}"
        if tool_name == "glob":
            return f"[Tool: Glob] {tool_args.get('pattern', '')}"
        if tool_name == "grep":
            return f"[Tool: Grep] {tool_args.get('pattern', '')}"
        args_str = ", ".join(
            f"{k}={str(v)[:_MAX_TOOL_ARG_CHARS]}" for k, v in list(tool_args.items())[:3]
        )
        return f"[Tool: {tool_name}] {args_str}"

    def _build_message(self, msg: Dict[str, Any], fallback: datetime) -> Optional[UnifiedMessage]:
        msg_type = msg.get("type", "")
        if msg_type == "user":
            role = Role.USER
        elif msg_type == "gemini":
            role = Role.ASSISTANT
        elif msg_type in ("info", "error", "warning"):
            role = Role.INFO
        else:
            self._count_record("unknown_records")
            return None

        raw_thoughts = msg.get("thoughts")
        thoughts = (
            [t for t in raw_thoughts if isinstance(t, dict)]
            if isinstance(raw_thoughts, list)
            else []
        )
        reasoning = None
        if thoughts:
            reasoning = "\n\n".join(
                f"**{t.get('subject', 'Thought')}**: {t.get('description', '')}" for t in thoughts
            )

        content = self._content_text(msg.get("content", ""))
        raw_call_list = msg.get("toolCalls")
        raw_calls = (
            [tc for tc in raw_call_list if isinstance(tc, dict)]
            if isinstance(raw_call_list, list)
            else []
        )
        tool_calls: List[Dict[str, Any]] = []
        for tc in raw_calls:
            result = tc.get("result")
            if result is not None and not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            if isinstance(result, str) and len(result) > _MAX_TOOL_OUTPUT_CHARS:
                result = result[:_MAX_TOOL_OUTPUT_CHARS] + "\n[truncated]"
            tool_calls.append(
                {
                    "id": tc.get("id"),
                    "name": tc.get("name", tc.get("displayName", "Unknown")),
                    "input": tc.get("args") if isinstance(tc.get("args"), dict) else {},
                    "output": result,
                    "status": tc.get("status"),
                }
            )
        if raw_calls:
            rendered = "\n\n".join(self._format_tool_call(tc) for tc in raw_calls)
            content = f"{content}\n\n{rendered}" if content else rendered

        if reasoning and not content:
            content = reasoning
        elif reasoning and content:
            content = f"{content}\n\n---\n**Thoughts:**\n{reasoning}"

        tokens = msg.get("tokens")
        msg_id = msg.get("id")
        model = msg.get("model")
        return UnifiedMessage(
            role=role,
            content=content,
            timestamp=_safe_ts(msg.get("timestamp", ""), fallback),
            message_id=msg_id if isinstance(msg_id, str) else None,
            model=model if isinstance(model, str) else None,
            tokens=tokens if isinstance(tokens, dict) else None,
            tool_calls=tool_calls,
            reasoning=reasoning,
        )

    def _parse_session_file(
        self,
        path: Path,
        project_path: Optional[str],
        project_hash: Optional[str],
        parent_dir: Optional[str] = None,
        parent_session_id: Optional[str] = None,
    ) -> UnifiedSession:
        """Parse one Gemini session file (current JSONL log or legacy single JSON).

        ``parent_dir`` is the ``chats/<dir>/`` a subagent lives in; ``parent_session_id`` is
        the resolved session id of the main session that owns that directory (or None).
        """
        if path.suffix == ".jsonl":
            header, raw_messages = self._replay_jsonl(path)
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("legacy session file is not an object")
            raw = data.get("messages", [])
            raw_messages = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
            header = {k: v for k, v in data.items() if k != "messages"}

        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        messages = [m for m in (self._build_message(r, mtime) for r in raw_messages) if m]

        raw_hash = header.get("projectHash")
        if isinstance(raw_hash, str) and raw_hash:
            project_hash = raw_hash
        if project_path is None and project_hash and len(project_hash) == 64:
            project_path = self._resolve_hash(project_hash)

        summary = header.get("summary") if isinstance(header.get("summary"), str) else None
        title = summary
        thread_id = make_thread_id(project_path=project_path, project_hash=project_hash)

        def qualified(name: str) -> str:
            return f"{parent_dir}:{name}" if parent_dir else name

        header_id = header.get("sessionId")
        base_id = header_id if isinstance(header_id, str) and header_id else path.stem
        session_id = qualified(base_id)
        if session_id in self._seen_session_ids:
            session_id = qualified(path.stem)
        self._seen_session_ids.add(session_id)
        if parent_dir:
            thread_id = f"gemini-cli:{parent_dir}"
        else:
            self._main_ids[path] = (base_id, session_id)
        is_subagent = bool(parent_dir) or header.get("kind") == "subagent"
        if is_subagent:
            label = f"[subagent {path.stem}]"
            title = f"{label} {summary}" if summary else label

        start = header.get("startTime")
        updated = header.get("lastUpdated")
        created_at = (
            _safe_ts(start, mtime) if start else (messages[0].timestamp if messages else mtime)
        )
        last_updated = (
            _safe_ts(updated, mtime) if updated else (messages[-1].timestamp if messages else mtime)
        )

        return UnifiedSession(
            tool=Tool.GEMINI_CLI,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            project_hash=project_hash,
            thread_id=thread_id,
            title=title,
            summary=summary,
            origin="subagent" if is_subagent else "unknown",
            parent_session_id=parent_session_id,
            relation="subagent" if parent_session_id else None,
            source_path=str(path),
        )
