"""Read Visual Studio 2022 Copilot's versioned MessagePack session streams."""

import hashlib
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypeGuard
from uuid import UUID

import msgpack

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import BaseExtractor
from lore.utils.home_discovery import discover_home_marker_paths

logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def _json_safe(value: Any) -> Any:
    """Keep provider tool envelopes serializable, including binary identifiers."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, msgpack.Timestamp):
        return value.to_datetime().isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class VisualStudioCopilotExtractor(BaseExtractor):
    """Discover project-local .vs histories without scanning entire volumes."""

    @property
    def tool(self) -> Tool:
        return Tool.VISUAL_STUDIO_COPILOT

    def source_description(self) -> str:
        """Describe the bounded project locations searched."""
        return (
            "~/source/repos/**/.vs/*/copilot-chat/*/sessions/*; current project .vs; "
            "LORE_EXTRA_HOME_PATHS profiles; LORE_VISUAL_STUDIO_PROJECT_PATHS roots"
        )

    def _sources(self) -> Iterator[tuple[Path, Path]]:
        roots = discover_home_marker_paths("source/repos")
        for raw in os.environ.get("LORE_VISUAL_STUDIO_PROJECT_PATHS", "").split(os.pathsep):
            if raw.strip():
                root = Path(raw.strip()).expanduser()
                if not root.is_dir():
                    logger.warning("Visual Studio project root unavailable: %s", root)
                else:
                    roots.append(root)
        queue = deque((root, 0) for root in roots)
        queue.append((Path.cwd(), 3))
        seen: set[Path] = set()
        seen_sources: set[Path] = set()
        # shortcut: three project levels and 2000 directories, configure narrower roots for deeper trees.
        while queue and len(seen) < 2000:
            project, depth = queue.popleft()
            try:
                project = project.resolve()
                if project in seen:
                    continue
                seen.add(project)
                vs = project / ".vs"
                if vs.is_dir():
                    for path in vs.glob("*/copilot-chat/*/sessions/*"):
                        if path.is_file():
                            resolved = path.resolve()
                            if resolved not in seen_sources:
                                seen_sources.add(resolved)
                                yield resolved, project
                if depth < 3:
                    for child in project.iterdir():
                        if (
                            not child.name.startswith(".")
                            and child.name not in {"node_modules", "bin", "obj", "build", "dist"}
                            and not child.is_symlink()
                            and child.is_dir()
                        ):
                            queue.append((child, depth + 1))
            except (OSError, RuntimeError) as exc:
                logger.warning("Cannot scan Visual Studio project %s: %s", project, exc)

    def is_available(self) -> bool:
        """Whether a candidate history file exists."""
        return next(self._sources(), None) is not None

    @property
    def storage_paths(self) -> list[Path]:
        """Expose session directories to the shared polling watcher."""
        return list(dict.fromkeys(path.parent for path, _ in self._sources()))

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        """Parse each source independently and apply the shared import policy."""
        for path, project in self._sources():
            self._count("files_matched")
            if self._source_unchanged(path):
                continue
            try:
                session = self._parse(path, project)
            except (OSError, ValueError, TypeError, OverflowError, msgpack.UnpackException) as exc:
                logger.warning("Cannot read Visual Studio session %s: %s", path, exc)
                continue
            self._count("files_parsed")
            if self.should_import_session(session):
                yield session

    def _parse(self, path: Path, project: Path) -> UnifiedSession:
        stat = path.stat()
        if stat.st_size > MAX_SOURCE_BYTES:
            raise ValueError("session exceeds 64 MiB limit")
        with path.open("rb") as source:
            data = source.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("session exceeds 64 MiB limit")
        unpacker = msgpack.Unpacker(
            raw=False, strict_map_key=False, max_buffer_size=MAX_SOURCE_BYTES
        )
        unpacker.feed(data)
        version = next(unpacker, None)
        if type(version) is not int or version != 1:
            raise ValueError("unsupported MessagePack format version")
        header = next(unpacker, None)
        if not isinstance(header, dict) or not isinstance(
            header.get("TimeCreated"), msgpack.Timestamp
        ):
            raise ValueError("invalid session header or TimeCreated")
        created = header["TimeCreated"].to_datetime()
        messages = []
        complete = unpacker.tell()
        for record in unpacker:
            complete = unpacker.tell()
            if not self._union(record) or record[0] not in (0, 1):
                self._count("records_unknown")
                continue
            messages.append(self._message(record[0], record[1], created))
        if complete != len(data):
            raise ValueError("truncated MessagePack session")
        file_id = str(UUID(path.name))
        namespace = hashlib.sha256(str(path.parent.parent).encode()).hexdigest()[:16]
        return UnifiedSession(
            tool=self.tool,
            session_id=f"visual-studio:{namespace}:{file_id}",
            created_at=created,
            last_updated=max(created, datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
            messages=messages,
            title=header.get("Name") if isinstance(header.get("Name"), str) else None,
            project_path=str(project),
            source_path=str(path),
            source_mtime_ns=stat.st_mtime_ns,
            source_version=str(version),
            origin="interactive",
        )

    @staticmethod
    def _union(value: Any) -> TypeGuard[list[Any]]:
        return (
            isinstance(value, list)
            and len(value) == 2
            and type(value[0]) is int
            and isinstance(value[1], dict)
        )

    def _message(self, tag: int, record: dict, created: datetime) -> UnifiedMessage:
        text, reasoning, calls = [], [], []
        content = record.get("Content", [])
        if not isinstance(content, list):
            raise ValueError("invalid message Content")
        for item in content:
            if not self._union(item):
                self._count("records_unknown")
                continue
            kind, block = item
            value = block.get("Content")
            if kind in (0, 3, 1, 10):
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid text content")
                if not value:
                    continue
                if kind == 10:
                    reasoning.append(value)
                elif kind == 1:
                    language = (
                        block.get("LanguageMarkdownIdentifier") or block.get("Language") or ""
                    )
                    fence = "`" * max(3, max(map(len, re.findall(r"`+", value)), default=0) + 1)
                    text.append(f"{fence}{language}\n{value}\n{fence}")
                else:
                    text.append(value)
            elif kind == 7:
                function = block.get("Function")
                if not isinstance(function, dict):
                    self._count("records_unknown")
                    continue
                arguments = function.get("Arguments")
                if self._union(arguments) and "json" in arguments[1]:
                    arguments = arguments[1]["json"]
                calls.append(
                    {
                        "id": _json_safe(block.get("Id")),
                        "tool": function.get("Name") or "tool",
                        "input": _json_safe(arguments),
                        "output": self._tool_output(block.get("Result")),
                        "status": str(block.get("State") or block.get("Status", "unknown")),
                        "provider_status": block.get("Status"),
                    }
                )
            else:
                self._count("records_unknown")
        model = record.get("Model")
        if self._union(model):
            model = model[1]
        model_name = None
        if isinstance(model, dict):
            model_name = (
                model.get("ModelId") or model.get("Id") or model.get("Name") or model.get("Family")
            )
        message_id = record.get("MessageId")
        if isinstance(message_id, bytes):
            message_id = message_id.hex()
        return UnifiedMessage(
            role=Role.USER if tag == 0 else Role.ASSISTANT,
            content="\n\n".join(text),
            timestamp=created,
            message_id=message_id if isinstance(message_id, str) else None,
            model=model_name if isinstance(model_name, str) else None,
            tool_calls=calls,
            reasoning="\n\n".join(reasoning) or None,
            extensions={
                "visual_studio": {"timestamp_inferred": True, "timestamp_source": "TimeCreated"}
            },
        )

    def _tool_output(self, result: Any) -> Any:
        if self._union(result) and result[0] == 0:
            value = result[1].get("Value")
            if isinstance(value, dict):
                container = value.get("ValueContainer")
                if (
                    isinstance(container, list)
                    and len(container) == 3
                    and isinstance(container[1], bytes)
                    and container[2] is False
                ):
                    try:
                        return _json_safe(
                            msgpack.unpackb(container[1], raw=False, strict_map_key=False)
                        )
                    except (ValueError, msgpack.UnpackException):
                        pass
        if result is not None:
            self._count("records_unknown")
        return _json_safe(result)
