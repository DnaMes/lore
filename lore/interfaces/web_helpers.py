from datetime import datetime
from pathlib import Path
from typing import Optional

from lore.utils.datetime import datetime_in_range, parse_date_bound, to_local_naive

TOOL_STYLES = {
    "claude-code": {"color": "#C084FC", "icon": "C", "name": "Claude"},
    "cursor": {"color": "#3B82F6", "icon": "Cu", "name": "Cursor"},
    "gemini-cli": {"color": "#60A5FA", "icon": "G", "name": "Gemini"},
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
    "zed": {"color": "#84CC16", "icon": "Zd", "name": "Zed"},
    "factory-droid": {"color": "#F97316", "icon": "Fd", "name": "Factory Droid"},
}


def get_style(tool):
    return TOOL_STYLES.get(tool, {"color": "#A1A1AA", "icon": "🤖", "name": tool})


def project_label(project_path: Optional[str]) -> str:
    if not project_path:
        return "Unassigned"
    name = Path(project_path).name
    return name or project_path


def parse_date_param(value: Optional[str], *, end: bool = False) -> Optional[datetime]:
    """Parse a start/end filter value into a naive local boundary (#172).

    Delegates to :func:`lore.utils.datetime.parse_date_bound`, so a date-only
    end value covers the whole selected day and an offset-carrying timestamp is
    converted to the host's local wall clock. Invalid input yields ``None``.
    """
    return parse_date_bound(value, end=end)


def filter_sessions(sessions, tool=None, tag=None, start=None, end=None, user_tags=None):
    """Apply the list filters to index-shaped session dicts.

    ``tag`` matches a session's saved tags (``session_tags``, user-editable,
    #173) as well as the LLM-generated ``keywords`` carried in the index, so a
    bookmarked session is findable by the name the user gave it. ``user_tags``
    is the ``{session_id: [tag, ...]}`` mapping from
    :func:`lore.storage.all_tags_by_session`; a session matches when either
    source contains the tag.
    """
    filtered = []
    for session in sessions:
        if tool and session.get("tool") != tool:
            continue
        if tag:
            normalized = str(tag).strip().lower()
            saved = (user_tags or {}).get(str(session.get("id")), [])
            keywords = session.get("keywords") or []
            if normalized not in [str(item).lower() for item in saved] and tag not in keywords:
                continue
        if start or end:
            created_raw = session.get("created")
            if not created_raw:
                continue
            try:
                # Mixed provider formats: offset-carrying values are converted to
                # the local wall clock the archive stores instead of raising the
                # naive/aware TypeError (#172).
                created = to_local_naive(
                    datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                )
            except ValueError:
                continue
            if not datetime_in_range(created, start, end):
                continue
        filtered.append(session)
    return filtered
