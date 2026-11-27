import json
import sqlite3

from annuli.extractors.opencode import OpenCodeExtractor


def test_opencode_sqlite_batches_parts_per_session(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "share" / "opencode").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        )
        """
    )

    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg-user",
            "ses-1",
            1,
            1,
            json.dumps({"role": "user", "time": {"created": 1}}),
        ),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg-assistant",
            "ses-1",
            2,
            2,
            json.dumps(
                {
                    "role": "assistant",
                    "time": {"created": 2},
                    "model": {"providerID": "openai", "modelID": "gpt-test"},
                }
            ),
        ),
    )

    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "prt-user-1",
            "msg-user",
            "ses-1",
            1,
            1,
            json.dumps({"type": "text", "text": "hello from user"}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "prt-assistant-1",
            "msg-assistant",
            "ses-1",
            2,
            2,
            json.dumps({"type": "reasoning", "text": "thinking"}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "prt-assistant-2",
            "msg-assistant",
            "ses-1",
            3,
            3,
            json.dumps({"type": "text", "text": "assistant reply"}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "prt-assistant-3",
            "msg-assistant",
            "ses-1",
            4,
            4,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "read",
                    "callID": "call-1",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": "/tmp/demo.py"},
                        "output": "ok",
                    },
                }
            ),
        ),
    )
    conn.commit()

    extractor = OpenCodeExtractor(force_full=True)
    messages = extractor._load_messages_from_sqlite(conn, "ses-1")

    assert len(messages) == 2
    assert messages[0].content == "hello from user"
    assert messages[1].reasoning == "thinking"
    assert "assistant reply" in messages[1].content
    assert "[Tool: read]" in messages[1].content
    assert messages[1].tool_calls == [
        {
            "id": "call-1",
            "tool": "read",
            "status": "completed",
            "input": {"filePath": "/tmp/demo.py"},
            "output": "ok",
            "truncated": False,
        }
    ]
    assert messages[1].model == "openai/gpt-test"

    conn.close()
