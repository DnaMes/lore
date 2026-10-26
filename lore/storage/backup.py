"""Versioned, SQLite-consistent backups and verified restoration (issue #161).

A bundle is a single zip file with two parts:

``manifest.json``
    The format-version marker (``format`` + ``format_version``), the creation
    time, the Lore version, the store's schema and applied migration versions,
    one sha256 and size per payload member, and the row count of every
    archived table.
``db/index_v2.sqlite``
    A consistent snapshot of the v2 store, taken with SQLite's online-backup
    API. A raw copy of a live WAL database can miss (or half-capture) the
    newest commits, so the snapshot is never a file copy.

Only the v2 store is archived. The rest of the data directory stays out on
purpose: provider credentials live in the environment (see
``docs/CONFIGURATION.md``), not in the store, and nothing in this module reads
the environment.

Restoring is verify-first: checksums, the snapshot's own ``integrity_check``,
the row counts and the schema version are all checked against the manifest
before the live store is touched. The snapshot is written to a sibling temp
file and moved into place with ``os.replace``, so a restore that fails leaves
the existing store as it was.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple
from uuid import uuid4

from .. import __version__
from ..utils.paths import restrict_file
from .schema import MIGRATIONS
from .writer import v2_db_path

BUNDLE_FORMAT = "lore-backup"
BUNDLE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_MEMBER = "db/index_v2.sqlite"

# Tables whose row counts go into the manifest and are re-checked on verify
# and restore: the transcript (sessions/messages), the shared FTS5 index, the
# memory store with its links and ratings, the user annotations (tags,
# knowledge, summaries, feedback) and the tombstones. The optional vec0
# ``session_embeddings`` table is carried inside the snapshot but not counted
# here — reading it needs the sqlite-vec extension, which the verifying host
# may not have.
COUNTED_TABLES: Tuple[str, ...] = (
    "sessions",
    "messages",
    "search_index",
    "memory",
    "memory_tags",
    "memory_sources",
    "memory_feedback",
    "session_tags",
    "session_knowledge",
    "session_summaries",
    "session_embedding_meta",
    "deleted_sessions",
)


class BackupError(RuntimeError):
    """A bundle could not be created, or must not be restored."""


@dataclass(frozen=True)
class Member:
    """One payload file inside a bundle."""

    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class BackupManifest:
    """The ``manifest.json`` contract of a bundle."""

    format_version: int
    created_at: str
    lore_version: str
    schema_version: int
    expected_schema_version: int
    migrations: Tuple[Tuple[int, str], ...]
    counts: Dict[str, int]
    members: Tuple[Member, ...]

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON form written into the bundle."""
        return {
            "format": BUNDLE_FORMAT,
            "format_version": self.format_version,
            "created_at": self.created_at,
            "lore_version": self.lore_version,
            "schema": {
                "version": self.schema_version,
                "expected_version": self.expected_schema_version,
                "migrations": [
                    {"version": version, "description": description}
                    for version, description in self.migrations
                ],
            },
            "counts": dict(self.counts),
            "members": [
                {"name": member.name, "sha256": member.sha256, "size": member.size}
                for member in self.members
            ],
        }


@dataclass(frozen=True)
class BackupResult:
    """Where a new bundle landed, and the manifest it carries."""

    bundle: Path
    manifest: BackupManifest


@dataclass(frozen=True)
class BundleReport:
    """Outcome of :func:`verify_bundle`; problems are collected, not raised."""

    path: Path
    ok: bool
    problems: Tuple[str, ...]
    manifest: Optional[BackupManifest]


@dataclass(frozen=True)
class RestoreReport:
    """Outcome of :func:`restore_backup`."""

    bundle: Path
    store: Path
    replaced: bool
    manifest: BackupManifest


def create_backup(output_dir: Path, destination: Path) -> BackupResult:
    """Write a bundle of the v2 store in ``output_dir`` to ``destination``.

    ``destination`` may be an existing directory, in which case the bundle is
    named ``lore-backup-<UTC timestamp>.zip`` inside it. The bundle itself is
    written to a temp file in the destination directory and moved into place,
    so an interrupted run never leaves a half-written bundle under the final
    name.
    """
    output_dir = Path(output_dir).expanduser()
    source = v2_db_path(output_dir)
    if not source.is_file():
        raise BackupError(f"no v2 store at {source} — nothing to back up")

    destination = Path(destination).expanduser()
    if destination.is_dir():
        destination = destination / _default_bundle_name()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lore-backup-") as workdir:
        snapshot = Path(workdir) / "index_v2.sqlite"
        _snapshot_database(source, snapshot)
        problems = _integrity_problems(snapshot)
        if problems:
            raise BackupError(f"the snapshot is not a usable store: {'; '.join(problems)}")
        schema_version, migrations = _read_schema(snapshot)
        manifest = BackupManifest(
            format_version=BUNDLE_FORMAT_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            lore_version=__version__,
            schema_version=schema_version,
            expected_schema_version=len(MIGRATIONS),
            migrations=migrations,
            counts=_read_counts(snapshot),
            members=(
                Member(
                    name=DB_MEMBER,
                    sha256=_sha256_file(snapshot),
                    size=snapshot.stat().st_size,
                ),
            ),
        )
        partial = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    MANIFEST_NAME,
                    json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
                )
                archive.write(snapshot, DB_MEMBER)
            os.replace(partial, destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    restrict_file(destination)
    return BackupResult(bundle=destination, manifest=manifest)


def verify_bundle(bundle: Path) -> BundleReport:
    """Check a bundle without restoring it.

    Covers the zip structure, the format marker, every member checksum and
    size, the snapshot's ``PRAGMA integrity_check``, the row counts against
    the manifest, and whether this Lore can restore the bundle's schema at
    all. Every problem found is reported; nothing is raised and nothing is
    written outside a temp directory.
    """
    bundle = Path(bundle).expanduser()
    if not bundle.is_file():
        return BundleReport(bundle, False, (f"{bundle} does not exist",), None)

    try:
        archive = zipfile.ZipFile(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        return BundleReport(bundle, False, (f"not a zip bundle: {exc}",), None)

    with archive:
        names = archive.namelist()
        if MANIFEST_NAME not in names:
            return BundleReport(
                bundle, False, (f"{MANIFEST_NAME} is missing from the bundle",), None
            )
        try:
            manifest = _parse_manifest(json.loads(archive.read(MANIFEST_NAME)))
        except (KeyError, TypeError, ValueError) as exc:
            return BundleReport(bundle, False, (f"{MANIFEST_NAME} is unreadable: {exc}",), None)

        problems: List[str] = []
        if manifest.format_version > BUNDLE_FORMAT_VERSION:
            problems.append(
                f"bundle format {manifest.format_version} is newer than this Lore "
                f"supports ({BUNDLE_FORMAT_VERSION})"
            )
        member_names = {member.name for member in manifest.members}
        for unexpected in sorted(set(names) - member_names - {MANIFEST_NAME}):
            problems.append(f"unexpected member {unexpected!r} in the bundle")
        for member in manifest.members:
            problems.extend(_member_problems(archive, member))
        corrupt = archive.testzip()
        if corrupt:
            problems.append(f"zip entry {corrupt!r} failed its CRC check")
        if not problems:
            problems.extend(_snapshot_problems(archive, manifest))
        return BundleReport(bundle, not problems, tuple(problems), manifest)


def restore_backup(bundle: Path, output_dir: Path, *, force: bool = False) -> RestoreReport:
    """Replace the v2 store in ``output_dir`` with the one in ``bundle``.

    The bundle is verified first and nothing is written when that fails. An
    existing store is only replaced with ``force``; the snapshot then lands in
    a sibling temp file and is moved over the store, so a failure leaves the
    old store untouched.
    """
    bundle = Path(bundle).expanduser()
    report = verify_bundle(bundle)
    if not report.ok:
        raise BackupError(
            f"{bundle} failed verification, nothing was restored: " + "; ".join(report.problems)
        )
    manifest = report.manifest
    if manifest is None:  # pragma: no cover - report.ok implies a manifest
        raise BackupError(f"{bundle} has no manifest")
    member = next(member for member in manifest.members if member.name == DB_MEMBER)

    output_dir = Path(output_dir).expanduser()
    store = v2_db_path(output_dir)
    replaced = store.exists()
    if replaced and not force:
        raise BackupError(
            f"{store} already exists; pass --force to replace it "
            "(back it up first with `lore backup <path>`)"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    temp_path = store.with_name(f".{store.name}.restore-{uuid4().hex}")
    try:
        with zipfile.ZipFile(bundle) as archive, archive.open(DB_MEMBER) as source:
            with open(temp_path, "wb") as target:
                shutil.copyfileobj(source, target)
        digest = _sha256_file(temp_path)
        if digest != member.sha256:
            raise BackupError(
                f"{DB_MEMBER} changed while it was extracted "
                f"(sha256 {digest}, manifest says {member.sha256})"
            )
        if replaced:
            _discard_store(store)
        os.replace(temp_path, store)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    restrict_file(store)
    return RestoreReport(bundle=bundle, store=store, replaced=replaced, manifest=manifest)


def _default_bundle_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"lore-backup-{stamp}.zip"


def _snapshot_database(source: Path, snapshot: Path) -> None:
    """Copy ``source`` into ``snapshot`` through SQLite's online-backup API."""
    uri = source.resolve().as_uri() + "?mode=ro"
    try:
        source_conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot read {source}: {exc}") from exc
    try:
        target_conn = sqlite3.connect(str(snapshot))
        try:
            source_conn.backup(target_conn)
            # Fold the copy into one self-contained file: a bundle carries no
            # -wal/-shm, and a non-WAL file can be opened by a reader without
            # write access to its directory.
            target_conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _discard_store(store: Path) -> None:
    """Checkpoint the store being replaced and drop its WAL sidecars.

    A restored file must not be paired with the replaced store's ``-wal`` or
    ``-shm``: SQLite would replay foreign frames into it. The checkpoint runs
    first so the old store stays complete if the swap fails after this point.
    """
    try:
        conn = sqlite3.connect(str(store))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # busy or not a database — the sidecars go anyway
    for suffix in ("-wal", "-shm"):
        Path(str(store) + suffix).unlink(missing_ok=True)


def _parse_manifest(data: Dict[str, Any]) -> BackupManifest:
    if data.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"format marker {data.get('format')!r} is not {BUNDLE_FORMAT!r}")
    schema = data["schema"]
    return BackupManifest(
        format_version=int(data["format_version"]),
        created_at=str(data["created_at"]),
        lore_version=str(data["lore_version"]),
        schema_version=int(schema["version"]),
        expected_schema_version=int(schema["expected_version"]),
        migrations=tuple(
            (int(entry["version"]), str(entry["description"])) for entry in schema["migrations"]
        ),
        counts={str(table): int(count) for table, count in data["counts"].items()},
        members=tuple(
            Member(name=str(entry["name"]), sha256=str(entry["sha256"]), size=int(entry["size"]))
            for entry in data["members"]
        ),
    )


def _member_problems(archive: zipfile.ZipFile, member: Member) -> List[str]:
    try:
        info = archive.getinfo(member.name)
    except KeyError:
        return [f"{member.name} is missing from the bundle"]
    problems = []
    if info.file_size != member.size:
        problems.append(f"{member.name} is {info.file_size} bytes, the manifest says {member.size}")
    with archive.open(member.name) as handle:
        digest = _sha256_stream(handle)
    if digest != member.sha256:
        problems.append(f"{member.name} has sha256 {digest}, the manifest says {member.sha256}")
    return problems


def _snapshot_problems(archive: zipfile.ZipFile, manifest: BackupManifest) -> List[str]:
    """Integrity, row-count and schema checks on the archived snapshot."""
    if DB_MEMBER not in {member.name for member in manifest.members}:
        return []
    with tempfile.TemporaryDirectory(prefix="lore-backup-verify-") as workdir:
        snapshot = Path(workdir) / "index_v2.sqlite"
        with archive.open(DB_MEMBER) as source, open(snapshot, "wb") as target:
            shutil.copyfileobj(source, target)

        problems = _integrity_problems(snapshot)
        if problems:
            return problems

        schema_version, _ = _read_schema(snapshot)
        if schema_version != manifest.schema_version:
            problems.append(
                f"the snapshot is at schema {schema_version}, the manifest says "
                f"{manifest.schema_version}"
            )
        supported = len(MIGRATIONS)
        if schema_version > supported:
            problems.append(
                f"the snapshot uses schema {schema_version}, newer than this Lore "
                f"supports ({supported}); upgrade Lore to restore it"
            )

        counts = _read_counts(snapshot)
        for table, expected in manifest.counts.items():
            actual = counts.get(table)
            if actual is None:
                problems.append(f"the manifest lists table {table!r}, the snapshot has none")
            elif actual != expected:
                problems.append(f"{table} has {actual} rows, the manifest says {expected}")
        return problems


def _integrity_problems(db_path: Path) -> List[str]:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return [f"the snapshot is not a readable SQLite database: {exc}"]
    if rows == ["ok"]:
        return []
    return [f"integrity_check failed: {'; '.join(str(row) for row in rows[:5])}"]


def _read_schema(db_path: Path) -> Tuple[int, Tuple[Tuple[int, str], ...]]:
    """Return ``(version, applied migrations)`` for the store at ``db_path``."""
    conn = sqlite3.connect(str(db_path))
    try:
        if not _has_table(conn, "schema_version"):
            return 0, ()
        rows = conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    migrations = tuple((int(version), str(description)) for version, description in rows)
    return (migrations[-1][0] if migrations else 0), migrations


def _read_counts(db_path: Path) -> Dict[str, int]:
    """Count every archived table that exists in the store at ``db_path``."""
    conn = sqlite3.connect(str(db_path))
    try:
        counts: Dict[str, int] = {}
        for table in COUNTED_TABLES:
            if _has_table(conn, table):
                # Table names come from the COUNTED_TABLES constant, never from
                # input, so interpolation cannot inject SQL.
                counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        return counts
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _sha256_file(path: Path) -> str:
    with open(path, "rb") as handle:
        return _sha256_stream(handle)


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
