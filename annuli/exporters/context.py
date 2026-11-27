from datetime import datetime
from typing import List

from ..core.models import UnifiedSession
from ..utils.datetime import make_naive


def format_context(sessions: List[UnifiedSession], max_messages: int = 15) -> str:
    """Format sessions as context for new tool."""
    if not sessions:
        return "# Starting new session\n\nNo previous context available."

    lines = []
    lines.append("# Continue AI Session")
    lines.append(f"\n**Project:** {sessions[0].project_path if sessions else 'Unknown'}")
    lines.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("\n---\n")

    # Get recent messages across all sessions
    all_messages = []
    for session in sessions:
        for msg in session.messages:
            if msg.content and msg.content.strip():
                # Normalize tool name
                tool_name = (
                    session.tool.value if hasattr(session.tool, "value") else str(session.tool)
                )

                all_messages.append(
                    {
                        "tool": tool_name,
                        "timestamp": msg.timestamp,
                        "role": msg.role.value,
                        "content": msg.content,
                    }
                )

    # Sort by timestamp and take last N
    all_messages.sort(key=lambda m: make_naive(m["timestamp"]) or datetime.min)
    recent = all_messages[-max_messages:]

    if not recent:
        return "# Starting new session\n\nNo previous messages found."

    lines.append("## Recent conversation:\n")

    for msg in recent:
        role_icon = "👤" if msg["role"] == "user" else "🤖"
        tool = msg["tool"].replace("Tool.", "").replace("_", "-").lower()

        content = msg["content"]
        # Truncate extremely long messages to save context window
        if len(content) > 2000:
            content = content[:2000] + "\n\n[... truncated ...]"

        lines.append(f"\n### {role_icon} {msg['role'].title()} `[{tool}]`\n")
        lines.append(f"{content}\n")

    lines.append("\n---\n")
    lines.append("## Continue from here\n")

    return "\n".join(lines)
