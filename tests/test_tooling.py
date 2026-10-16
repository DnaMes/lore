from lore.utils.tooling import (
    is_supported_tool,
    normalize_tool_name,
    to_session_switch_tool,
)


def test_normalize_tool_aliases():
    assert normalize_tool_name("gemini") == "gemini-cli"
    assert normalize_tool_name("claude") == "claude-code"
    assert normalize_tool_name("vscode") == "vscode-copilot"


def test_supported_tools_include_aliases():
    assert is_supported_tool("gemini")
    assert is_supported_tool("gemini-cli")
    assert is_supported_tool("claude")
    assert is_supported_tool("claude-code")


def test_session_switch_mapping():
    assert to_session_switch_tool("gemini") == "gemini"
    assert to_session_switch_tool("gemini-cli") == "gemini"
    assert to_session_switch_tool("claude-code") == "claude"
    assert to_session_switch_tool("zed") is None
