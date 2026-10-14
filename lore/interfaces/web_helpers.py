from datetime import datetime
from pathlib import Path
from typing import Optional

TOOL_STYLES = {
    "claude-code": {"color": "#C084FC", "icon": "C", "name": "Claude"},
    "cursor": {"color": "#3B82F6", "icon": "Cu", "name": "Cursor"},
    "gemini-cli": {"color": "#60A5FA", "icon": "G", "name": "Gemini"},
    "warp": {"color": "#2DD4BF", "icon": "W", "name": "Warp"},
    "codex": {"color": "#22C55E", "icon": "X", "name": "Codex"},
    "vscode-copilot": {"color": "#0ea5e9", "icon": "Co", "name": "VSCode Copilot"},
    "copilot-cli": {"color": "#0284c7", "icon": "Gh", "name": "Copilot CLI"},
    "opencode": {"color": "#F97316", "icon": "O", "name": "OpenCode"},
    "antigravity": {"color": "#8B5CF6", "icon": "A", "name": "Antigravity"},
    "aider": {"color": "#14B8A6", "icon": "Ai", "name": "Aider"},
    "pi": {"color": "#EC4899", "icon": "Pi", "name": "Pi"},
    "command-code": {"color": "#6366F1", "icon": "Cc", "name": "Command Code"},
    "hermes": {"color": "#14B8A6", "icon": "He", "name": "Hermes"},
    "cline": {"color": "#F59E0B", "icon": "Cl", "name": "Cline"},
    "roo-code": {"color": "#8B5CF6", "icon": "Ro", "name": "Roo Code"},
    "kilo-code": {"color": "#10B981", "icon": "Ki", "name": "Kilo Code"},
    "goose": {"color": "#EAB308", "icon": "Go", "name": "Goose"},
    "crush": {"color": "#F472B6", "icon": "Cr", "name": "Crush"},
    "kimi": {"color": "#38BDF8", "icon": "Ki", "name": "Kimi"},
    "qwen": {"color": "#A78BFA", "icon": "Qw", "name": "Qwen Code"},
}


def get_style(tool):
    return TOOL_STYLES.get(tool, {"color": "#A1A1AA", "icon": "🤖", "name": tool})


def project_label(project_path: Optional[str]) -> str:
    if not project_path:
        return "Unassigned"
    name = Path(project_path).name
    return name or project_path


def parse_date_param(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def filter_sessions(sessions, tool=None, tag=None, start=None, end=None):
    filtered = []
    for session in sessions:
        if tool and session.get("tool") != tool:
            continue
        if tag:
            keywords = session.get("keywords") or []
            if tag not in keywords:
                continue
        if start or end:
            created_raw = session.get("created")
            if not created_raw:
                continue
            try:
                created = datetime.fromisoformat(created_raw)
            except ValueError:
                continue
            if start and created < start:
                continue
            if end and created > end:
                continue
        filtered.append(session)
    return filtered
