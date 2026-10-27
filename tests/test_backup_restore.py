"""Versioned, SQLite-consistent backups and verified restoration (#161).

The store fixture is built once per module and copied per test: writing
messages embeds the sessions, and loading the embedding model for every test
would dwarf the actual assertions.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import (
    BackupError,
    add_memory,
    add_session_tag,
    create_backup,
    get_session_tags,
    initialise,
    restore_backup,
    verify_bundle,
)
from lore.storage.backup import BUNDLE_FORMAT_VERSION, DB_MEMBER, MANIFEST_NAME
from lore.storage.schema import MIGRATIONS, SCHEMA_VERSION
from lore.storage.writer import v2_db_path, write_sessions
from lore.utils.paths import lore_home

T0 = datetime(2026, 1, 8, 9, 30, tzinfo=timezone.utc)
NEEDLE = "haystackneedle"  # one FTS token, so MATCH needs no quoting


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


def _build_store(output_dir: Path) -> None:
    """A store with transcripts, FTS rows, a memory and a tag."""
    output_dir.mkdir(parents=True, exist_ok=True)
    db = v2_db_path(output_dir)
    initialise(db)
    write_sessions(
        db,
        [
            _session("s1", f"where is the {NEEDLE}", "it is in the archive"),
            _session("s2", "unrelated question", "unrelated answer"),
        ],
    )
    add_memory(output_dir, "fact", "backup test fact", "the body", tags=["backup"])
    add_session_tag(output_dir, "s1", "important")


@pytest.fixture(scope="module")
def built_store(tmp_path_factory) -> Path:
    store = tmp_path_factory.mktemp("built") / "archive"
    _build_store(store)
    return store


@pytest.fixture
def store(built_store: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "archive"
    shutil.copytree(built_store, destination)
    return destination


def _row_counts(store_dir: Path) -> dict[str, int]:
    """Row count per logical table (FTS index included, vec0 tables skipped)."""
    conn = sqlite3.connect(str(v2_db_path(store_dir)))
    try:
        tables = [
            name
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
            # ``session_embeddings`` is a vec0 virtual table: reading it needs
            # the optional sqlite-vec extension, which this connection has not
            # loaded. It travels inside the snapshot with the rest of the file.
            if "USING vec0" not in (sql or "")
        ]
        return {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables
        }
    finally:
        conn.close()


def _rewrite_member(bundle: Path, name: str, mutate) -> None:
    """Replace one member of ``bundle`` with ``mutate(original bytes)``."""
    with zipfile.ZipFile(bundle) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in entries:
            archive.writestr(filename, mutate(data) if filename == name else data)


# ---------------------------------------------------------------------------
# bundle shape and manifest
# ---------------------------------------------------------------------------


def test_backup_writes_a_zip_with_a_format_version_marker(store: Path, tmp_path: Path):
    bundle = tmp_path / "bundle.zip"
    result = create_backup(store, bundle)

    assert result.bundle == bundle
    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {MANIFEST_NAME, DB_MEMBER}
        manifest = json.loads(archive.read(MANIFEST_NAME))

    assert manifest["format"] == "lore-backup"
    assert manifest["format_version"] == BUNDLE_FORMAT_VERSION
    assert manifest["lore_version"]
    assert manifest["created_at"]
    assert manifest["schema"]["version"] == SCHEMA_VERSION
    assert manifest["schema"]["expected_version"] == SCHEMA_VERSION
    assert [entry["version"] for entry in manifest["schema"]["migrations"]] == [
        version for version, _, _ in MIGRATIONS
    ]
    (member,) = manifest["members"]
    assert member["name"] == DB_MEMBER
    assert member["size"] > 0
    assert len(member["sha256"]) == 64


def test_manifest_counts_every_archived_table(store: Path, tmp_path: Path):
    manifest = create_backup(store, tmp_path / "bundle.zip").manifest

    assert manifest.counts["sessions"] == 2
    assert manifest.counts["messages"] == 4
    assert manifest.counts["search_index"] >= 3  # two sessions + at least the needle message
    assert manifest.counts["memory"] == 1
    assert manifest.counts["memory_tags"] == 1
    assert manifest.counts["session_tags"] == 1
    assert manifest.counts == {
        table: count for table, count in _row_counts(store).items() if table in manifest.counts
    }


def test_backup_refuses_a_directory_without_a_store(tmp_path: Path):
    with pytest.raises(BackupError, match="no v2 store"):
        create_backup(tmp_path / "empty", tmp_path / "bundle.zip")


def test_backup_names_the_bundle_when_given_a_directory(store: Path, tmp_path: Path):
    destination = tmp_path / "bundles"
    destination.mkdir()
    result = create_backup(store, destination)

    assert result.bundle.parent == destination
    assert result.bundle.name.startswith("lore-backup-")
    assert result.bundle.suffix == ".zip"


# ---------------------------------------------------------------------------
# the snapshot is taken from SQLite, not from the file
# ---------------------------------------------------------------------------


def test_snapshot_includes_commits_still_in_the_wal(store: Path, tmp_path: Path):
    """A raw copy of a live WAL database would miss the newest commit."""
    db = v2_db_path(store)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        conn.execute(
            "INSERT INTO session_tags(session_id, tag, created) VALUES ('s2', 'wal-only', ?)",
            (T0.isoformat(),),
        )
        conn.commit()
        wal = Path(str(db) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0

        manifest = create_backup(store, tmp_path / "bundle.zip").manifest
        assert manifest.counts["session_tags"] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


def test_round_trip_restores_every_row_in_an_isolated_home(built_store: Path, tmp_path: Path):
    live = lore_home()
    shutil.copytree(built_store, live)
    result = create_backup(live, tmp_path / "bundle.zip")
    assert verify_bundle(result.bundle).ok

    target = tmp_path / "restored"
    report = restore_backup(result.bundle, target)

    assert report.store == v2_db_path(target)
    assert not report.replaced
    restored_counts = _row_counts(target)
    for table, expected in result.manifest.counts.items():
        assert restored_counts[table] == expected, table

    conn = sqlite3.connect(str(v2_db_path(target)))
    try:
        hits = conn.execute(
            "SELECT entity_id FROM search_index WHERE search_index MATCH ?", (NEEDLE,)
        ).fetchall()
        assert [row[0] for row in hits] == ["s1"]
        assert conn.execute("SELECT title FROM memory").fetchone()[0] == "backup test fact"
    finally:
        conn.close()
    assert get_session_tags(target, "s1") == ["important"]


def test_restored_store_is_usable_without_rebuilding(tmp_path: Path):
    """No index repair: the FTS table comes back with its rows."""
    source = tmp_path / "source"
    _build_store(source)
    bundle = create_backup(source, tmp_path / "bundle.zip").bundle
    target = tmp_path / "target"
    restore_backup(bundle, target)

    conn = sqlite3.connect(str(v2_db_path(target)))
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM search_index WHERE search_index MATCH 'archive'"
            ).fetchone()[0]
            >= 1
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def test_verify_passes_on_a_fresh_bundle(store: Path, tmp_path: Path):
    report = verify_bundle(create_backup(store, tmp_path / "bundle.zip").bundle)

    assert report.ok
    assert report.problems == ()
    assert report.manifest is not None
    assert report.manifest.counts["sessions"] == 2


def test_verify_reports_a_missing_file(tmp_path: Path):
    report = verify_bundle(tmp_path / "nowhere.zip")

    assert not report.ok
    assert "does not exist" in report.problems[0]


def test_verify_reports_a_file_that_is_not_a_zip(tmp_path: Path):
    path = tmp_path / "notes.zip"
    path.write_bytes(b"definitely not a zip")

    report = verify_bundle(path)

    assert not report.ok
    assert "not a zip bundle" in report.problems[0]


def test_tampered_payload_fails_the_checksum_and_blocks_the_restore(store: Path, tmp_path: Path):
    bundle = create_backup(store, tmp_path / "bundle.zip").bundle

    def flip_last_byte(data: bytes) -> bytes:
        return data[:-1] + bytes([data[-1] ^ 0xFF])

    _rewrite_member(bundle, DB_MEMBER, flip_last_byte)

    report = verify_bundle(bundle)
    assert not report.ok
    assert any("sha256" in problem for problem in report.problems)
    with pytest.raises(BackupError, match="failed verification"):
        restore_backup(bundle, tmp_path / "restored")
    assert not v2_db_path(tmp_path / "restored").exists()


def test_manifest_counts_must_match_the_snapshot(store: Path, tmp_path: Path):
    bundle = create_backup(store, tmp_path / "bundle.zip").bundle

    def inflate_messages(data: bytes) -> bytes:
        manifest = json.loads(data)
        manifest["counts"]["messages"] = 99
        return json.dumps(manifest).encode()

    _rewrite_member(bundle, MANIFEST_NAME, inflate_messages)

    report = verify_bundle(bundle)
    assert not report.ok
    assert any(
        "messages has 4 rows, the manifest says 99" in problem for problem in report.problems
    )


def test_verify_rejects_an_unexpected_member(store: Path, tmp_path: Path):
    bundle = create_backup(store, tmp_path / "bundle.zip").bundle
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("extra/payload.bin", b"smuggled")

    report = verify_bundle(bundle)

    assert not report.ok
    assert any("unexpected member" in problem for problem in report.problems)


def test_verify_rejects_a_newer_bundle_format(store: Path, tmp_path: Path):
    bundle = create_backup(store, tmp_path / "bundle.zip").bundle

    def bump_format(data: bytes) -> bytes:
        manifest = json.loads(data)
        manifest["format_version"] = BUNDLE_FORMAT_VERSION + 1
        return json.dumps(manifest).encode()

    _rewrite_member(bundle, MANIFEST_NAME, bump_format)

    report = verify_bundle(bundle)

    assert not report.ok
    assert any("newer than this Lore" in problem for problem in report.problems)


def test_verify_rejects_a_bundle_without_a_manifest(store: Path, tmp_path: Path):
    bundle = create_backup(store, tmp_path / "bundle.zip").bundle
    with zipfile.ZipFile(bundle) as archive:
        payload = archive.read(DB_MEMBER)
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DB_MEMBER, payload)

    report = verify_bundle(bundle)

    assert not report.ok
    assert "missing from the bundle" in report.problems[0]


# ---------------------------------------------------------------------------
# schema compatibility
# ---------------------------------------------------------------------------


def test_bundle_from_a_newer_schema_is_refused(store: Path, tmp_path: Path):
    conn = sqlite3.connect(str(v2_db_path(store)))
    try:
        conn.execute(
            "INSERT INTO schema_version(version, description, applied_at) VALUES (?, ?, ?)",
            (SCHEMA_VERSION + 1, "a migration this Lore does not have", T0.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    result = create_backup(store, tmp_path / "bundle.zip")
    assert result.manifest.schema_version == SCHEMA_VERSION + 1

    report = verify_bundle(result.bundle)
    assert not report.ok
    assert any("newer than this Lore" in problem for problem in report.problems)
    with pytest.raises(BackupError, match="newer than this Lore"):
        restore_backup(result.bundle, tmp_path / "restored")
    assert not v2_db_path(tmp_path / "restored").exists()


# ---------------------------------------------------------------------------
# restoring over a live store
# ---------------------------------------------------------------------------


def test_restore_refuses_to_overwrite_without_force(built_store: Path, tmp_path: Path):
    bundle = create_backup(built_store, tmp_path / "bundle.zip").bundle
    live = tmp_path / "live"
    shutil.copytree(built_store, live)
    add_session_tag(live, "s9", "live-only")

    with pytest.raises(BackupError, match="--force"):
        restore_backup(bundle, live)

    assert get_session_tags(live, "s9") == ["live-only"]
    assert _row_counts(live)["session_tags"] == 2


def test_restore_force_replaces_the_store(built_store: Path, tmp_path: Path):
    bundle = create_backup(built_store, tmp_path / "bundle.zip").bundle
    live = tmp_path / "live"
    shutil.copytree(built_store, live)
    add_session_tag(live, "s9", "live-only")

    report = restore_backup(bundle, live, force=True)

    assert report.replaced
    assert get_session_tags(live, "s9") == []
    assert _row_counts(live)["session_tags"] == report.manifest.counts["session_tags"]


# ---------------------------------------------------------------------------
# no credentials
# ---------------------------------------------------------------------------


def test_bundle_carries_no_credentials(built_store: Path, tmp_path: Path, monkeypatch):
    secret = "sk-lore-backup-test-4f2a9c"
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "FLASK_SECRET_KEY"):
        monkeypatch.setenv(name, secret)

    live = lore_home()
    shutil.copytree(built_store, live)
    provider_cache = live / "llm_cache"
    provider_cache.mkdir()
    (provider_cache / "providers.json").write_text(
        json.dumps({"api_key": secret}), encoding="utf-8"
    )

    result = create_backup(live, tmp_path / "bundle.zip")

    assert secret.encode() not in result.bundle.read_bytes()
    with zipfile.ZipFile(result.bundle) as archive:
        assert set(archive.namelist()) == {MANIFEST_NAME, DB_MEMBER}
        for name in archive.namelist():
            assert secret.encode() not in archive.read(name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["lore", *argv])
    lore_cli.main()


def test_cli_backup_verify_and_restore(built_store: Path, tmp_path: Path, monkeypatch, capsys):
    live = lore_home()
    shutil.copytree(built_store, live)
    bundle = tmp_path / "cli.zip"

    _run_cli(monkeypatch, "--output-dir", str(live), "backup", str(bundle))
    written = capsys.readouterr().out
    assert f"Backup written: {bundle}" in written
    assert "lore-backup v1" in written
    assert "sessions=2" in written

    _run_cli(monkeypatch, "backup", "verify", str(bundle))
    verified = capsys.readouterr().out
    assert f"Bundle OK: {bundle}" in verified

    restored = tmp_path / "restored"
    _run_cli(monkeypatch, "--output-dir", str(restored), "restore", str(bundle))
    output = capsys.readouterr().out
    assert f"Restored {v2_db_path(restored)} (created)" in output
    assert v2_db_path(restored).is_file()


def test_cli_verify_reports_a_bad_bundle(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not a zip")

    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "backup", "verify", str(path))

    assert exit_info.value.code == 1
    assert "Bundle FAILED verification" in capsys.readouterr().err


def test_cli_restore_without_force_exits_1(built_store: Path, tmp_path: Path, monkeypatch, capsys):
    bundle = create_backup(built_store, tmp_path / "bundle.zip").bundle
    live = tmp_path / "live"
    shutil.copytree(built_store, live)

    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "--output-dir", str(live), "restore", str(bundle))

    assert exit_info.value.code == 1
    assert "Restore refused" in capsys.readouterr().err


def test_cli_backup_without_a_path_prints_usage(tmp_path: Path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "backup", "verify")

    assert exit_info.value.code == 1
    assert "usage: lore backup [verify] <path>" in capsys.readouterr().err
