#!/usr/bin/env python3
"""
AI Session Manager - Seamless switching between AI tools with full context

Usage:
    lore-session list                          # Show all sessions
    lore-session switch gemini                 # Switch to Gemini with current context
    lore-session switch codex                  # Switch to Codex with current context
    lore-session continue                      # Auto-select best tool based on rate limits
"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from lore.utils.paths import make_thread_id

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lore.exporters.context import format_context
from lore.extractors.factory import get_all_extractors
from lore.utils.console import make_console_safe
from lore.utils.datetime import make_naive
from lore.utils.paths import get_current_project
from lore.utils.security import get_safe_executable
from lore.utils.tooling import normalize_tool_name, to_session_switch_tool

CONTEXT_FILE = Path("/tmp/ai-session-context.md")


def get_project_sessions(project_path: str):
    """Get all sessions for current project using lore package."""
    extractors = get_all_extractors()
    sessions = []
    thread_id = make_thread_id(project_path=project_path)

    for extractor in extractors:
        if not extractor.is_available():
            continue

        try:
            for session in extractor.extract_sessions():
                # Heuristic: Match project path or if project path is in session path
                if session.project_path and project_path in str(session.project_path):
                    sessions.append(session)
                elif session.project_path and str(session.project_path) in project_path:
                    # Containment is checked in both directions on purpose: a
                    # session recorded in a subdirectory belongs to this project,
                    # and so does one recorded in a parent directory (a monorepo
                    # root, or the session started before the user cd'd deeper).
                    # Substring matching is deliberate over path-aware parenting —
                    # it is cheap and over-matching here only widens the candidate
                    # list the user picks from.
                    sessions.append(session)
                elif thread_id and session.thread_id and session.thread_id == thread_id:
                    sessions.append(session)
        except (OSError, AttributeError, RuntimeError):
            continue

    # Sort by creation time
    sessions.sort(key=lambda s: make_naive(s.created_at))
    return sessions


def switch_to_tool(tool: str, context: str):
    """Switch to specified AI tool with context."""

    normalized_tool = normalize_tool_name(tool) or tool
    switch_tool = to_session_switch_tool(normalized_tool) or tool

    # Save context
    try:
        with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
            f.write(context)
    except Exception as e:
        print(f"❌ Error saving context: {e}")
        return False

    commands = {
        "gemini": ["gemini", f"$(cat {CONTEXT_FILE})"],
        "codex": ["codex", f"@{CONTEXT_FILE}"],
        "cursor": None,  # Manual
        "vscode": None,  # Manual
        "claude": ["claude", "--continue"],
    }

    if switch_tool not in commands:
        print(f"❌ Unknown tool: {tool}")
        print(f"Available: {', '.join(commands.keys())}")
        return False

    cmd = commands[switch_tool]

    if cmd is None:
        # Manual tools
        print(f"✓ Context saved to: {CONTEXT_FILE}")
        print(f"\nTo continue in {tool.title()}:")
        if tool == "cursor":
            print("  1. Open Cursor")
            print("  2. Press Cmd/Ctrl+I")
            print(f"  3. Paste content from {CONTEXT_FILE}")
        elif tool == "vscode":
            print("  1. Open VSCode")
            print("  2. Press Ctrl+Shift+I (Copilot Chat)")
            print(f"  3. Type: @{CONTEXT_FILE}")
        return True

    print(f"\n🚀 Switching to {tool}...\n")
    print(f"Context: {CONTEXT_FILE}")
    print(f"Command: {' '.join(cmd)}\n")

    try:
        executable = get_safe_executable(switch_tool)
        if not executable:
            print(f"❌ {switch_tool} not found or not safe. Install it first.")
            return False

        if switch_tool == "gemini":
            subprocess.run([executable, context], shell=False, timeout=3600)
        elif switch_tool == "codex":
            subprocess.run([executable, f"@{CONTEXT_FILE}"], shell=False, timeout=3600)
        elif switch_tool == "claude":
            subprocess.run([executable, "--continue"], shell=False, timeout=3600)
    except FileNotFoundError:
        print(f"❌ {tool} not found. Install it first.")
        return False
    except subprocess.TimeoutExpired:
        print(f"\n⏱️  {tool} session timed out after 1 hour")
        return True
    except KeyboardInterrupt:
        print("\n\n✓ Session ended")
        return True

    return True


def auto_switch():
    """Auto-select best tool based on rate limits and availability."""

    # Check Claude Code availability/rate limit
    claude_available = True
    if shutil.which("claude"):
        try:
            pass
        except (subprocess.CalledProcessError, OSError):
            pass
    else:
        claude_available = False

    # Priority order: Gemini (large context) > Codex (free/cheap) > Claude
    # If user asks to continue, they likely want to switch AWAY from current tool if it failed.
    # But if starting fresh, Claude is good.

    tools = ["gemini", "codex", "claude"]

    print("🤖 Auto-selecting AI tool...\n")

    for tool in tools:
        if (
            shutil.which(tool)
            or shutil.which(f"{tool}-cli")
            or (tool == "gemini" and shutil.which("gemini-cli"))
        ):
            print(f"✓ Selected: {tool}")
            return tool

    print("❌ No AI tools available")
    return None


def get_thread_sessions(thread_id: str):
    extractors = get_all_extractors()
    sessions = []

    for extractor in extractors:
        if not extractor.is_available():
            continue

        try:
            for session in extractor.extract_sessions():
                if session.thread_id and session.thread_id == thread_id:
                    sessions.append(session)
        except (OSError, AttributeError, RuntimeError):
            continue

    sessions.sort(key=lambda s: make_naive(s.created_at))
    return sessions


def list_sessions_cmd():
    """List all sessions for current project."""
    project = get_current_project()
    sessions = get_project_sessions(project)

    print(f"\n{'=' * 60}")
    print(f"AI Sessions for: {project}")
    print(f"{'=' * 60}\n")

    if not sessions:
        print("No sessions found for this project.")
        print("\nTip: Run 'lore export --all' to rebuild index")
        return

    # Group by tool
    by_tool = {}
    for s in sessions:
        tool = s.tool.value if hasattr(s.tool, "value") else str(s.tool)
        tool = tool.replace("Tool.", "").replace("_", "-").lower()
        if tool not in by_tool:
            by_tool[tool] = []
        by_tool[tool].append(s)

    for tool, tool_sessions in sorted(by_tool.items()):
        print(f"📝 {tool.upper()}: {len(tool_sessions)} sessions")

        # Show latest 3
        for session in tool_sessions[-3:]:
            created = session.created_at.strftime("%Y-%m-%d %H:%M") if session.created_at else "???"
            msgs = len(session.messages)
            title = session.title or session.session_id[:12]
            print(f"   • {created} | {msgs:3d} msgs | {title}")

        if len(tool_sessions) > 3:
            print(f"   ... and {len(tool_sessions) - 3} more")
        print()

    print("─" * 60)
    print(f"Total: {len(sessions)} sessions\n")


def main():
    make_console_safe()
    parser = argparse.ArgumentParser(
        description="AI Session Manager - Seamless tool switching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # list command
    subparsers.add_parser("list", help="List all sessions for current project")

    # switch command
    switch_parser = subparsers.add_parser("switch", help="Switch to another AI tool")
    switch_parser.add_argument(
        "tool",
        help="AI tool to switch to",
    )
    switch_parser.add_argument(
        "--messages",
        type=int,
        default=15,
        help="Number of recent messages (default: 15)",
    )
    switch_parser.add_argument("--thread-id", help="Continue a specific thread id")

    # continue command
    subparsers.add_parser("continue", help="Auto-select best available tool")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "list":
        list_sessions_cmd()

    elif args.command == "switch":
        project = get_current_project()
        if args.thread_id:
            sessions = get_thread_sessions(args.thread_id)
        else:
            sessions = get_project_sessions(project)

        if not sessions:
            print(f"\n⚠️  No previous sessions found for: {project}")
            print("Starting fresh session...\n")
            context = f"# New Session\n\n**Project:** {project}\n**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        else:
            print(f"\n🔍 Loading context from {len(sessions)} previous sessions...")
            context = format_context(sessions, args.messages)

        switch_to_tool(args.tool, context)

    elif args.command == "continue":
        tool = auto_switch()
        if tool:
            project = get_current_project()
            sessions = get_project_sessions(project)
            context = format_context(sessions, 15)
            switch_to_tool(tool, context)


if __name__ == "__main__":
    main()
