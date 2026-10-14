"""Security utilities for lore."""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from lore.utils.tooling import normalize_tool_name

logger = logging.getLogger(__name__)


def sanitize_path(user_path: str, base_dir: Path, allow_absolute: bool = False) -> Path:
    """
    Validate that a user-provided path is safe and within the base directory.

    Args:
        user_path: User-provided path (may be relative or absolute)
        base_dir: Base directory that the path must be within
        allow_absolute: Whether to allow absolute paths outside base_dir

    Returns:
        Resolved safe path

    Raises:
        ValueError: If path traversal is detected or path is outside base_dir
    """
    if not user_path:
        raise ValueError("Path cannot be empty")

    # Convert to Path and expand user home directory
    try:
        path = Path(user_path).expanduser()
    except (ValueError, RuntimeError) as e:
        raise ValueError(f"Invalid path: {e}")

    # If absolute path and not allowed, reject
    if path.is_absolute() and not allow_absolute:
        raise ValueError(f"Absolute paths not allowed: {user_path}")

    # Resolve the full path (follows symlinks)
    if path.is_absolute():
        resolved = path.resolve()
        if not allow_absolute:
            raise ValueError(f"Absolute paths not allowed: {user_path}")
    else:
        resolved = (base_dir / path).resolve()
        # Ensure relative paths stay within base_dir
        try:
            resolved.relative_to(base_dir.resolve())
        except ValueError:
            raise ValueError(f"Path traversal detected: {user_path} resolves outside {base_dir}")

    # Check for suspicious patterns. For absolute paths these are not caught by
    # the relative_to() traversal check above, so ".." must be rejected outright
    # here rather than merely logged, or a caller can walk an absolute path
    # anywhere on disk (e.g. "/home/user/../../etc/passwd").
    path_str = str(user_path)
    suspicious_patterns = [
        "..",
        "~/",
        "${",
        "$(",
    ]
    for pattern in suspicious_patterns:
        if pattern in path_str:
            if path.is_absolute() and pattern == "..":
                raise ValueError(f"Path traversal detected: {user_path}")
            logger.warning(f"Suspicious pattern '{pattern}' in path: {user_path}")

    return resolved


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """
    Sanitize a filename to prevent directory traversal and invalid characters.

    Args:
        filename: User-provided filename
        max_length: Maximum filename length

    Returns:
        Safe filename

    Raises:
        ValueError: If filename is invalid or too long
    """
    if not filename:
        raise ValueError("Filename cannot be empty")

    # Remove path separators
    if "/" in filename or "\\" in filename or os.sep in filename:
        raise ValueError(f"Filename cannot contain path separators: {filename}")

    # Remove or replace invalid characters for common filesystems
    # Windows: < > : " / \ | ? *
    # Unix: just / and \0
    invalid_chars = '<>:"|?*\0'
    sanitized = filename
    for char in invalid_chars:
        sanitized = sanitized.replace(char, "_")

    # Limit length
    if len(sanitized) > max_length:
        raise ValueError(f"Filename too long (max {max_length}): {filename}")

    # Reject reserved names on Windows
    reserved = [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    ]
    if sanitized.upper() in reserved or sanitized.upper().split(".")[0] in reserved:
        raise ValueError(f"Reserved filename: {filename}")

    return sanitized


def validate_tool_executable(tool: str, executable_path: str) -> bool:
    """
    Validate that an executable path is safe to run.

    Args:
        tool: Tool name (for logging)
        executable_path: Path to executable

    Returns:
        True if safe, False otherwise
    """
    if not executable_path:
        logger.error(f"Empty executable path for tool: {tool}")
        return False

    # Check if file exists and is executable
    if not os.path.isfile(executable_path):
        logger.error(f"Executable not found: {executable_path}")
        return False

    if not os.access(executable_path, os.X_OK):
        logger.error(f"File is not executable: {executable_path}")
        return False

    # Reject suspicious paths
    suspicious = ["..", "~/", "${", "$(", ";", "|", "&", "\n", "\r"]
    for pattern in suspicious:
        if pattern in executable_path:
            logger.error(f"Suspicious pattern in executable path: {executable_path}")
            return False

    return True


ALLOWED_TOOLS = {
    "gemini": ["gemini", "gemini-cli"],
    "codex": ["codex", "codex-cli"],
    "claude": ["claude", "claude-code"],
    "cursor": ["cursor"],
    "copilot": ["gh", "copilot"],
}


def get_safe_executable(tool: str) -> Optional[str]:
    """
    Get the executable path for a tool safely.

    Args:
        tool: Tool name

    Returns:
        Safe executable path, or None if not found/not safe
    """
    import shutil

    if tool not in ALLOWED_TOOLS:
        logger.error(f"Tool not in allowed list: {tool}")
        return None

    # Try each possible executable name for this tool
    for exe_name in ALLOWED_TOOLS[tool]:
        executable = shutil.which(exe_name)
        if executable and validate_tool_executable(tool, executable):
            return executable

    logger.warning(f"No safe executable found for tool: {tool}")
    return None


def validate_tool_name(tool: str) -> bool:
    """Validate that tool name is from allowed list."""
    tool = normalize_tool_name(tool) or tool
    valid_tools = [
        "claude-code",
        "cursor",
        "gemini-cli",
        "warp",
        "codex",
        "vscode-copilot",
        "copilot-cli",
        "opencode",
        "antigravity",
        "aider",
        "pi",
        "command-code",
        "hermes",
        "cline",
        "roo-code",
        "kilo-code",
        "goose",
        "crush",
        "kimi",
        "qwen",
        "zed",
        "factory-droid",
    ]
    return tool in valid_tools


# Strict allowlist patterns for session IDs.
# UUID format: Claude Code and similar tools
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Alphanumeric/base58 IDs: OpenCode and other tools (8–64 chars)
_ALPHANUM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Claude Code subagent IDs: <parent-uuid>:<agent-name> where agent-name is
# like 'agent-a3e4335'. The colon disambiguates subagents from main sessions
# because the agent file name alone is not globally unique.
_SUBAGENT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[A-Za-z0-9_-]{1,64}$"
)


def validate_session_id(session_id: str, max_length: int = 256) -> bool:
    """Validate session ID format using a strict allowlist approach.

    Accepts IDs that match any of:
    - UUID format (e.g. Claude Code main session)
    - Alphanumeric/base58 (e.g. OpenCode)
    - <UUID>:<agent-name> (Claude Code subagent)
    """
    if not session_id or len(session_id) > max_length:
        return False
    return bool(
        _UUID_RE.match(session_id)
        or _ALPHANUM_RE.match(session_id)
        or _SUBAGENT_RE.match(session_id)
    )


def validate_search_param(param: str, max_length: int = 256) -> bool:
    """Validate search parameters (tool, project, etc)."""
    if not param:
        return True

    if len(param) > max_length:
        return False

    forbidden_chars = [";", "|", "&", "\n", "\r", "\0", "<", ">", '"', "'"]
    return not any(char in param for char in forbidden_chars)


def validate_search_query(query: str, max_length: int = 256) -> bool:
    """Validate a free-text search query.

    Same rules as :func:`validate_search_param`, except that a double quote is
    allowed when the whole query is one quoted phrase: it starts and ends with
    ``"``, has exactly two quotes and non-empty content between them (#197).
    """
    if len(query or "") > max_length:
        return False

    # Only plain spaces may surround the quotes; \n and \r must still be rejected.
    text = (query or "").strip(" ")
    if len(text) >= 2 and text.startswith('"') and text.endswith('"') and text.count('"') == 2:
        return bool(text[1:-1].strip()) and validate_search_param(text[1:-1], max_length)
    return validate_search_param(query, max_length)
