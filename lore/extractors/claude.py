import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)

_MAX_REASONING_CHARS = 20_000

# Record types that are intentionally not turned into messages (dossier noise list).
_KNOWN_NON_MESSAGE_RECORD_TYPES = frozenset(
    {
        "attachment",
        "queue-operation",
        "file-history-snapshot",
        "last-prompt",
        "agent-name",
        "permission-mode",
        "progress",
        "cost-state",
    }
)

_SYSTEM_SILENT_SUBTYPES = frozenset({"compact_boundary", "turn_duration"})


def _parse_usage_tokens(usage) -> dict | None:
    """Map a Claude ``message.usage`` block to a UnifiedMessage tokens dict.

    Claude reports ``input_tokens`` / ``output_tokens`` (plus cache counters). We
    keep input/output/total so total_tokens can aggregate them (#67).
    """
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if not isinstance(inp, (int, float)) and not isinstance(out, (int, float)):
        return None
    inp = int(inp) if isinstance(inp, (int, float)) else 0
    out = int(out) if isinstance(out, (int, float)) else 0
    if inp == 0 and out == 0:
        return None
    return {"input": inp, "output": out, "total": inp + out}


def _stringify_tool_result(content) -> str:
    """Flatten a Claude tool_result ``content`` to text.

    The field is either a plain string or a list of content blocks
    (``{"type": "text", "text": ...}`` / image refs). Join the text blocks
    and keep a placeholder for non-text so nothing silently vanishes.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(f"[{block.get('type', 'block')}]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


class ClaudeCodeExtractor(BaseExtractor):
    """Extract chat history from Claude Code."""

    # 2: subagent transcripts carry ``origin="subagent"``. They keep the prompt floor
    # they always had (only sessions imported before are tagged, the index does not grow).
    # 3: subagent sessions also carry ``parent_session_id`` and ``relation="subagent"``.
    # 4: titles, thinking, model, compaction/meta kinds, known-record telemetry.
    PARSER_VERSION = 4
    AUTOMATED_BYPASSES_PROMPT_FLOOR = False

    def __init__(self):
        self.base_path = Path.home() / ".claude" / "projects"
        self.base_paths = self._discover_base_paths()

    def _discover_base_paths(self):
        paths = [self.base_path]
        for candidate in discover_home_marker_paths(".claude/projects"):
            if candidate not in paths:
                paths.append(candidate)
        return [path for path in paths if path.exists()]

    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def is_available(self) -> bool:
        return len(self.base_paths) > 0

    def source_description(self) -> str:
        return "~/.claude/projects/*/*.jsonl, ~/.claude/projects/*/*/subagents/*.jsonl"

    def _decode_project_name(self, encoded: str) -> str:
        """Decode project path from directory name."""
        if not encoded.startswith("-"):
            return encoded

        # Remove leading dash and split by dash
        parts = encoded[1:].split("-")

        # Try to reconstruct the path by testing which combinations exist
        result_parts = []
        current = ""
        matched_any = False

        for i, part in enumerate(parts):
            if current:
                test_path = current + "-" + part
            else:
                test_path = part

            if result_parts:
                full_test = "/" + "/".join(result_parts) + "/" + test_path
            else:
                full_test = "/" + test_path

            if os.path.exists(full_test):
                result_parts.append(test_path)
                current = ""
                matched_any = True
            else:
                current = test_path

        if current:
            result_parts.append(current)

        if not matched_any:
            # Nothing on disk validated any split — the result is a pure
            # heuristic guess that may silently mis-decode (#124).
            logger.debug(
                "Project path reconstruction found no on-disk prefix match "
                "for %r; result %r is heuristic",
                encoded,
                "/" + "/".join(result_parts),
            )

        if result_parts:
            return "/" + "/".join(result_parts)

        # Degenerate input (nothing left after the leading dash) — the
        # naive replace cannot be validated against disk at all (#124).
        logger.debug(
            "Project path reconstruction had no parts for %r; using naive dash-split",
            encoded,
        )
        return "/" + encoded[1:].replace("-", "/")

    def find_session_by_id(self, session_id: str) -> Optional[UnifiedSession]:
        """Locate one session by id, parsing only its source file (#127).

        Main sessions live at ``<projectdir>/<session_id>.jsonl``, so the
        file is directly addressable — no per-project glob, no reading of
        every transcript in the archive. Subagent transcripts keep
        composite ids (``<parent>:agent-xxx``) with non-addressable file
        names and simply miss here, falling through to the caller's bulk
        scan. Mirrors the main-session branch of :meth:`extract_sessions`
        including the import-quality filter.
        """
        if not self.is_available():
            return None
        for base_path in self.base_paths:
            for project_dir in base_path.iterdir():
                if not project_dir.is_dir():
                    continue
                candidate = project_dir / f"{session_id}.jsonl"
                if not candidate.is_file():
                    continue
                try:
                    project_path = self._decode_project_name(project_dir.name)
                    session = self._parse_session(candidate, project_path)
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", candidate, e)
                    continue
                if self.should_import_session(session):
                    return session
        return None

    def extract_sessions(self) -> Iterator[UnifiedSession]:
        if not self.is_available():
            return

        # Claude Code occasionally stores the same sessionId under two
        # different project directories (e.g. when a session is resumed via
        # --continue from a different cwd). Both files exist on disk and both
        # claim the same id, which then violates UNIQUE(sessions.id) in the
        # v2 store. Keep the most recently modified copy and drop the rest.
        seen: dict[str, UnifiedSession] = {}

        for base_path in self.base_paths:
            for project_dir in base_path.iterdir():
                if not project_dir.is_dir():
                    continue

                project_path = self._decode_project_name(project_dir.name)

                # Main sessions: <projectdir>/<sessionid>.jsonl
                for jsonl_file in project_dir.glob("*.jsonl"):
                    self._count("files_matched")
                    if self._source_unchanged(jsonl_file):
                        continue
                    try:
                        session = self._parse_session(jsonl_file, project_path)
                        self._count("files_parsed")

                        if self.should_import_session(session):
                            self._add_or_replace_newer(seen, session)
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", jsonl_file, e)

                # Subagent sessions: <projectdir>/<sessionid>/subagents/agent-XXX.jsonl
                # Claude Code stores each Task-tool subagent invocation as its
                # own JSONL alongside the parent. Without this loop the heavy
                # multi-agent workflows (the bulk of the user's activity)
                # would all be invisible — only the 46 direct-child files
                # would show up while the 300+ subagent transcripts get lost.
                for subagent_file in project_dir.glob("*/subagents/*.jsonl"):
                    self._count("files_matched")
                    if self._source_unchanged(subagent_file):
                        continue
                    try:
                        parent_session_id = subagent_file.parent.parent.name
                        session = self._parse_session(
                            subagent_file,
                            project_path,
                            parent_session_id=parent_session_id,
                        )
                        self._count("files_parsed")

                        if self.should_import_session(session):
                            self._add_or_replace_newer(seen, session)
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", subagent_file, e)

        yield from seen.values()

    def _flatten_message_content(
        self,
        content,
        tool_use_by_id: dict[str, dict],
        *,
        collect_thinking: bool = False,
    ) -> tuple[str, list[dict], Optional[str]]:
        """Turn Claude message content into text, tool_calls, and optional reasoning."""
        if isinstance(content, str):
            return content, [], None
        if not isinstance(content, list):
            return str(content) if content is not None else "", [], None

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        reasoning_parts: list[str] = []
        reasoning_len = 0
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    text_parts.append(item.get("text", ""))
                elif item_type == "thinking" and collect_thinking:
                    thinking = item.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        # Cap while collecting: a multi-megabyte block must never be
                        # held (or joined) in full just to be cut afterwards.
                        room = _MAX_REASONING_CHARS - reasoning_len
                        if room > 0:
                            piece = thinking[:room]
                            reasoning_parts.append(piece)
                            reasoning_len += len(piece) + 1
                elif item_type == "tool_use":
                    tool_calls.append(item)
                    tool_use_id = item.get("id")
                    if tool_use_id:
                        tool_use_by_id[tool_use_id] = item
                    tool_name = item.get("name", "Unknown")
                    tool_input = item.get("input", {})
                    if isinstance(tool_input, dict):
                        if tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            text_parts.append(f"[Tool: {tool_name}]\n```bash\n{cmd}\n```")
                        elif tool_name in ("Read", "Write", "Edit"):
                            file_path = tool_input.get("file_path", "")
                            text_parts.append(f"[Tool: {tool_name}] {file_path}")
                        elif tool_name == "Glob":
                            pattern = tool_input.get("pattern", "")
                            text_parts.append(f"[Tool: {tool_name}] {pattern}")
                        elif tool_name == "Grep":
                            pattern = tool_input.get("pattern", "")
                            text_parts.append(f"[Tool: {tool_name}] {pattern}")
                        else:
                            text_parts.append(f"[Tool: {tool_name}]")
                elif item_type == "tool_result":
                    tool_id = item.get("tool_use_id", "")
                    result_content = _stringify_tool_result(item.get("content", ""))
                    text_parts.append(f"[Tool Result]\n{result_content}")
                    parent_call = tool_use_by_id.get(tool_id)
                    if parent_call is not None:
                        parent_call["output"] = result_content
                        if item.get("is_error"):
                            parent_call["status"] = "error"
            elif isinstance(item, str):
                text_parts.append(item)
        content_text = "\n".join(text_parts)
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
        if reasoning and len(reasoning) > _MAX_REASONING_CHARS:
            reasoning = reasoning[:_MAX_REASONING_CHARS]
        return content_text, tool_calls, reasoning

    @staticmethod
    def _add_or_replace_newer(seen: "dict[str, UnifiedSession]", session: UnifiedSession) -> None:
        """Insert ``session`` unless an entry with the same id is newer."""
        existing = seen.get(session.session_id)
        if existing is None:
            seen[session.session_id] = session
            return
        # last_updated is set in _parse_session for every parsed file.
        if session.last_updated > existing.last_updated:
            logger.debug(
                "Duplicate session id %s: keeping newer copy from %s",
                session.session_id,
                session.source_path,
            )
            seen[session.session_id] = session

    def _parse_session(
        self, path: Path, project_path: str, parent_session_id: str | None = None
    ) -> UnifiedSession:
        """Parse a single JSONL session file.

        When parent_session_id is set, the file is a subagent transcript:
        title gets a [subagent] prefix and thread_id is bound to the parent
        so the UI groups them together.
        """
        messages = []
        summary = None
        custom_title = None
        ai_title = None
        title_source: Optional[TitleSource] = None
        session_id = path.stem
        if parent_session_id:
            # Subagent files use names like 'agent-a9dff77.jsonl' which are
            # not globally unique across parent sessions — disambiguate.
            session_id = f"{parent_session_id}:{path.stem}"
        cli_version = None
        git_branch = None
        created_at = None
        # tool_use and its matching tool_result live in DIFFERENT JSONL records
        # (the result arrives in the next user message). Keep a reference to each
        # tool_use dict so we can attach the result to it once it shows up. The
        # dicts are stored by reference inside messages[], so mutating them here
        # after the loop is fine.
        tool_use_by_id: dict[str, dict] = {}
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

                if record_type == "summary":
                    summary = self._sanitize_summary(record.get("summary"))
                elif record_type == "custom-title":
                    custom_title = self._sanitize_summary(record.get("customTitle"))
                elif record_type == "ai-title":
                    raw_ai = record.get("aiTitle")
                    if raw_ai is None:
                        raw_ai = record.get("title")
                    ai_title = self._sanitize_summary(raw_ai)
                elif record_type == "system":
                    subtype = record.get("subtype")
                    if subtype not in _SYSTEM_SILENT_SUBTYPES:
                        self._count("records_unknown")
                elif record_type in _KNOWN_NON_MESSAGE_RECORD_TYPES:
                    continue
                elif record_type == "user":
                    msg_data = record.get("message", {})
                    if record.get("isCompactSummary") is True:
                        role = Role.INFO
                        kind = "compaction_summary"
                    else:
                        role = Role.USER
                        if (
                            record.get("isMeta") is True
                            or record.get("isVisibleInTranscriptOnly") is True
                        ):
                            kind = "injected_context"
                        else:
                            kind = "text"
                    content, tool_calls, _ = self._flatten_message_content(
                        msg_data.get("content", ""),
                        tool_use_by_id,
                    )
                    timestamp = parse_timestamp(record.get("timestamp", ""))
                    if created_at is None or timestamp < created_at:
                        created_at = timestamp
                    if last_updated is None or timestamp > last_updated:
                        last_updated = timestamp
                    if cli_version is None:
                        cli_version = record.get("version")
                    if git_branch is None:
                        git_branch = record.get("gitBranch")
                    if session_id == path.stem:
                        session_id = record.get("sessionId", path.stem)
                    messages.append(
                        UnifiedMessage(
                            role=role,
                            content=content,
                            timestamp=timestamp,
                            message_id=record.get("uuid"),
                            tool_calls=tool_calls if tool_calls else [],
                            tokens=_parse_usage_tokens(msg_data.get("usage")),
                            kind=kind,
                        )
                    )
                elif record_type == "assistant":
                    msg_data = record.get("message", {})
                    content, tool_calls, reasoning = self._flatten_message_content(
                        msg_data.get("content", ""),
                        tool_use_by_id,
                        collect_thinking=True,
                    )
                    model = msg_data.get("model")
                    if not isinstance(model, str):
                        model = None
                    timestamp = parse_timestamp(record.get("timestamp", ""))
                    if created_at is None or timestamp < created_at:
                        created_at = timestamp
                    if last_updated is None or timestamp > last_updated:
                        last_updated = timestamp
                    if cli_version is None:
                        cli_version = record.get("version")
                    if git_branch is None:
                        git_branch = record.get("gitBranch")
                    if session_id == path.stem:
                        session_id = record.get("sessionId", path.stem)
                    messages.append(
                        UnifiedMessage(
                            role=Role.ASSISTANT,
                            content=content,
                            timestamp=timestamp,
                            message_id=record.get("uuid"),
                            tool_calls=tool_calls if tool_calls else [],
                            tokens=_parse_usage_tokens(msg_data.get("usage")),
                            reasoning=reasoning,
                            model=model,
                        )
                    )
                else:
                    self._count("records_unknown")

        if created_at is None:
            created_at = datetime.fromtimestamp(path.stat().st_mtime)
        if last_updated is None:
            last_updated = datetime.fromtimestamp(path.stat().st_mtime)

        if custom_title:
            title = custom_title
            title_source = TitleSource.NATIVE
        elif ai_title:
            title = ai_title
            title_source = TitleSource.NATIVE
        else:
            title = summary
        if parent_session_id:
            agent_label = path.stem
            if title:
                title = f"[subagent {agent_label}] {title}"
            else:
                title = f"[subagent {agent_label}]"

        thread_id = make_thread_id(project_path=project_path)
        if parent_session_id:
            thread_id = f"claude-code:{parent_session_id}"

        return UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id=session_id,
            created_at=created_at,
            last_updated=last_updated,
            messages=messages,
            project_path=project_path,
            thread_id=thread_id,
            title=title,
            summary=summary,
            title_source=title_source,
            origin="subagent" if parent_session_id else "unknown",
            parent_session_id=parent_session_id,
            relation="subagent" if parent_session_id else None,
            cli_version=cli_version,
            git_branch=git_branch,
            source_path=str(path),
        )

    def _sanitize_summary(self, value):
        if not isinstance(value, str):
            return None
        summary = value.strip()
        if not summary:
            return None

        lowered = summary.lower()
        bad_markers = (
            "<command-name>",
            "<command-message>",
            "<command-args>",
            "<local-command-",
            "caveat: the messages below were generated by the user",
            "agents.md instructions",
            "instructions for /home",
        )
        if any(marker in lowered for marker in bad_markers):
            return None

        return summary
