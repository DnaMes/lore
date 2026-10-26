"""Canonical message envelope round trips (#154).

What an extractor knows beyond the flattened text — native ids, ordered content
blocks, tool calls with arguments and results, a status, provider fields nobody
modelled — must survive a write/read cycle through SQLite, including unknown
fields. Legacy rows stay readable and must never claim to be complete.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import envelope
from lore.storage.reader import load_session_messages_v2
from lore.storage.schema import apply_migrations, current_version, initialise
from lore.storage.writer import write_sessions


def _session(messages: list[UnifiedMessage], sid: str = "s-envelope") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title="Envelope",
        messages=messages,
    )


def _write(tmp_path, session: UnifiedSession):
    output_dir = tmp_path / "state"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_sessions(output_dir / "index_v2.sqlite", [session])
    return output_dir


def _conn(tmp_path):
    return initialise(tmp_path / "state" / "index_v2.sqlite")


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


def test_structured_message_round_trips_without_information_loss(tmp_path):
    blocks = [
        {"type": "text", "text": "Let me look."},
        {"type": "thinking", "thinking": "The file is large."},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "Read",
            "input": {"path": "/home/u/proj/app.py", "lines": [1, 40]},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": "def main():\n    pass\n",
            "is_error": False,
        },
        {"type": "image", "source": {"media_type": "image/png", "data": "iVBORw0KGgo="}},
        {"type": "unknown_provider_block", "payload": {"nested": [1, 2, 3]}},
    ]
    tool_calls = [
        {
            "id": "call-1",
            "tool": "Read",
            "input": {"path": "/home/u/proj/app.py"},
            "output": "def main():\n    pass\n",
            "status": "ok",
            "duration_ms": 12,
            "is_error": False,
            "truncated": False,
            "caller": {"type": "assistant"},
        }
    ]
    extensions = {
        "envelope": 1,
        "claude": {"usage": {"input_tokens": 12, "output_tokens": 34}, "request_id": "req-9"},
        "unknown_field": {"kept": True},
    }
    message = UnifiedMessage(
        role=Role.ASSISTANT,
        content="Let me look.",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        message_id="msg-native-1",
        model="claude-sonnet-4",
        tokens={"input": 12, "output": 34},
        reasoning="The file is large.",
        status="ok",
    )
    message.content_blocks = blocks
    message.tool_calls = tool_calls
    message.extensions = extensions

    output_dir = _write(tmp_path, _session([message]))
    loaded = load_session_messages_v2(output_dir, "s-envelope", include_reasoning=True)

    assert loaded is not None and len(loaded) == 1
    got = loaded[0]
    assert got.message_id == "msg-native-1"
    assert got.model == "claude-sonnet-4"
    assert got.tokens == {"input": 12, "output": 34}
    assert got.reasoning == "The file is large."
    assert got.status == "ok"
    assert got.envelope_version == envelope.ENVELOPE_VERSION
    assert got.content_blocks == blocks
    assert [block["type"] for block in got.content_blocks] == [b["type"] for b in blocks]
    assert got.tool_calls == tool_calls
    assert got.extensions == extensions


def test_failed_tool_call_status_survives(tmp_path):
    call = {
        "id": "call-err",
        "tool": "Bash",
        "input": {"command": "false"},
        "output": "",
        "error": "exit status 1",
        "status": "failed",
    }
    message = UnifiedMessage(
        role=Role.ASSISTANT,
        content="That failed.",
        timestamp=datetime(2026, 1, 1),
        status="failed",
    )
    message.tool_calls = [call]

    loaded = load_session_messages_v2(_write(tmp_path, _session([message])), "s-envelope")

    assert loaded[0].tool_calls == [call]
    assert loaded[0].status == "failed"


def test_null_and_empty_stay_distinguishable(tmp_path):
    empty = UnifiedMessage(
        role=Role.USER,
        content="hello",
        timestamp=datetime(2026, 1, 1),
    )
    empty.content_blocks = []
    empty.extensions = {}

    loaded = load_session_messages_v2(_write(tmp_path, _session([empty])), "s-envelope")

    got = loaded[0]
    assert got.content_blocks == []
    assert got.tool_calls == []
    assert got.extensions == {}


def test_missing_envelope_values_stay_none(tmp_path):
    plain = UnifiedMessage(
        role=Role.USER,
        content="hello",
        timestamp=datetime(2026, 1, 1),
    )

    loaded = load_session_messages_v2(_write(tmp_path, _session([plain])), "s-envelope")

    got = loaded[0]
    assert got.content_blocks is None
    assert got.extensions is None
    assert got.message_id is None
    assert got.status == "unknown"


# ---------------------------------------------------------------------------
# oversized values
# ---------------------------------------------------------------------------


def test_oversized_field_is_stored_as_a_labelled_stub_not_dropped(tmp_path):
    huge_output = "x" * (envelope.MAX_ENVELOPE_CHARS + 1000)
    message = UnifiedMessage(
        role=Role.ASSISTANT,
        content="done",
        timestamp=datetime(2026, 1, 1),
        status="ok",
    )
    message.tool_calls = [
        {"id": "c1", "tool": "Read", "input": {}, "output": huge_output, "status": "ok"}
    ]

    output_dir = _write(tmp_path, _session([message]))
    loaded = load_session_messages_v2(output_dir, "s-envelope")

    calls = loaded[0].tool_calls
    assert len(calls) == 1
    # The list keeps its shape; only the oversized value became a stub.
    assert calls[0]["id"] == "c1"
    stub = calls[0]["output"]
    assert stub["_truncated"] is True
    assert stub["original_length"] == len(huge_output)
    assert len(stub["sha256"]) == 64
    assert stub["sha256"] == envelope._sha256(huge_output)
    assert loaded[0].status == "ok"


def test_truncation_does_not_affect_other_envelope_fields(tmp_path):
    message = UnifiedMessage(
        role=Role.ASSISTANT,
        content="done",
        timestamp=datetime(2026, 1, 1),
        message_id="native-7",
    )
    message.content_blocks = [{"type": "text", "text": "y" * (envelope.MAX_ENVELOPE_CHARS + 5)}]
    message.extensions = {"provider": {"kept": "yes"}}

    loaded = load_session_messages_v2(_write(tmp_path, _session([message])), "s-envelope")

    got = loaded[0]
    assert got.message_id == "native-7"
    assert got.extensions == {"provider": {"kept": "yes"}}
    assert envelope.is_truncation_stub(got.content_blocks[0]["text"]) is True


# ---------------------------------------------------------------------------
# legacy and damaged rows
# ---------------------------------------------------------------------------


def test_legacy_row_stays_readable_and_reports_version_zero(tmp_path):
    output_dir = _write(
        tmp_path,
        _session(
            [UnifiedMessage(role=Role.USER, content="old row", timestamp=datetime(2026, 1, 1))]
        ),
    )
    conn = _conn(tmp_path)
    conn.execute(
        "UPDATE messages SET native_id = NULL, content_blocks_json = NULL, "
        "tool_calls_json = NULL, status = NULL, extensions_json = NULL, envelope_version = 0"
    )
    conn.commit()

    loaded = load_session_messages_v2(output_dir, "s-envelope")

    got = loaded[0]
    assert got.content == "old row"
    assert got.envelope_version == 0
    assert got.content_blocks is None
    assert got.tool_calls == []


def test_damaged_envelope_json_does_not_break_the_reader(tmp_path):
    output_dir = _write(
        tmp_path,
        _session([UnifiedMessage(role=Role.USER, content="body", timestamp=datetime(2026, 1, 1))]),
    )
    conn = _conn(tmp_path)
    conn.execute(
        "UPDATE messages SET content_blocks_json = '{not json', tool_calls_json = 'null', "
        "extensions_json = '[1,2]', status = 'not-a-status'"
    )
    conn.commit()

    loaded = load_session_messages_v2(output_dir, "s-envelope")

    got = loaded[0]
    assert got.content == "body"
    assert got.content_blocks is None
    assert got.tool_calls == []
    assert got.extensions is None
    assert got.status == "unknown"


def test_unknown_status_is_normalised_to_unknown():
    message = UnifiedMessage(
        role=Role.USER, content="x", timestamp=datetime(2026, 1, 1), status="weird"
    )
    assert envelope.envelope_for(message)["status"] == "unknown"
    assert envelope.normalise_status("FAILED") == "failed"


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migration_20_is_idempotent_and_keeps_legacy_messages_readable(tmp_path):
    conn = initialise(tmp_path / "index_v2.sqlite")
    assert current_version(conn) >= 20
    conn.execute(
        "INSERT INTO sessions (id, tool, created, updated) VALUES ('legacy', 'codex', "
        "'2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO messages (session_id, seq, role, content) VALUES ('legacy', 0, 'user', 'hi')"
    )
    conn.commit()

    apply_migrations(conn)

    assert current_version(conn) == current_version(conn)
    row = conn.execute(
        "SELECT envelope_version, native_id FROM messages WHERE session_id = 'legacy'"
    ).fetchone()
    assert row["envelope_version"] == 0
    assert row["native_id"] is None


@pytest.mark.parametrize("value", [None, {}, [], {"a": 1}, [1, "two", {"three": 3}]])
def test_dump_and_load_are_lossless(value):
    dumped = envelope.dump_envelope_value(value, "field")
    if value is None:
        assert dumped is None
    else:
        assert envelope.load_envelope_value(dumped) == value
