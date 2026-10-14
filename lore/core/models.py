from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


def _message_token_total(tokens: Optional[Dict[str, Any]]) -> int:
    """Best-effort token count for a single message, robust to extractor formats.

    Different tools store usage differently: some provide a precomputed ``total``,
    others only ``input``/``output`` (e.g. OpenCode's raw payload), Claude uses
    ``input_tokens``/``output_tokens``. Prefer an explicit ``total``; otherwise sum
    the input + output variants. Cache/reasoning counts are ignored to keep the
    headline number comparable across tools.
    """
    if not isinstance(tokens, dict):
        return 0

    explicit = tokens.get("total")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return int(explicit)

    total = 0
    for key in ("input", "output", "input_tokens", "output_tokens"):
        value = tokens.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


# Canonical keys for a tool_call dict. Extractors historically diverged
# (opencode "tool" vs claude "name", codex/copilot "arguments" vs "input"),
# which silently hid args for some tools in the render layer (#79).
def normalize_tool_call(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map any extractor's tool_call dict to the canonical shape.

    Canonical keys: ``tool`` (name), ``input`` (args), ``output``, ``id``,
    ``status``, ``truncated``. Accepts the legacy aliases ``name`` (→tool) and
    ``arguments`` (→input). Unknown extra keys are preserved so nothing is lost.
    """
    if not isinstance(raw, dict):
        return {"tool": "tool", "input": {}, "output": None}

    tool = raw.get("tool") or raw.get("name") or "tool"
    tool_input = raw.get("input")
    if tool_input is None:
        tool_input = raw.get("arguments")
    if tool_input is None:
        tool_input = {}

    canonical = {
        "id": raw.get("id"),
        "tool": tool,
        "input": tool_input,
        "output": raw.get("output"),
        "status": raw.get("status"),
        "truncated": bool(raw.get("truncated", False)),
    }
    # Preserve any extra keys (e.g. type, caller) without clobbering canonical ones.
    for key, value in raw.items():
        if key not in canonical and key not in ("name", "arguments"):
            canonical[key] = value
    return canonical


# Closed value sets for the contract fields; storage normalises anything else.
ORIGINS = ("interactive", "exec", "cron", "subagent", "unknown")
# Non-interactive work: stored, but hidden from default views and LLM stages
# unless ``include_automated`` is set (see ``lore.services.index.filter_automated``).
AUTOMATED_ORIGINS = ("exec", "cron", "subagent")
RELATIONS = ("subagent", "fork", "continuation", "compaction")
MESSAGE_KINDS = ("text", "injected_context", "compaction_summary")


class Tool(Enum):
    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
    VSCODE_COPILOT = "vscode-copilot"
    COPILOT_CLI = "copilot-cli"
    GEMINI_CLI = "gemini-cli"
    WARP = "warp"
    CODEX = "codex"
    OPENCODE = "opencode"
    ANTIGRAVITY = "antigravity"
    AIDER = "aider"
    PI = "pi"


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    INFO = "info"


@dataclass
class UnifiedMessage:
    """Single message in a conversation."""

    role: Role
    content: str
    timestamp: datetime
    message_id: Optional[str] = None
    model: Optional[str] = None
    tokens: Optional[Dict[str, int]] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    reasoning: Optional[str] = None
    kind: str = "text"  # one of MESSAGE_KINDS
    parent_id: Optional[str] = None
    active: bool = True
    # Compact [{name,status,duration_ms,is_error}] read back from storage; never
    # holds arguments or results, so renderers must not treat it as tool_calls.
    tool_events: List[Dict[str, Any]] = field(default_factory=list)


class TitleSource(Enum):
    """Source of the session title."""

    NATIVE = "native"  # Tool provided the title
    SUMMARY = "summary"  # Generated from summary field
    FIRST_MESSAGE = "first_message"  # Extracted from first user message
    KEYWORDS = "keywords"  # Generated via keyword extraction
    LLM = "llm"  # Generated via local LLM
    FALLBACK = "fallback"  # Generic date-based fallback


@dataclass
class UnifiedSession:
    """A complete conversation session."""

    tool: Tool
    session_id: str
    created_at: datetime
    last_updated: datetime
    messages: List[UnifiedMessage] = field(default_factory=list)
    project_path: Optional[str] = None
    project_hash: Optional[str] = None
    thread_id: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    cli_version: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    source_path: Optional[str] = None
    generated_title: Optional[str] = None
    title_source: Optional[TitleSource] = None
    todos: List[Dict[str, Any]] = field(default_factory=list)
    origin: str = "unknown"  # one of ORIGINS
    parent_session_id: Optional[str] = None
    relation: Optional[str] = None  # one of RELATIONS
    source_version: Optional[str] = None
    parser_version: int = 0

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def user_prompt_count(self) -> int:
        return sum(
            1 for msg in self.messages if msg.role == Role.USER and (msg.content or "").strip()
        )

    @property
    def assistant_message_count(self) -> int:
        return sum(
            1 for msg in self.messages if msg.role == Role.ASSISTANT and (msg.content or "").strip()
        )

    @property
    def total_tokens(self) -> Optional[int]:
        total = 0
        for msg in self.messages:
            total += _message_token_total(msg.tokens)
        return total if total > 0 else None
