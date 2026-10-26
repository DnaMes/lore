"""Immutable source revisions (#151).

The store must keep the raw input behind a parsed session recoverable after the
provider file is gone, deduplicate unchanged bytes, keep divergent content as a
separate revision, and refuse credential material. Contract:
``docs/superpowers/specs/2026-10-26-source-revision-and-identity-design.md``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import source_revisions
from lore.storage.schema import apply_migrations, current_version, initialise
from lore.storage.source_revisions import (
    artifacts_for_revision,
    capture_artifact,
    capture_session_source,
    capture_source_revision,
    get_source_revision,
    is_credential_source,
    read_source_revision,
    revision_for_session,
    revisions_for_source,
    source_identity_for,
    undecoded_revisions,
)
from lore.storage.writer import write_sessions


def _db(tmp_path):
    return initialise(tmp_path / "index_v2.sqlite")


def _session(source_path: str, sid: str = "s1") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/home/u/proj",
        title="Sample",
        source_path=source_path,
        messages=[
            UnifiedMessage(role=Role.USER, content="hello", timestamp=datetime(2026, 1, 1, 1)),
            UnifiedMessage(role=Role.ASSISTANT, content="hi", timestamp=datetime(2026, 1, 1, 2)),
        ],
    )


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.claude/.credentials.json",
        "/home/u/.claude/auth.json",
        "/home/u/proj/.env",
        "/home/u/proj/.env.production",
        "/home/u/.ssh/id_rsa",
        "/home/u/.ssh/id_ed25519.pub",
        "/home/u/certs/client.pem",
        "/home/u/certs/client.key",
        "/home/u/secrets.json",
        "/home/u/api_key.txt",
        "/home/u/tokens.json",
    ],
)
def test_credential_material_is_never_a_capture_target(path):
    assert is_credential_source(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.claude/projects/p/abc.jsonl",
        "/home/u/.codex/sessions/2026/01/rollout.jsonl",
        "/home/u/.config/Cursor/User/workspaceStorage/x.vscdb",
        "/home/u/.gemini/tmp/session.json",
    ],
)
def test_source_transcripts_are_capture_targets(path):
    assert is_credential_source(path) is False


def test_credential_file_is_not_stored(tmp_path):
    conn = _db(tmp_path)
    secret = tmp_path / "credentials.json"
    secret.write_text('{"token": "sk-live-SUPERSECRET"}', encoding="utf-8")

    result = capture_session_source(conn, tool="claude-code", source_path=secret)

    assert result.status == "excluded"
    assert result.revision_id is None
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
    # And it never leaks into any stored blob.
    for (blob,) in conn.execute("SELECT content FROM source_revisions"):
        assert b"SUPERSECRET" not in blob


# ---------------------------------------------------------------------------
# capture, idempotence, revisions
# ---------------------------------------------------------------------------


def test_capture_is_idempotent_for_unchanged_bytes(tmp_path):
    conn = _db(tmp_path)
    source = tmp_path / "session.jsonl"
    source.write_text('{"a": 1}\n', encoding="utf-8")

    first = capture_session_source(conn, tool="claude-code", source_path=source)
    second = capture_session_source(conn, tool="claude-code", source_path=source)

    assert first.status == "created" and first.created is True
    assert second.status == "reused"
    assert second.revision_id == first.revision_id
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1


def test_identical_bytes_under_a_new_mtime_reuse_the_revision(tmp_path):
    conn = _db(tmp_path)
    revision = capture_source_revision(
        conn,
        tool="codex",
        source_path=tmp_path / "rollout.jsonl",
        content=b'{"type": "message"}\n',
        source_mtime_ns=1_000,
        source_size=21,
    )
    again = capture_source_revision(
        conn,
        tool="codex",
        source_path=tmp_path / "rollout.jsonl",
        content=b'{"type": "message"}\n',
        source_mtime_ns=2_000,
        source_size=21,
        force_checksum=True,
    )

    assert again.revision_id == revision.revision_id
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1


def test_divergent_content_becomes_a_new_revision_and_preserves_the_old(tmp_path):
    conn = _db(tmp_path)
    source = tmp_path / "session.jsonl"
    source.write_text('{"event": "old"}\n', encoding="utf-8")
    first = capture_session_source(conn, tool="claude-code", source_path=source)

    source.write_text('{"event": "new"}\n', encoding="utf-8")
    second = capture_session_source(
        conn, tool="claude-code", source_path=source, parsed_mtime_ns=None
    )

    assert second.revision_id != first.revision_id
    revisions = revisions_for_source(
        conn, source_identity_for("claude-code", str(source.absolute()))
    )
    assert [r["revision_seq"] for r in revisions] == [1, 2]
    assert read_source_revision(conn, first.revision_id) == b'{"event": "old"}\n'
    assert read_source_revision(conn, second.revision_id) == b'{"event": "new"}\n'


def test_raw_bytes_survive_deletion_of_the_source_file(tmp_path):
    conn = _db(tmp_path)
    source = tmp_path / "session.jsonl"
    payload = '{"message": "round trip \\u00e4\\u00f6\\u00fc"}\n'.encode()
    source.write_bytes(payload)

    result = capture_session_source(conn, tool="claude-code", source_path=source)
    source.unlink()

    assert read_source_revision(conn, result.revision_id) == payload
    meta = get_source_revision(conn, result.revision_id)
    assert meta["byte_size"] == len(payload)
    assert meta["content_sha256"]


def test_unknown_and_malformed_decode_status_is_kept(tmp_path):
    conn = _db(tmp_path)
    result = capture_source_revision(
        conn,
        tool="antigravity",
        source_path=tmp_path / "conversation.pb",
        content=b"\x00\x01not-a-known-format",
        decode_status="unknown",
        decode_note="protobuf without a schema",
    )
    meta = get_source_revision(conn, result.revision_id)
    assert meta["decode_status"] == "unknown"
    assert meta["decode_note"] == "protobuf without a schema"
    assert meta["media_type"] == "protobuf"
    assert [row["id"] for row in undecoded_revisions(conn)] == [result.revision_id]


def test_stale_window_is_flagged_not_hidden(tmp_path):
    conn = _db(tmp_path)
    source = tmp_path / "session.jsonl"
    source.write_text('{"event": 1}\n', encoding="utf-8")

    result = capture_session_source(
        conn, tool="claude-code", source_path=source, parsed_mtime_ns=123
    )

    assert result.revision_id is not None
    meta = get_source_revision(conn, result.revision_id)
    assert meta["decode_status"] == "stale"
    assert "parsed mtime_ns=123" in meta["decode_note"]


def test_missing_source_is_reported_as_unavailable(tmp_path):
    conn = _db(tmp_path)
    result = capture_session_source(conn, tool="claude-code", source_path=tmp_path / "gone.jsonl")
    assert result.status == "unavailable"
    assert result.revision_id is None
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0


def test_invalid_decode_status_is_rejected(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ValueError):
        capture_source_revision(
            conn,
            tool="claude-code",
            source_path=tmp_path / "x.jsonl",
            content=b"x",
            decode_status="definitely-not-a-status",
        )


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def test_artifacts_are_captured_per_revision_and_idempotent(tmp_path):
    conn = _db(tmp_path)
    revision = capture_source_revision(
        conn, tool="claude-code", source_path=tmp_path / "s.jsonl", content=b"{}"
    )
    first = capture_artifact(
        conn,
        revision_id=revision.revision_id,
        artifact_key="image-1.png",
        content=b"\x89PNG-bytes",
        media_type="image/png",
        decode_status="unparsed",
        decode_note="image block",
    )
    again = capture_artifact(
        conn,
        revision_id=revision.revision_id,
        artifact_key="image-1.png",
        content=b"\x89PNG-bytes",
        media_type="image/png",
    )
    replaced = capture_artifact(
        conn,
        revision_id=revision.revision_id,
        artifact_key="image-1.png",
        content=b"\x89PNG-other",
        media_type="image/png",
    )

    assert first == again == replaced
    artifacts = artifacts_for_revision(conn, revision.revision_id)
    assert len(artifacts) == 1
    assert artifacts[0]["decode_status"] == "unparsed"
    assert artifacts[0]["byte_size"] == len(b"\x89PNG-other")


def test_artifacts_of_two_revisions_stay_separate(tmp_path):
    conn = _db(tmp_path)
    source = tmp_path / "s.jsonl"
    source.write_text("v1", encoding="utf-8")
    one = capture_session_source(conn, tool="claude-code", source_path=source)
    source.write_text("v2", encoding="utf-8")
    two = capture_session_source(conn, tool="claude-code", source_path=source)

    capture_artifact(conn, revision_id=one.revision_id, artifact_key="a.txt", content=b"one")
    capture_artifact(conn, revision_id=two.revision_id, artifact_key="a.txt", content=b"two")

    assert len(artifacts_for_revision(conn, one.revision_id)) == 1
    assert len(artifacts_for_revision(conn, two.revision_id)) == 1


# ---------------------------------------------------------------------------
# writer integration
# ---------------------------------------------------------------------------


def test_write_sessions_links_the_session_to_its_captured_revision(tmp_path):
    db = tmp_path / "state" / "index_v2.sqlite"
    source = tmp_path / "claude.jsonl"
    source.write_text('{"type": "summary"}\n', encoding="utf-8")
    session = _session(str(source))

    write_sessions(db, [session])

    conn = initialise(db)
    stored = revision_for_session(conn, session.session_id)
    assert stored is not None
    assert stored["tool"] == "claude-code"
    assert read_source_revision(conn, stored["id"]) == source.read_bytes()


def test_write_sessions_without_a_source_path_still_works(tmp_path):
    db = tmp_path / "index_v2.sqlite"
    session = _session("")

    write_sessions(db, [session])

    conn = initialise(db)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT source_revision_id FROM sessions").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0


def test_repeated_sync_does_not_grow_the_archive(tmp_path):
    db = tmp_path / "index_v2.sqlite"
    source = tmp_path / "claude.jsonl"
    source.write_text('{"type": "summary"}\n', encoding="utf-8")
    session = _session(str(source))

    write_sessions(db, [session])
    write_sessions(db, [session])

    conn = initialise(db)
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# migration safety
# ---------------------------------------------------------------------------


def test_migration_19_is_idempotent_and_keeps_legacy_rows_readable(tmp_path):
    db = tmp_path / "index_v2.sqlite"
    conn = initialise(db)
    applied = current_version(conn)
    assert applied >= 19
    conn.execute(
        "INSERT INTO sessions (id, tool, created, updated) VALUES ('legacy', 'codex', "
        "'2026-01-01', '2026-01-01')"
    )
    conn.commit()

    apply_migrations(conn)

    assert current_version(conn) == applied
    row = conn.execute("SELECT source_revision_id FROM sessions WHERE id = 'legacy'").fetchone()
    assert row["source_revision_id"] is None


def test_source_identity_is_stable_and_namespaced(tmp_path):
    identity = source_identity_for("codex", tmp_path / "a.jsonl")
    assert identity.startswith("codex:local:")
    assert source_identity_for("codex", tmp_path / "a.jsonl", "machine-b").startswith(
        "codex:machine-b:"
    )
    assert identity == source_identity_for("codex", tmp_path / "a.jsonl")
    assert source_revisions.media_type_for("x.JSONL") == "jsonl"
