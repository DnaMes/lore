"""Tests for annuli/extractors/cursor.py.

Cursor stores chat history in a SQLite database at
``~/.config/Cursor/User/globalStorage/state.vscdb``. The ``cursorDiskKV``
table holds ``composerData:<id>`` rows (one per conversation) plus
``bubbleId:<composer>:<bubble>`` rows for the "linked bubbles" format and a
single ``workbench.panel.aichat.view.aichat.chatdata`` row for the sidebar
tabs. These tests build a real SQLite DB on disk under a temp HOME.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from annuli.core.models import Role, Tool
from annuli.extractors.cursor import CursorExtractor


def _db_path(home: Path) -> Path:
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _make_db(home: Path, rows: dict[str, str]) -> Path:
    """Create the Cursor state.vscdb with a cursorDiskKV table populated."""
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in rows.items():
            conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_not_available_when_no_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CursorExtractor().is_available() is False


def test_available_when_db_exists(monkeypatch, tmp_path):
    _make_db(tmp_path, {})
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CursorExtractor().is_available() is True


def test_extract_sessions_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list(CursorExtractor().extract_sessions()) == []


def test_tool_property_is_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CursorExtractor().tool is Tool.CURSOR


# ---------------------------------------------------------------------------
# Old "conversation" array format
# ---------------------------------------------------------------------------


def test_parse_conversation_array_format(monkeypatch, tmp_path):
    composer = {
        "name": "Implement quicksort",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "conversation": [
            {"type": 1, "text": "Write a quicksort", "timestamp": 1_700_000_000_000},
            {"type": 2, "text": "Here is quicksort.", "timestamp": 1_700_000_050_000},
        ],
    }
    _make_db(tmp_path, {"composerData:conv-001": json.dumps(composer)})
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool is Tool.CURSOR
    assert s.session_id == "conv-001"
    assert s.title == "Implement quicksort"
    assert s.message_count == 2
    assert s.messages[0].role is Role.USER
    assert s.messages[1].role is Role.ASSISTANT
    assert s.messages[0].content == "Write a quicksort"
    assert s.messages[1].content == "Here is quicksort."


def test_conversation_skips_empty_text(monkeypatch, tmp_path):
    composer = {
        "name": "Partly empty",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "conversation": [
            {"type": 1, "text": "real prompt"},
            {"type": 2, "text": ""},
        ],
    }
    _make_db(tmp_path, {"composerData:conv-empty": json.dumps(composer)})
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    # Empty assistant text dropped, only the user prompt survives.
    assert sessions[0].message_count == 1
    assert sessions[0].messages[0].content == "real prompt"


# ---------------------------------------------------------------------------
# New "fullConversationHeadersOnly" linked-bubbles format
# ---------------------------------------------------------------------------


def test_parse_linked_bubbles_format(monkeypatch, tmp_path):
    composer_id = "conv-bubbles"
    composer = {
        "name": "Bubble chat",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1},
            {"bubbleId": "b2", "type": 2},
        ],
    }
    rows = {
        f"composerData:{composer_id}": json.dumps(composer),
        f"bubbleId:{composer_id}:b1": json.dumps({"text": "user bubble text"}),
        f"bubbleId:{composer_id}:b2": json.dumps({"text": "assistant bubble text"}),
    }
    _make_db(tmp_path, rows)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.message_count == 2
    assert s.messages[0].role is Role.USER
    assert s.messages[0].content == "user bubble text"
    assert s.messages[1].role is Role.ASSISTANT
    assert s.messages[1].content == "assistant bubble text"


def test_linked_bubbles_missing_bubble_row_skipped(monkeypatch, tmp_path):
    """A header pointing at a missing bubble row must not crash."""
    composer_id = "conv-missing"
    composer = {
        "name": "Half-missing",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1},
            {"bubbleId": "b2", "type": 2},
        ],
    }
    rows = {
        f"composerData:{composer_id}": json.dumps(composer),
        f"bubbleId:{composer_id}:b1": json.dumps({"text": "only this one exists"}),
    }
    _make_db(tmp_path, rows)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].message_count == 1
    assert sessions[0].messages[0].content == "only this one exists"


def test_linked_bubbles_header_without_bubble_id_skipped(monkeypatch, tmp_path):
    composer_id = "conv-noheaderid"
    composer = {
        "name": "No bubble id",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "fullConversationHeadersOnly": [
            {"type": 1},
            {"bubbleId": "b2", "type": 1},
        ],
    }
    rows = {
        f"composerData:{composer_id}": json.dumps(composer),
        f"bubbleId:{composer_id}:b2": json.dumps({"text": "valid prompt"}),
    }
    _make_db(tmp_path, rows)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].message_count == 1


# ---------------------------------------------------------------------------
# Single-prompt "text" fallback
# ---------------------------------------------------------------------------


def test_parse_single_text_prompt_fallback(monkeypatch, tmp_path):
    composer = {
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_000_000,
        "text": "a standalone prompt with no conversation array",
    }
    _make_db(tmp_path, {"composerData:conv-text": json.dumps(composer)})
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.message_count == 1
    assert s.messages[0].role is Role.USER
    assert "standalone prompt" in s.messages[0].content


# ---------------------------------------------------------------------------
# Sidebar tabs format
# ---------------------------------------------------------------------------


def test_parse_sidebar_tabs(monkeypatch, tmp_path):
    chatdata = {
        "tabs": [
            {
                "tabId": "tab-1",
                "chatTitle": "Sidebar chat one",
                "timestamp": 1_700_000_000_000,
                "bubbles": [
                    {"type": "user", "text": "sidebar question"},
                    {"type": "ai", "text": "sidebar answer"},
                ],
            }
        ]
    }
    rows = {
        "workbench.panel.aichat.view.aichat.chatdata": json.dumps(chatdata),
    }
    _make_db(tmp_path, rows)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sidebar-tab-1"
    assert s.title == "Sidebar chat one"
    assert s.message_count == 2
    assert s.messages[0].role is Role.USER
    assert s.messages[1].role is Role.ASSISTANT


def test_sidebar_tab_richtext_fallback(monkeypatch, tmp_path):
    chatdata = {
        "tabs": [
            {
                "tabId": "tab-rt",
                "chatTitle": "Rich text tab",
                "timestamp": 1_700_000_000_000,
                "bubbles": [
                    {"type": "user", "richText": "from richText field"},
                ],
            }
        ]
    }
    _make_db(
        tmp_path,
        {"workbench.panel.aichat.view.aichat.chatdata": json.dumps(chatdata)},
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].messages[0].content == "from richText field"


def test_sidebar_tab_without_messages_dropped(monkeypatch, tmp_path):
    chatdata = {
        "tabs": [
            {
                "tabId": "empty-tab",
                "chatTitle": "Empty",
                "timestamp": 1_700_000_000_000,
                "bubbles": [],
            }
        ]
    }
    _make_db(
        tmp_path,
        {"workbench.panel.aichat.view.aichat.chatdata": json.dumps(chatdata)},
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    assert list(CursorExtractor().extract_sessions()) == []


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_corrupt_composer_json_skipped(monkeypatch, tmp_path):
    good = {
        "name": "Good one",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "conversation": [{"type": 1, "text": "valid prompt"}],
    }
    rows = {
        "composerData:bad": "this is not json {{{",
        "composerData:good": json.dumps(good),
    }
    _make_db(tmp_path, rows)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "good"


def test_extract_multiple_composers(monkeypatch, tmp_path):
    rows = {}
    for i in range(3):
        rows[f"composerData:conv-{i}"] = json.dumps(
            {
                "name": f"Chat {i}",
                "createdAt": 1_700_000_000_000,
                "lastUpdatedAt": 1_700_000_100_000,
                "conversation": [{"type": 1, "text": f"prompt {i}"}],
            }
        )
    _make_db(tmp_path, rows)
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 3
    assert {s.session_id for s in sessions} == {"conv-0", "conv-1", "conv-2"}


def test_missing_cursordiskkv_table_does_not_crash(monkeypatch, tmp_path):
    """A DB file without the expected table must yield no sessions, no crash."""
    path = _db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(tmp_path))

    assert list(CursorExtractor().extract_sessions()) == []


def test_hex_encoded_blob_decoded(monkeypatch, tmp_path):
    """Cursor sometimes stores values as hex-encoded UTF-8 strings."""
    composer = {
        "name": "Hex chat",
        "createdAt": 1_700_000_000_000,
        "lastUpdatedAt": 1_700_000_100_000,
        "conversation": [{"type": 1, "text": "hex encoded prompt"}],
    }
    hex_value = json.dumps(composer).encode("utf-8").hex()
    _make_db(tmp_path, {"composerData:conv-hex": hex_value})
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(CursorExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].messages[0].content == "hex encoded prompt"
