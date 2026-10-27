"""Portable archive bundles with source provenance (#163).

Two temporary archives stand in for two machines: the same record id, the same
source path, the same raw bytes — and then the same archives with one of those
things changed. Every test asserts what the *receiving* store looks like, because
the contract is that an import never destroys local content.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import (
    BackupError,
    add_memory,
    add_session_tag,
    get_session_tags,
    initialise,
    link_memory_to_session,
    record_feedback,
    save_session_knowledge,
    write_sessions,
)
from lore.storage.backup import DB_MEMBER, MANIFEST_NAME
from lore.storage.portable import (
    PORTABLE_FORMAT,
    PORTABLE_FORMAT_VERSION,
    create_portable_bundle,
    import_bundle,
    verify_portable_bundle,
)
from lore.storage.schema import SCHEMA_VERSION
from lore.storage.source_health import record_source_state
from lore.storage.source_revisions import (
    capture_source_revision,
    read_source_revision,
    source_identity_for,
)
from lore.storage.writer import v2_db_path

T0 = datetime(2026, 1, 8, 9, 30, tzinfo=timezone.utc)
NEEDLE = "haystackneedle"  # one FTS token, so MATCH needs no quoting


@contextmanager
def _namespace(scope: str):
    """Set the source namespace a store is written under (machine scope)."""
    saved = os.environ.get("LORE_SOURCE_NAMESPACE")
    os.environ["LORE_SOURCE_NAMESPACE"] = scope
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("LORE_SOURCE_NAMESPACE", None)
        else:
            os.environ["LORE_SOURCE_NAMESPACE"] = saved


def _session(session_id: str, *texts: str) -> UnifiedSession:
    messages = [
        UnifiedMessage(
            role=Role.USER if index % 2 == 0 else Role.ASSISTANT,
            content=text,
            timestamp=T0,
        )
        for index, text in enumerate(texts)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=T0,
        last_updated=T0,
        project_path="/proj",
        messages=messages,
    )


def _build_store(
    directory: Path,
    *,
    scope: str,
    session_id: str = "s1",
    texts: tuple[str, ...] = ("plain question", "plain answer"),
    source_path: Path | None = None,
    source_text: str = '{"event": "first"}\n',
    memory_title: str = "bundle fact",
) -> Path:
    """A store with a session, annotations, a memory and one captured revision."""
    directory.mkdir(parents=True, exist_ok=True)
    db = v2_db_path(directory)
    initialise(db).close()
    with _namespace(scope):
        write_sessions(db, [_session(session_id, *texts)])

    source = Path(source_path) if source_path else directory / "transcript.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(source_text, encoding="utf-8")
    conn = sqlite3.connect(str(db))
    try:
        capture_source_revision(
            conn,
            tool="claude-code",
            source_path=source,
            content=source.read_bytes(),
            namespace=scope,
        )
        record_source_state(
            conn,
            source_identity=source_identity_for("claude-code", source, scope),
            tool="claude-code",
            source_path=str(source),
            state="parsed",
        )
        conn.commit()
    finally:
        conn.close()

    add_session_tag(directory, session_id, "important")
    memory_id = add_memory(directory, "fact", memory_title, "the body", tags=["bundles"])
    link_memory_to_session(directory, memory_id, session_id, message_seq=0, note="from the session")
    record_feedback(directory, memory_id, "andre", 4)
    save_session_knowledge(directory, session_id, "fp-1", "test-model", 1, {"summary": "kept"})
    return directory


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> Path:
    """A shared path, so two stores can capture the *same* source identity."""
    return tmp_path_factory.mktemp("sources") / "transcript.jsonl"


@pytest.fixture(scope="module")
def machine_a(tmp_path_factory, sources: Path) -> Path:
    return _build_store(
        tmp_path_factory.mktemp("machine-a") / "archive",
        scope="machine-a",
        texts=("plain question", f"where is the {NEEDLE}"),
        source_path=sources,
    )


@pytest.fixture(scope="module")
def machine_b(tmp_path_factory, sources: Path) -> Path:
    """Same record id and same source path, different content and namespace."""
    return _build_store(
        tmp_path_factory.mktemp("machine-b") / "archive",
        scope="machine-b",
        texts=("different question", "different answer"),
        source_path=sources,
        source_text='{"event": "second"}\n',
        memory_title="bundle fact from b",
    )


@pytest.fixture(scope="module")
def machine_a_identical(tmp_path_factory, sources: Path) -> Path:
    """Same namespace, same path and same bytes as ``machine_a``."""
    return _build_store(
        tmp_path_factory.mktemp("machine-a-2") / "archive",
        scope="machine-a",
        texts=("plain question", f"where is the {NEEDLE}"),
        source_path=sources,
    )


@pytest.fixture(scope="module")
def machine_a_changed(tmp_path_factory, sources: Path) -> Path:
    """Same namespace and same source path as ``machine_a``, different bytes."""
    return _build_store(
        tmp_path_factory.mktemp("machine-a-3") / "archive",
        scope="machine-a",
        texts=("plain question", f"where is the {NEEDLE}"),
        source_path=sources,
        source_text='{"event": "changed"}\n',
    )


@pytest.fixture
def live(machine_a: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "live"
    shutil.copytree(machine_a, destination)
    return destination


def _export(store: Path, destination: Path) -> Path:
    return create_portable_bundle(store, destination).bundle


def _conn(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(v2_db_path(store)))
    conn.row_factory = sqlite3.Row
    return conn


def _count(store: Path, table: str) -> int:
    conn = _conn(store)
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        conn.close()


def _rewrite_member(bundle: Path, name: str, mutate) -> None:
    with zipfile.ZipFile(bundle) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in entries:
            archive.writestr(filename, mutate(data) if filename == name else data)


def _store_digest(store: Path) -> str:
    import hashlib

    return hashlib.sha256(v2_db_path(store).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# the bundle itself
# ---------------------------------------------------------------------------


def test_bundle_records_format_provenance_and_evidence(machine_a: Path, tmp_path: Path):
    export = create_portable_bundle(machine_a, tmp_path / "bundle.zip")

    with zipfile.ZipFile(export.bundle) as archive:
        assert set(archive.namelist()) == {MANIFEST_NAME, DB_MEMBER}
        manifest = json.loads(archive.read(MANIFEST_NAME))

    assert manifest["format"] == PORTABLE_FORMAT
    assert manifest["format_version"] == PORTABLE_FORMAT_VERSION
    assert manifest["provenance"]["namespace"] == "machine-a"
    assert manifest["provenance"]["machine_scope"]
    assert manifest["provenance"]["created_at"]
    assert manifest["provenance"]["lore_version"]
    assert manifest["schema"]["version"] == SCHEMA_VERSION
    assert manifest["evidence"]["source_revisions"] == 1
    assert len(manifest["evidence"]["digest"]) == 64
    assert manifest["counts"]["sessions"] == 1
    assert manifest["counts"]["messages"] == 2
    (member,) = manifest["members"]
    assert member["name"] == DB_MEMBER and member["size"] > 0
    assert verify_portable_bundle(export.bundle).ok


def test_bundle_carries_no_credentials(machine_b: Path, tmp_path: Path):
    secret = "sk-lore-bundle-test-7c31bd"
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "FLASK_SECRET_KEY"):
        os.environ[name] = secret
    try:
        source = tmp_path / "archive"
        shutil.copytree(machine_b, source)
        (source / "credentials.json").write_text(json.dumps({"api_key": secret}), encoding="utf-8")
        cache = source / "llm_cache"
        cache.mkdir()
        (cache / "providers.json").write_text(json.dumps({"api_key": secret}), encoding="utf-8")

        bundle = _export(source, tmp_path / "bundle.zip")

        assert secret.encode() not in bundle.read_bytes()
        with zipfile.ZipFile(bundle) as archive:
            for name in archive.namelist():
                assert secret.encode() not in archive.read(name)
    finally:
        for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "FLASK_SECRET_KEY"):
            os.environ.pop(name, None)


# ---------------------------------------------------------------------------
# round trip into a fresh home
# ---------------------------------------------------------------------------


def test_round_trip_into_a_fresh_home(machine_a: Path, tmp_path: Path):
    bundle = _export(machine_a, tmp_path / "bundle.zip")
    target = tmp_path / "fresh"

    report = import_bundle(bundle, target)

    assert report.sessions_added == 1
    assert report.messages_added == 2
    assert report.revisions_added == 1
    assert _count(target, "sessions") == 1
    assert _count(target, "messages") == 2
    assert _count(target, "source_revisions") == 1
    assert _count(target, "source_artifacts") == 0
    assert get_session_tags(target, "s1") == ["important"]

    conn = _conn(target)
    try:
        hits = conn.execute(
            "SELECT entity_id FROM search_index WHERE search_index MATCH ?", (NEEDLE,)
        ).fetchall()
        assert [row[0] for row in hits] == ["s1"]
        row = conn.execute(
            "SELECT source_namespace, source_record_id, content_sha256, copy_of_session_id "
            "FROM sessions WHERE id = 's1'"
        ).fetchone()
        assert row["source_namespace"] == "claude-code:machine-a"
        assert row["source_record_id"] == "s1"
        assert row["content_sha256"]
        assert row["copy_of_session_id"] is None
        assert conn.execute("SELECT title FROM memory").fetchone()[0] == "bundle fact"
        # The append-only import log records the same origin as the rows.
        log = conn.execute("SELECT mode, provenance_json FROM bundle_imports").fetchone()
        assert log["mode"] == "merge"
        assert json.loads(log["provenance_json"])["namespace"] == "machine-a"
    finally:
        conn.close()

    assert report.origins["claude-code:machine-a"] == {"sessions": 1, "revisions": 1}


def test_annotations_and_evidence_survive(machine_a: Path, tmp_path: Path):
    bundle = _export(machine_a, tmp_path / "bundle.zip")
    target = tmp_path / "fresh"

    report = import_bundle(bundle, target)

    assert report.tags_added == 1
    assert report.knowledge_added == 1
    assert report.memories_added == 1
    assert report.memory_tags_added == 1
    assert report.memory_feedback_added == 1
    assert report.evidence_links_added == 1
    assert report.health_added == 1

    conn = _conn(target)
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM session_knowledge WHERE session_id='s1'"
            ).fetchone()[0]
        )
        assert payload == {"summary": "kept"}
        link = conn.execute("SELECT * FROM memory_sources").fetchone()
        assert link["session_id"] == "s1"
        assert link["message_seq"] == 0
        assert link["message_sha256"]
        assert link["session_namespace"] == "claude-code:machine-a"
        # The link resolves to the message row that was actually imported.
        message = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND seq = ?",
            (link["session_id"], link["message_seq"]),
        ).fetchone()
        assert message["content"] == "plain question"
        assert conn.execute("SELECT score FROM memory_feedback").fetchone()[0] == 4
        assert conn.execute("SELECT state FROM source_health").fetchone()[0] == "parsed"
    finally:
        conn.close()


def test_import_is_idempotent(machine_a: Path, tmp_path: Path):
    bundle = _export(machine_a, tmp_path / "bundle.zip")
    target = tmp_path / "fresh"
    import_bundle(bundle, target)
    before = {table: _count(target, table) for table in ("sessions", "messages", "memory_sources")}

    second = import_bundle(bundle, target)

    assert second.revisions_added == 0
    assert second.revisions_deduplicated == 1
    assert second.sessions_added == 0
    assert second.sessions_identical == 1
    assert second.messages_added == 0
    assert second.memories_added == 0
    assert second.evidence_links_added == 0
    assert second.wrote_anything is False
    assert {table: _count(target, table) for table in before} == before


# ---------------------------------------------------------------------------
# identity: same record id from two namespaces
# ---------------------------------------------------------------------------


def test_differing_namespaces_for_the_same_record_id_both_survive(
    live: Path, machine_b: Path, tmp_path: Path
):
    bundle = _export(machine_b, tmp_path / "bundle.zip")

    report = import_bundle(bundle, live)

    assert report.sessions_copies == 1
    assert _count(live, "sessions") == 2
    conn = _conn(live)
    try:
        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, source_namespace, source_record_id, copy_of_session_id, content_sha256 "
                "FROM sessions"
            )
        }
        assert set(rows) == {"s1", "claude-code:machine-b:s1#2"}
        assert rows["s1"]["source_namespace"] == "claude-code:machine-a"
        copy = rows["claude-code:machine-b:s1#2"]
        assert copy["source_namespace"] == "claude-code:machine-b"
        assert copy["source_record_id"] == "s1"
        assert copy["copy_of_session_id"] == "s1"
        assert copy["content_sha256"] != rows["s1"]["content_sha256"]
        titles = {row[0] for row in conn.execute("SELECT content FROM messages")}
        assert {"plain question", "different question"} <= titles
    finally:
        conn.close()


def test_local_session_is_never_overwritten(live: Path, machine_b: Path, tmp_path: Path):
    bundle = _export(machine_b, tmp_path / "bundle.zip")
    before = _conn(live).execute("SELECT content_sha256 FROM sessions WHERE id='s1'").fetchone()[0]

    import_bundle(bundle, live)

    after = _conn(live).execute("SELECT content_sha256 FROM sessions WHERE id='s1'").fetchone()[0]
    assert after == before


# ---------------------------------------------------------------------------
# revisions: identical dedupe, divergent survive
# ---------------------------------------------------------------------------


def test_identical_revisions_deduplicate(live: Path, machine_a_identical: Path, tmp_path: Path):
    bundle = _export(machine_a_identical, tmp_path / "bundle.zip")
    revisions_before = _count(live, "source_revisions")

    report = import_bundle(bundle, live)

    assert report.revisions_added == 0
    assert report.revisions_deduplicated == 1
    assert _count(live, "source_revisions") == revisions_before == 1


def test_divergent_revision_is_kept_as_an_extra_revision(
    live: Path, machine_a_changed: Path, tmp_path: Path
):
    bundle = _export(machine_a_changed, tmp_path / "bundle.zip")

    report = import_bundle(bundle, live)

    assert report.revisions_added == 1
    assert _count(live, "source_revisions") == 2
    conn = _conn(live)
    try:
        rows = conn.execute(
            "SELECT id, revision_seq, content, content_sha256 FROM source_revisions "
            "ORDER BY revision_seq"
        ).fetchall()
        assert [row["revision_seq"] for row in rows] == [1, 2]
        assert rows[0]["content_sha256"] != rows[1]["content_sha256"]
        stored = conn.execute(
            "SELECT source_identity FROM source_revisions ORDER BY revision_seq"
        ).fetchall()
        # Both revisions belong to the same source identity: same file, changed bytes.
        assert stored[0][0] == stored[1][0]
    finally:
        conn.close()
    conn = _conn(live)
    try:
        first = read_source_revision(conn, rows[0]["id"])
        second = read_source_revision(conn, rows[1]["id"])
    finally:
        conn.close()
    assert (first, second) == (b'{"event": "first"}\n', b'{"event": "changed"}\n')


# ---------------------------------------------------------------------------
# dry run and tampering
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(live: Path, machine_b: Path, tmp_path: Path):
    bundle = _export(machine_b, tmp_path / "bundle.zip")
    before = _store_digest(live)

    report = import_bundle(bundle, live, dry_run=True)

    assert report.dry_run is True
    assert report.sessions_copies == 1
    assert report.revisions_added == 1
    assert _store_digest(live) == before
    assert _count(live, "sessions") == 1
    assert _count(live, "bundle_imports") == 0


def test_dry_run_against_an_empty_target_writes_nothing(machine_a: Path, tmp_path: Path):
    bundle = _export(machine_a, tmp_path / "bundle.zip")
    target = tmp_path / "fresh"

    report = import_bundle(bundle, target, dry_run=True)

    assert report.sessions_added == 1
    assert not v2_db_path(target).exists()


def test_tampered_bundle_is_refused_before_any_write(machine_a: Path, tmp_path: Path):
    bundle = _export(machine_a, tmp_path / "bundle.zip")

    def flip_last_byte(data: bytes) -> bytes:
        return data[:-1] + bytes([data[-1] ^ 0xFF])

    _rewrite_member(bundle, DB_MEMBER, flip_last_byte)
    target = tmp_path / "fresh"

    assert not verify_portable_bundle(bundle).ok
    with pytest.raises(BackupError, match="failed verification"):
        import_bundle(bundle, target)
    assert not v2_db_path(target).exists()


def test_tampered_evidence_digest_is_refused(machine_a: Path, tmp_path: Path):
    bundle = _export(machine_a, tmp_path / "bundle.zip")

    def reseal_evidence(data: bytes) -> bytes:
        manifest = json.loads(data)
        manifest["evidence"]["digest"] = "0" * 64
        return json.dumps(manifest).encode()

    _rewrite_member(bundle, MANIFEST_NAME, reseal_evidence)

    report = verify_portable_bundle(bundle)

    assert not report.ok
    assert any("evidence digest" in problem for problem in report.problems)
    with pytest.raises(BackupError, match="nothing was imported"):
        import_bundle(bundle, tmp_path / "fresh")


# ---------------------------------------------------------------------------
# tombstones, replacement and the CLI
# ---------------------------------------------------------------------------


def test_archive_tombstone_does_not_delete_a_local_session(
    machine_a: Path, live: Path, tmp_path: Path
):
    source = tmp_path / "archived"
    shutil.copytree(machine_a, source)
    conn = sqlite3.connect(str(v2_db_path(source)))
    try:
        # s2 was deleted on the other machine; this store never had it.
        conn.execute(
            "INSERT INTO deleted_sessions (session_id, deleted_at, reason) VALUES (?, ?, ?)",
            ("s2", T0.isoformat(), "pruned"),
        )
        conn.execute(
            "INSERT INTO deleted_sessions (session_id, deleted_at, reason) VALUES (?, ?, ?)",
            ("s1", T0.isoformat(), "pruned"),
        )
        conn.commit()
    finally:
        conn.close()
    bundle = _export(source, tmp_path / "bundle.zip")

    report = import_bundle(bundle, live)

    assert report.tombstones_added == 1
    assert report.tombstones_skipped == 1
    assert _count(live, "sessions") == 1
    assert _count(live, "deleted_sessions") == 1


def test_replace_mode_installs_the_archive(live: Path, machine_b: Path, tmp_path: Path):
    bundle = _export(machine_b, tmp_path / "bundle.zip")
    add_session_tag(live, "s9", "live-only")

    report = import_bundle(bundle, live, mode="replace")

    assert report.mode == "replace"
    assert report.sessions_added == 1
    assert get_session_tags(live, "s1") == ["important"]
    assert get_session_tags(live, "s9") == []
    conn = _conn(live)
    try:
        assert conn.execute("SELECT source_namespace FROM sessions").fetchone()[0] == (
            "claude-code:machine-b"
        )
        assert conn.execute("SELECT mode FROM bundle_imports").fetchone()[0] == "replace"
    finally:
        conn.close()


def test_cli_exports_and_imports(machine_a: Path, tmp_path: Path, monkeypatch, capsys):
    bundle = tmp_path / "cli.zip"
    target = tmp_path / "fresh"
    source = tmp_path / "archive"
    shutil.copytree(machine_a, source)

    monkeypatch.setattr(
        sys, "argv", ["lore", "--output-dir", str(source), "bundle", "export", str(bundle)]
    )
    lore_cli.main()
    written = capsys.readouterr().out
    assert f"Bundle written: {bundle}" in written
    assert f"{PORTABLE_FORMAT} v{PORTABLE_FORMAT_VERSION}" in written
    assert "machine-a" in written
    assert "source_revisions=1" in written

    monkeypatch.setattr(
        sys,
        "argv",
        ["lore", "--output-dir", str(target), "bundle", "import", str(bundle), "--dry-run"],
    )
    lore_cli.main()
    dry = capsys.readouterr().out
    assert "Would import" in dry
    assert "Dry run: nothing was written." in dry
    assert not v2_db_path(target).exists()

    monkeypatch.setattr(
        sys, "argv", ["lore", "--output-dir", str(target), "bundle", "import", str(bundle)]
    )
    lore_cli.main()
    imported = capsys.readouterr().out
    assert "Imported" in imported
    assert "origins:    claude-code:machine-a: sessions=1 revisions=1" in imported
    assert _count(target, "sessions") == 1
