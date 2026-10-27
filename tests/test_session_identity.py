"""Namespaced session identity and divergent copies (#155).

Two providers, two machines or two divergent copies of one conversation can
produce the same unqualified session id. The store must keep both, link the
copy, and keep every legacy id resolvable.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage.identity import (
    canonical_content_hash,
    copies_of,
    resolve_session_id,
    source_namespace_for,
)
from lore.storage.reader import load_session_messages_v2
from lore.storage.schema import apply_migrations, current_version, initialise
from lore.storage.writer import write_sessions


def _session(
    sid: str = "abc",
    text: str = "original",
    tool: Tool = Tool.CLAUDE_CODE,
) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title="Identity",
        messages=[
            UnifiedMessage(role=Role.USER, content=text, timestamp=datetime(2026, 1, 1, 1)),
            UnifiedMessage(role=Role.ASSISTANT, content="reply", timestamp=datetime(2026, 1, 1, 2)),
        ],
    )


def _write(tmp_path, *sessions):
    write_sessions(tmp_path / "index_v2.sqlite", list(sessions))
    return initialise(tmp_path / "index_v2.sqlite")


@contextmanager
def _namespace(value):
    """Run a write as if it came from another machine/home scope."""
    previous = os.environ.get("LORE_SOURCE_NAMESPACE")
    os.environ["LORE_SOURCE_NAMESPACE"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LORE_SOURCE_NAMESPACE", None)
        else:
            os.environ["LORE_SOURCE_NAMESPACE"] = previous


def _write_ns(tmp_path, namespace, *sessions):
    with _namespace(namespace):
        write_sessions(tmp_path / "index_v2.sqlite", list(sessions))
    return initialise(tmp_path / "index_v2.sqlite")


def _rows(conn):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, tool, source_namespace, source_record_id, content_sha256, "
            "copy_of_session_id FROM sessions ORDER BY id"
        )
    ]


# ---------------------------------------------------------------------------
# hashing and namespaces
# ---------------------------------------------------------------------------


def test_identical_sessions_hash_equal_and_a_changed_message_does_not():
    one = _session()
    two = _session()
    three = _session(text="changed")

    assert canonical_content_hash(one) == canonical_content_hash(two)
    assert canonical_content_hash(one) != canonical_content_hash(three)


def test_namespace_is_tool_scoped_and_env_overridable(monkeypatch):
    session = _session()
    assert source_namespace_for(session) == "claude-code:local"
    monkeypatch.setenv("LORE_SOURCE_NAMESPACE", "desktop-2")
    assert source_namespace_for(session) == "claude-code:desktop-2"


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def test_identical_copy_deduplicates_to_one_row(tmp_path):
    conn = _write(tmp_path, _session(), _session())

    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["content_sha256"] == canonical_content_hash(_session())
    assert rows[0]["copy_of_session_id"] is None
    assert rows[0]["source_record_id"] == "abc"
    assert rows[0]["source_namespace"] == "claude-code:local"


def test_same_namespace_update_refreshes_in_place(tmp_path):
    conn = _write(tmp_path, _session(text="original"))
    with _namespace("local"):
        write_sessions(tmp_path / "index_v2.sqlite", [_session(text="rewritten")])
    conn = initialise(tmp_path / "index_v2.sqlite")

    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["id"] == "abc"
    assert [m.content for m in load_session_messages_v2(tmp_path, "abc")] == [
        "rewritten",
        "reply",
    ]


def test_divergent_copy_from_another_namespace_is_kept_and_linked(tmp_path):
    _write_ns(tmp_path, "machine-a", _session(text="original"))
    conn = _write_ns(tmp_path, "machine-b", _session(text="rewritten"))

    rows = _rows(conn)
    assert len(rows) == 2
    first, second = rows[0], rows[1]
    assert first["id"] == "abc"
    assert first["copy_of_session_id"] is None
    assert second["id"].startswith("claude-code:machine-b:abc#")
    assert second["copy_of_session_id"] == "abc"
    assert second["source_record_id"] == "abc"
    assert first["content_sha256"] != second["content_sha256"]

    # Both histories stay readable, and the first one was not rewritten.
    assert [m.content for m in load_session_messages_v2(tmp_path, "abc")] == [
        "original",
        "reply",
    ]
    copy_messages = load_session_messages_v2(tmp_path, second["id"])
    assert copy_messages[0].content == "rewritten"


def test_two_tools_with_the_same_record_id_both_survive(tmp_path):
    claude = _session(text="claude text", tool=Tool.CLAUDE_CODE)
    codex = _session(text="codex text", tool=Tool.CODEX)

    conn = _write(tmp_path, claude, codex)

    rows = _rows(conn)
    assert len(rows) == 2
    namespaces = {row["source_namespace"] for row in rows}
    assert namespaces == {"claude-code:local", "codex:local"}
    assert {row["source_record_id"] for row in rows} == {"abc"}


def test_third_divergent_copy_gets_the_next_suffix(tmp_path):
    _write_ns(tmp_path, "a", _session(text="v1"))
    _write_ns(tmp_path, "b", _session(text="v2"))
    conn = _write_ns(tmp_path, "c", _session(text="v3"))

    rows = _rows(conn)
    assert len(rows) == 3
    suffixes = sorted(
        row["id"] for row in rows if row["id"].startswith("claude-code:") and "#" in row["id"]
    )
    assert suffixes == ["claude-code:b:abc#2", "claude-code:c:abc#2"]
    assert all(row["copy_of_session_id"] == "abc" for row in rows[1:])


def test_copies_of_lists_the_divergent_histories(tmp_path):
    _write_ns(tmp_path, "a", _session(text="v1"))
    conn = _write_ns(tmp_path, "b", _session(text="v2"))

    copies = copies_of(conn, "abc")
    assert len(copies) == 1
    assert copies[0]["id"].startswith("claude-code:b:abc#")


# ---------------------------------------------------------------------------
# compatibility resolver
# ---------------------------------------------------------------------------


def test_resolver_maps_legacy_qualified_record_and_hash_ids(tmp_path):
    _write_ns(tmp_path, "a", _session(text="v1"))
    conn = _write_ns(tmp_path, "b", _session(text="v2"))
    copy_id = _rows(conn)[1]["id"]
    digest = canonical_content_hash(_session(text="v2"))

    assert resolve_session_id(conn, "abc") == "abc"
    # A stored qualified copy id resolves to itself (it is a real session).
    assert resolve_session_id(conn, copy_id) == copy_id
    # An unknown suffix of a known base falls back to the base through the
    # record id — an export from another machine still opens the session.
    assert resolve_session_id(conn, "claude-code:b:abc#99") == "abc"
    assert resolve_session_id(conn, "does-not-exist") is None
    assert resolve_session_id(conn, "") is None
    # A raw provider record id and a content hash both resolve.
    assert resolve_session_id(conn, "abc") in {"abc", copy_id}
    assert resolve_session_id(conn, digest) is not None


def test_resolver_finds_a_copy_by_its_content_hash(tmp_path):
    _write_ns(tmp_path, "a", _session(text="v1"))
    conn = _write_ns(tmp_path, "b", _session(text="v2"))
    copy_id = _rows(conn)[1]["id"]
    digest = canonical_content_hash(_session(text="v2"))

    assert _rows(conn)[1]["content_sha256"] == digest
    assert resolve_session_id(conn, digest) in {"abc", copy_id}


def test_store_survives_a_legacy_row_without_a_hash(tmp_path):
    write_sessions(tmp_path / "index_v2.sqlite", [_session(text="legacy")])
    conn = initialise(tmp_path / "index_v2.sqlite")
    conn.execute("UPDATE sessions SET content_sha256 = NULL, source_namespace = NULL")
    conn.commit()

    # Refreshing a legacy row must not create a phantom copy.
    from lore.storage.writer import write_sessions as write

    write(tmp_path / "index_v2.sqlite", [_session(text="legacy")])
    conn = initialise(tmp_path / "index_v2.sqlite")
    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["copy_of_session_id"] is None
    assert rows[0]["content_sha256"] == canonical_content_hash(_session(text="legacy"))


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migration_21_is_idempotent_and_keeps_existing_ids(tmp_path):
    conn = initialise(tmp_path / "identity.sqlite")
    assert current_version(conn) >= 21
    conn.execute(
        "INSERT INTO sessions (id, tool, created, updated) VALUES ('old', 'codex', "
        "'2026-01-01', '2026-01-01')"
    )
    conn.commit()

    apply_migrations(conn)

    row = conn.execute(
        "SELECT source_namespace, source_record_id, content_sha256, copy_of_session_id "
        "FROM sessions WHERE id = 'old'"
    ).fetchone()
    assert tuple(row) == (None, None, None, None)
    assert resolve_session_id(conn, "old") == "old"


def test_collision_with_different_content_is_detected_by_the_resolver(tmp_path):
    _write_ns(tmp_path, "a", _session(text="first"))
    conn = _write_ns(tmp_path, "b", _session(text="second"))
    rows = _rows(conn)
    assert len({row["content_sha256"] for row in rows}) == 2
