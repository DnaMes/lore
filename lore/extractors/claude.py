import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..core.models import Role, Tool, UnifiedMessage, UnifiedSession
from ..utils.datetime import parse_timestamp
from ..utils.home_discovery import discover_home_marker_paths
from ..utils.paths import make_thread_id
from .base import BaseExtractor

logger = logging.getLogger(__name__)


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

    def _decode_project_name(self, encoded: str) -> str:
        """Decode project path from directory name."""
        if not encoded.startswith("-"):
            return encoded

        # Remove leading dash and split by dash
        parts = encoded[1:].split("-")

        # Try to reconstruct the path by testing which combinations exist
        result_parts = []
        current = ""

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
            else:
                current = test_path

        if current:
            result_parts.append(current)

        if result_parts:
            return "/" + "/".join(result_parts)

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
                    try:
                        session = self._parse_session(jsonl_file, project_path)

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
                    try:
                        parent_session_id = subagent_file.parent.parent.name
                        session = self._parse_session(
                            subagent_file,
                            project_path,
                            parent_session_id=parent_session_id,
                        )

                        if self.should_import_session(session):
                            self._add_or_replace_newer(seen, session)
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", subagent_file, e)

        yield from seen.values()

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
                elif record_type in ("user", "assistant"):
                    msg_data = record.get("message", {})
                    role = Role.USER if record_type == "user" else Role.ASSISTANT
                    content = msg_data.get("content", "")

                    # Handle content that is a list (tool use, etc.)
                    if isinstance(content, list):
                        text_parts = []
                        tool_calls = []
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("type") == "text":
                                    text_parts.append(item.get("text", ""))
                                elif item.get("type") == "tool_use":
                                    tool_calls.append(item)
                                    tool_use_id = item.get("id")
                                    if tool_use_id:
                                        tool_use_by_id[tool_use_id] = item
                                    tool_name = item.get("name", "Unknown")
                                    tool_input = item.get("input", {})
                                    if isinstance(tool_input, dict):
                                        if tool_name == "Bash":
                                            cmd = tool_input.get("command", "")
                                            text_parts.append(
                                                f"[Tool: {tool_name}]\n```bash\n{cmd}\n```"
                                            )
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
                                elif item.get("type") == "tool_result":
                                    tool_id = item.get("tool_use_id", "")
                                    result_content = _stringify_tool_result(item.get("content", ""))
                                    # Full result kept — no truncation. The web view
                                    # collapses long output behind an expand toggle
                                    # (see session_view_prep), the source must stay
                                    # complete for search and forensics.
                                    text_parts.append(f"[Tool Result]\n{result_content}")
                                    # Attach the result back to its tool_use dict so
                                    # the structured renderer can show call+result
                                    # together instead of re-parsing the string.
                                    parent_call = tool_use_by_id.get(tool_id)
                                    if parent_call is not None:
                                        parent_call["output"] = result_content
                                        if item.get("is_error"):
                                            parent_call["status"] = "error"
                            elif isinstance(item, str):
                                text_parts.append(item)
                        content = "\n".join(text_parts)
                    else:
                        tool_calls = []

                    timestamp = parse_timestamp(record.get("timestamp", ""))

                    if created_at is None or timestamp < created_at:
                        created_at = timestamp
                    if last_updated is None or timestamp > last_updated:
                        last_updated = timestamp

                    # Extract version and branch
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
                        )
                    )

        if created_at is None:
            created_at = datetime.fromtimestamp(path.stat().st_mtime)
        if last_updated is None:
            last_updated = datetime.fromtimestamp(path.stat().st_mtime)

        title = summary
        if parent_session_id:
            agent_label = path.stem
            if summary:
                title = f"[subagent {agent_label}] {summary}"
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
