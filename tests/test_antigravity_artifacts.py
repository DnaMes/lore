"""Antigravity undecoded artifacts (#158).

Antigravity's protobuf conversations cannot be decoded by this parser. They must
be preserved as raw evidence (a source revision with a non-``parsed`` decode
status), counted as unsupported, and never reported as a parsed session.
"""

from __future__ import annotations

import json
from datetime import datetime

from lore.extractors.antigravity import AntigravityExtractor
from lore.services.extraction import capture_source_artifacts
from lore.storage.schema import initialise
from lore.storage.source_health import source_health_summary
from lore.storage.source_revisions import (
    read_source_revision,
    revisions_for_source,
    source_identity_for,
)
from lore.storage.writer import write_sessions


def _brain(root, session_id: str = "conv-1"):
    brain = root / "brain" / session_id
    logs = brain / ".system_generated" / "logs"
    conversations = brain / ".system_generated" / "conversations"
    logs.mkdir(parents=True, exist_ok=True)
    conversations.mkdir(parents=True, exist_ok=True)
    return brain, logs, conversations


def _extractor(root) -> AntigravityExtractor:
    extractor = AntigravityExtractor()
    extractor.base_paths = [root]
    return extractor


def test_only_protobuf_tree_is_not_available(tmp_path):
    brain, _, conversations = _brain(tmp_path)
    (conversations / "0001.pb").write_bytes(b"\x08\x01undecoded")

    extractor = _extractor(tmp_path)

    assert extractor.is_available() is False
    assert extractor.has_source_data() is True


def test_source_artifacts_yields_the_raw_protobuf_bytes(tmp_path):
    brain, _, conversations = _brain(tmp_path)
    blob = conversations / "0001.pb"
    blob.write_bytes(b"\x08\x01conversation")

    artifacts = list(_extractor(tmp_path).source_artifacts())

    assert len(artifacts) == 1
    source_path, key, content, media_type, status, note = artifacts[0]
    assert source_path == str(blob)
    assert key == ".system_generated/conversations/0001.pb"
    assert content == b"\x08\x01conversation"
    assert media_type == "protobuf"
    assert status == "unparsed"
    assert "not decoded" in note


def test_source_artifacts_ignores_symlinks_and_oversized_blobs(tmp_path):
    brain, _, conversations = _brain(tmp_path)
    outside = tmp_path / "outside.pb"
    outside.write_bytes(b"secret")
    (conversations / "link.pb").symlink_to(outside)
    extractor = _extractor(tmp_path)
    extractor._MAX_ARTIFACT_BYTES = 4
    (conversations / "big.pb").write_bytes(b"x" * 10)

    assert list(extractor.source_artifacts()) == []
    assert extractor.discovery["artifacts_oversized"] == 1


def test_capture_stores_the_blob_as_an_unparsed_revision(tmp_path):
    brain, _, conversations = _brain(tmp_path)
    blob = conversations / "0001.pb"
    payload = b"\x08\x01undecoded-conversation"
    blob.write_bytes(payload)

    output_dir = tmp_path / "state"
    output_dir.mkdir()
    write_sessions(output_dir / "index_v2.sqlite", [])
    counts = capture_source_artifacts(output_dir, [_extractor(tmp_path)])

    conn = initialise(output_dir / "index_v2.sqlite")
    revisions = revisions_for_source(conn, source_identity_for("antigravity", str(blob)))
    assert counts == {"antigravity:unparsed": 1}
    assert len(revisions) == 1
    assert revisions[0]["decode_status"] == "unparsed"
    assert revisions[0]["media_type"] == "protobuf"
    assert read_source_revision(conn, revisions[0]["id"]) == payload

    summary = source_health_summary(conn)
    assert summary["states"]["unsupported"]["sources"] == 1


def test_capture_is_idempotent_and_isolates_a_broken_extractor(tmp_path):
    brain, _, conversations = _brain(tmp_path)
    (conversations / "0001.pb").write_bytes(b"\x08\x01")

    output_dir = tmp_path / "state"
    output_dir.mkdir()
    write_sessions(output_dir / "index_v2.sqlite", [])

    class Broken:
        class tool:
            value = "broken"

        def source_artifacts(self):
            raise RuntimeError("boom")

    capture_source_artifacts(output_dir, [Broken(), _extractor(tmp_path)])
    capture_source_artifacts(output_dir, [_extractor(tmp_path)])

    conn = initialise(output_dir / "index_v2.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1


def test_undecoded_blob_is_never_counted_as_a_parsed_session(tmp_path):
    brain, logs, conversations = _brain(tmp_path)
    (conversations / "0001.pb").write_bytes(b"\x08\x01")
    records = [
        {
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "content": "hello from antigravity",
            "created_at": datetime(2026, 1, 1).isoformat(),
        },
        {
            "type": "PLANNER_RESPONSE",
            "content": "hi",
            "created_at": datetime(2026, 1, 1, 0, 1).isoformat(),
        },
    ]
    (logs / "transcript_full.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    sessions = list(_extractor(tmp_path).extract_sessions())

    # The transcript is readable, so the conversation is a session; the .pb blob
    # is preserved separately and is not itself reported as parsed.
    assert len(sessions) == 1
    extractor = _extractor(tmp_path)
    list(extractor.extract_sessions())
    assert extractor.discovery.get("transcripts_parsed") == 1
