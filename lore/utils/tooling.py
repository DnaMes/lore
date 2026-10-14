from typing import Optional

CANONICAL_TOOLS = {
    "claude-code",
    "gemini-cli",
    "codex",
    "warp",
    "cursor",
    "vscode-copilot",
    "copilot-cli",
    "opencode",
    "antigravity",
    "aider",
    "pi",
}


TOOL_ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "gemini": "gemini-cli",
    "gemini-cli": "gemini-cli",
    "codex": "codex",
    "warp": "warp",
    "cursor": "cursor",
    "vscode": "vscode-copilot",
    "vscode-copilot": "vscode-copilot",
    "copilot": "vscode-copilot",
    "copilot-cli": "copilot-cli",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "aider": "aider",
    "pi": "pi",
    "pi-coding-agent": "pi",
}


SESSION_SWITCH_ALIASES = {
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "codex": "codex",
    "cursor": "cursor",
    "vscode-copilot": "vscode",
    "pi": "pi",
}


def normalize_tool_name(tool: Optional[str]) -> Optional[str]:
    if not tool:
        return tool
    return TOOL_ALIASES.get(tool, tool)


def is_supported_tool(tool: Optional[str]) -> bool:
    if not tool:
        return False
    normalized = normalize_tool_name(tool)
    return normalized in CANONICAL_TOOLS


def to_session_switch_tool(tool: str) -> Optional[str]:
    normalized = normalize_tool_name(tool)
    return SESSION_SWITCH_ALIASES.get(normalized)
