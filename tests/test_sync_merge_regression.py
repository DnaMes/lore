from datetime import datetime

from annuli.core.models import Tool, UnifiedSession
from annuli_cli import _merge_sessions_with_existing_index


def _session(session_id: str, tool: Tool) -> UnifiedSession:
    now = datetime(2026, 3, 4, 12, 0, 0)
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=[],
    )


def test_merge_sessions_keeps_existing_index_entries():
    extracted = [_session("new-1", Tool.GEMINI_CLI)]
    existing_index = [
        {
            "id": "old-1",
            "tool": "codex",
            "created": "2026-03-01T10:00:00",
            "updated": "2026-03-01T10:10:00",
            "project": "/repo/old",
            "thread_id": "thread-old",
            "title": "Old Session",
        }
    ]

    merged = _merge_sessions_with_existing_index(extracted, existing_index)
    ids = {session.session_id for session in merged}

    assert ids == {"new-1", "old-1"}
