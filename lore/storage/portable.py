"""Portable archive bundles: export, verify and merge-import (issue #163).

``lore backup``/``lore restore`` move a store within one machine — restore
replaces the store wholesale. Moving an archive *between* machines needs the
other half: an import that merges into a store it must never damage.

A portable bundle is the backup bundle plus a contract:

``manifest.json``
    ``format: "lore-portable-bundle"``, ``format_version``, the provenance of the
    exporting store (source namespace scope, machine scope, creation time, Lore
    version), the store's schema and applied migrations, an evidence
    fingerprint over every captured ``(source_identity, content_sha256)``
    revision, one sha256 and size per payload member, and the row count of every
    archived table.
``db/index_v2.sqlite``
    The SQLite online-backup snapshot: sessions, messages, annotations, memory
    and the raw ``source_revisions``/``source_artifacts`` evidence. It travels as
    one member so the joins between them (``sessions.source_revision_id``,
    ``memory_sources.source_revision_id``) cannot come apart.

Only the store is archived. Provider credentials live in the environment (see
``docs/CONFIGURATION.md``), never in the store, and nothing in this module reads
the environment for a secret.

Import is verify-first: manifest, member checksums and sizes, the evidence
fingerprint, the snapshot's ``PRAGMA integrity_check``, the row counts and the
schema compatibility are all checked before the live store is opened. Then:

- revisions dedupe on ``(source_identity, content_sha256)``; changed bytes become
  an extra ``revision_seq``, never an overwritten revision;
- sessions follow :func:`lore.storage.identity.resolve_store_identity`, so an
  identical copy changes nothing and a divergent copy is stored under a
  qualified id with ``copy_of_session_id`` — a local row is never replaced;
- imported rows keep their original ``source_namespace``, and the report counts
  what came from which origin;
- tags, knowledge, summaries, memory (with tags, ratings and evidence links),
  tombstones and per-source health are merged, never overwritten;
- embeddings are not imported: a vector is derived data belonging to the host
  that computed it.

``--dry-run`` merges into a temporary snapshot of the live store, so it reports
the exact counts of a real import while writing nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from .. import __version__
from ..utils.paths import restrict_file
from .backup import (
    DB_MEMBER,
    MANIFEST_NAME,
    BackupError,
    Member,
    _discard_store,
    _integrity_problems,
    _member_problems,
    _read_counts,
    _read_schema,
    _sha256_file,
    _snapshot_database,
)
from .identity import NAMESPACE_ENV, content_hash_from_rows, resolve_store_identity
from .schema import SCHEMA_VERSION, open_v2_connection
from .writer import v2_db_path

PORTABLE_FORMAT = "lore-portable-bundle"
PORTABLE_FORMAT_VERSION = 1

#: Modes accepted by :func:`import_bundle`; ``--merge`` is the default.
IMPORT_MODES = ("merge", "replace")

#: Raw evidence tables whose fingerprint goes into the manifest.
EVIDENCE_TABLES = ("source_revisions", "source_artifacts")

_SESSION_COLUMNS: Tuple[str, ...] = (
    "id",
    "tool",
    "project",
    "thread_id",
    "title",
    "created",
    "updated",
    "source_path",
    "source_mtime_ns",
    "git_branch",
    "git_commit",
    "cli_version",
    "metadata_json",
    "messages_count",
    "prompt_count",
    "prompt_outline",
    "export_path",
    "total_tokens",
    "messages_synced",
    "origin",
    "parent_session_id",
    "relation",
    "source_version",
    "parser_version",
    "source_revision_id",
    "source_namespace",
    "source_record_id",
    "content_sha256",
    "copy_of_session_id",
)

_MESSAGE_COLUMNS: Tuple[str, ...] = (
    "seq",
    "role",
    "content",
    "timestamp",
    "model",
    "tokens_json",
    "kind",
    "parent_id",
    "active",
    "tool_events_json",
    "reasoning",
    "native_id",
    "content_blocks_json",
    "tool_calls_json",
    "status",
    "extensions_json",
    "envelope_version",
)

_EVIDENCE_LINK_COLUMNS: Tuple[str, ...] = (
    "memory_id",
    "session_id",
    "message_id",
    "note",
    "message_seq",
    "message_native_id",
    "message_sha256",
    "source_revision_id",
    "session_namespace",
)


@dataclass(frozen=True)
class Provenance:
    """Where a bundle came from: the origin of every row it carries."""

    namespace: str
    machine_scope: str
    created_at: str
    lore_version: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "namespace": self.namespace,
            "machine_scope": self.machine_scope,
            "created_at": self.created_at,
            "lore_version": self.lore_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Provenance":
        return cls(
            namespace=str(data["namespace"]),
            machine_scope=str(data["machine_scope"]),
            created_at=str(data["created_at"]),
            lore_version=str(data["lore_version"]),
        )


@dataclass(frozen=True)
class PortableManifest:
    """The ``manifest.json`` contract of a portable bundle."""

    format_version: int
    provenance: Provenance
    schema_version: int
    expected_schema_version: int
    migrations: Tuple[Tuple[int, str], ...]
    counts: Dict[str, int]
    evidence: Dict[str, Any]
    members: Tuple[Member, ...]

    @property
    def created_at(self) -> str:
        return self.provenance.created_at

    @property
    def lore_version(self) -> str:
        return self.provenance.lore_version

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON form written into the bundle."""
        return {
            "format": PORTABLE_FORMAT,
            "format_version": self.format_version,
            "provenance": self.provenance.to_dict(),
            "schema": {
                "version": self.schema_version,
                "expected_version": self.expected_schema_version,
                "migrations": [
                    {"version": version, "description": description}
                    for version, description in self.migrations
                ],
            },
            "evidence": dict(self.evidence),
            "counts": dict(self.counts),
            "members": [
                {"name": member.name, "sha256": member.sha256, "size": member.size}
                for member in self.members
            ],
        }


@dataclass(frozen=True)
class PortableExport:
    """Where a new portable bundle landed, and the manifest it carries."""

    bundle: Path
    manifest: PortableManifest


@dataclass(frozen=True)
class PortableReport:
    """Outcome of :func:`verify_portable_bundle`; problems are collected."""

    path: Path
    ok: bool
    problems: Tuple[str, ...]
    manifest: Optional[PortableManifest]


@dataclass
class ImportReport:
    """What an import changed, and where the imported rows came from.

    Counters are separate for *added* and *kept/deduplicated* rows because the
    interesting question after importing an archive is what it did **not** touch.
    ``origins`` maps an originating ``source_namespace`` to the rows that came
    from it.
    """

    bundle: Path
    mode: str
    dry_run: bool
    manifest: PortableManifest
    revisions_added: int = 0
    revisions_deduplicated: int = 0
    artifacts_added: int = 0
    artifacts_deduplicated: int = 0
    artifacts_kept: int = 0
    sessions_added: int = 0
    sessions_copies: int = 0
    sessions_identical: int = 0
    sessions_messages_refilled: int = 0
    messages_added: int = 0
    tags_added: int = 0
    knowledge_added: int = 0
    summaries_added: int = 0
    memories_added: int = 0
    memories_identical: int = 0
    memory_tags_added: int = 0
    memory_feedback_added: int = 0
    evidence_links_added: int = 0
    evidence_links_deduplicated: int = 0
    tombstones_added: int = 0
    tombstones_skipped: int = 0
    health_added: int = 0
    health_updated: int = 0
    health_kept: int = 0
    origins: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def wrote_anything(self) -> bool:
        """True when the import changed the local store."""
        return any(
            getattr(self, name) > 0
            for name in (
                "revisions_added",
                "artifacts_added",
                "sessions_added",
                "sessions_copies",
                "sessions_messages_refilled",
                "messages_added",
                "tags_added",
                "knowledge_added",
                "summaries_added",
                "memories_added",
                "memory_tags_added",
                "memory_feedback_added",
                "evidence_links_added",
                "tombstones_added",
                "health_added",
                "health_updated",
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON form used by ``--json`` and the import log."""
        return {
            "bundle": str(self.bundle),
            "mode": self.mode,
            "dry_run": self.dry_run,
            "provenance": self.manifest.provenance.to_dict(),
            "revisions": {
                "added": self.revisions_added,
                "deduplicated": self.revisions_deduplicated,
            },
            "artifacts": {
                "added": self.artifacts_added,
                "deduplicated": self.artifacts_deduplicated,
                "kept_local": self.artifacts_kept,
            },
            "sessions": {
                "added": self.sessions_added,
                "copies": self.sessions_copies,
                "identical": self.sessions_identical,
                "messages_refilled": self.sessions_messages_refilled,
            },
            "messages_added": self.messages_added,
            "annotations": {
                "tags_added": self.tags_added,
                "knowledge_added": self.knowledge_added,
                "summaries_added": self.summaries_added,
            },
            "memory": {
                "added": self.memories_added,
                "identical": self.memories_identical,
                "tags_added": self.memory_tags_added,
                "feedback_added": self.memory_feedback_added,
                "evidence_links_added": self.evidence_links_added,
                "evidence_links_deduplicated": self.evidence_links_deduplicated,
            },
            "tombstones": {
                "added": self.tombstones_added,
                "skipped": self.tombstones_skipped,
            },
            "health": {
                "added": self.health_added,
                "updated": self.health_updated,
                "kept": self.health_kept,
            },
            "origins": {origin: dict(counts) for origin, counts in sorted(self.origins.items())},
        }


def create_portable_bundle(output_dir: Path, destination: Path) -> PortableExport:
    """Write a portable bundle of the store in ``output_dir`` to ``destination``.

    ``destination`` may be an existing directory, in which case the bundle is
    named ``lore-bundle-<UTC timestamp>.zip`` inside it. The source store is
    snapshotted through SQLite's online-backup API, so a writer running at the
    same time cannot leave a torn or stale copy, and the bundle is written to a
    temp file before being moved into place under its final name.
    """
    output_dir = Path(output_dir).expanduser()
    source = v2_db_path(output_dir)
    if not source.is_file():
        raise BackupError(f"no v2 store at {source} — nothing to export")

    destination = Path(destination).expanduser()
    if destination.is_dir():
        destination = destination / _default_bundle_name()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lore-bundle-") as workdir:
        snapshot = Path(workdir) / "index_v2.sqlite"
        _snapshot_database(source, snapshot)
        problems = _integrity_problems(snapshot)
        if problems:
            raise BackupError(f"the snapshot is not a usable store: {'; '.join(problems)}")
        schema_version, migrations = _read_schema(snapshot)
        manifest = PortableManifest(
            format_version=PORTABLE_FORMAT_VERSION,
            provenance=Provenance(
                namespace=_store_namespace(snapshot),
                machine_scope=_machine_scope(),
                created_at=_now(),
                lore_version=__version__,
            ),
            schema_version=schema_version,
            expected_schema_version=SCHEMA_VERSION,
            migrations=migrations,
            counts=_read_counts(snapshot),
            evidence=_evidence_summary(snapshot),
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
    return PortableExport(bundle=destination, manifest=manifest)


def verify_portable_bundle(bundle: Path) -> PortableReport:
    """Check a portable bundle without importing it.

    Covers the zip structure, the format marker, every member checksum and size,
    the evidence fingerprint (revision/artifact counts plus a digest over every
    captured checksum), the snapshot's ``PRAGMA integrity_check``, the row counts
    against the manifest and whether this Lore can read the bundle's schema at
    all. Every problem found is reported; nothing is raised and nothing is
    written outside a temp directory.
    """
    bundle = Path(bundle).expanduser()
    if not bundle.is_file():
        return PortableReport(bundle, False, (f"{bundle} does not exist",), None)

    try:
        archive = zipfile.ZipFile(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        return PortableReport(bundle, False, (f"not a zip bundle: {exc}",), None)

    with archive:
        names = archive.namelist()
        if MANIFEST_NAME not in names:
            return PortableReport(
                bundle, False, (f"{MANIFEST_NAME} is missing from the bundle",), None
            )
        try:
            manifest = _parse_manifest(json.loads(archive.read(MANIFEST_NAME)))
        except (KeyError, TypeError, ValueError) as exc:
            return PortableReport(bundle, False, (f"{MANIFEST_NAME} is unreadable: {exc}",), None)

        problems: List[str] = []
        if manifest.format_version > PORTABLE_FORMAT_VERSION:
            problems.append(
                f"bundle format {manifest.format_version} is newer than this Lore "
                f"supports ({PORTABLE_FORMAT_VERSION})"
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
        return PortableReport(bundle, not problems, tuple(problems), manifest)


def import_bundle(
    bundle: Path,
    output_dir: Path,
    *,
    mode: str = "merge",
    dry_run: bool = False,
) -> ImportReport:
    """Merge (or replace with) the archive in ``bundle``.

    The bundle is verified before the live store is opened, so a tampered or
    unreadable archive cannot write anything. ``mode="merge"`` keeps every local
    row and only adds what is missing (see the module docstring for the rules);
    ``mode="replace"`` installs the archived store as the live one, which is what
    a first import onto an empty machine wants. ``dry_run`` performs the merge
    against a temporary copy of the live store and reports the same counts.
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"unknown import mode {mode!r}; expected one of {IMPORT_MODES}")

    bundle = Path(bundle).expanduser()
    verified = verify_portable_bundle(bundle)
    if not verified.ok:
        raise BackupError(
            f"{bundle} failed verification, nothing was imported: " + "; ".join(verified.problems)
        )
    manifest = verified.manifest
    if manifest is None:  # pragma: no cover - ok implies a manifest
        raise BackupError(f"{bundle} has no manifest")

    output_dir = Path(output_dir).expanduser()
    store = v2_db_path(output_dir)
    report = ImportReport(bundle=bundle, mode=mode, dry_run=dry_run, manifest=manifest)
    bundle_sha = _sha256_file(bundle)

    with tempfile.TemporaryDirectory(prefix="lore-bundle-import-") as workdir:
        snapshot = Path(workdir) / "bundle.sqlite"
        _extract_member(bundle, DB_MEMBER, snapshot)

        if mode == "replace":
            _count_replaced_rows(report, snapshot)
            if not dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                _install_snapshot(snapshot, store)
                conn = open_v2_connection(store)
                try:
                    _record_import(conn, bundle_sha, report)
                finally:
                    conn.close()
            return report

        target_dir = output_dir
        if dry_run:
            # A dry run merges into a copy, so "writes nothing" is structural
            # rather than a promise every branch has to remember.
            target_dir = Path(workdir) / "dry-run"
            target_dir.mkdir()
            if store.is_file():
                _snapshot_database(store, v2_db_path(target_dir))
        _merge_into(target_dir, snapshot, report, bundle_sha=bundle_sha)
    return report


def _default_bundle_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"lore-bundle-{stamp}.zip"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _namespace_scope() -> str:
    """The host's source-namespace scope (``LORE_SOURCE_NAMESPACE`` or ``local``)."""
    return os.environ.get(NAMESPACE_ENV, "").strip() or "local"


def _store_namespace(snapshot: Path) -> str:
    """The namespace scope the store's own rows carry.

    Read from the rows rather than from the environment: a bundle exported later,
    on a host whose ``LORE_SOURCE_NAMESPACE`` has changed, must still say where its
    content actually came from. The environment is only a fallback for a store
    with no namespaced rows yet.
    """
    conn = sqlite3.connect(str(snapshot))
    try:
        row = conn.execute(
            "SELECT source_namespace, COUNT(*) FROM sessions "
            "WHERE source_namespace IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1"
        ).fetchone()
        if row is not None:
            return str(row[0]).split(":", 1)[-1]
        row = conn.execute("SELECT source_identity FROM source_revisions LIMIT 1").fetchone()
        if row is not None:
            return _namespace_of_identity(str(row[0])).split(":", 1)[-1]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return _namespace_scope()


def _machine_scope() -> str:
    """The machine the bundle was written on (hostname; no environment read)."""
    return socket.gethostname() or "unknown"


def _parse_manifest(data: Dict[str, Any]) -> PortableManifest:
    if data.get("format") != PORTABLE_FORMAT:
        raise ValueError(f"format marker {data.get('format')!r} is not {PORTABLE_FORMAT!r}")
    schema = data["schema"]
    return PortableManifest(
        format_version=int(data["format_version"]),
        provenance=Provenance.from_dict(data["provenance"]),
        schema_version=int(schema["version"]),
        expected_schema_version=int(schema["expected_version"]),
        migrations=tuple(
            (int(entry["version"]), str(entry["description"])) for entry in schema["migrations"]
        ),
        counts={str(table): int(count) for table, count in data["counts"].items()},
        evidence={str(key): value for key, value in data["evidence"].items()},
        members=tuple(
            Member(
                name=str(entry["name"]),
                sha256=str(entry["sha256"]),
                size=int(entry["size"]),
            )
            for entry in data["members"]
        ),
    )


def _evidence_summary(db_path: Path) -> Dict[str, Any]:
    """Count the raw evidence and fingerprint every captured checksum.

    The digest is the bundle's evidence identity: two bundles of the same sources
    agree on it even when their row ids, titles or annotations differ. It is
    recomputed on import, so an archive whose raw bytes were swapped fails
    verification even if its SQLite checksums still pass.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        revisions = conn.execute(
            "SELECT source_identity, content_sha256 FROM source_revisions "
            "ORDER BY source_identity, content_sha256"
        ).fetchall()
        artifacts = conn.execute(
            "SELECT r.source_identity, r.content_sha256, a.artifact_key, a.content_sha256 "
            "FROM source_artifacts a JOIN source_revisions r ON r.id = a.revision_id "
            "ORDER BY r.source_identity, r.content_sha256, a.artifact_key"
        ).fetchall()
    except sqlite3.Error:
        # A store from before migration 19 has no evidence tables at all.
        revisions, artifacts = [], []
    finally:
        conn.close()

    digest = hashlib.sha256()
    for row in revisions:
        digest.update(_evidence_line("revision", row))
    for row in artifacts:
        digest.update(_evidence_line("artifact", row))
    return {
        "source_revisions": len(revisions),
        "source_artifacts": len(artifacts),
        "digest": digest.hexdigest(),
    }


def _evidence_line(kind: str, row) -> bytes:
    joined = "\0".join(str(value) for value in row)
    return f"{kind}\0{joined}\n".encode("utf-8", "surrogatepass")


def _snapshot_problems(archive: zipfile.ZipFile, manifest: PortableManifest) -> List[str]:
    """Integrity, row-count, evidence and schema checks on the archived snapshot."""
    if DB_MEMBER not in {member.name for member in manifest.members}:
        return []
    with tempfile.TemporaryDirectory(prefix="lore-bundle-verify-") as workdir:
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
        supported = SCHEMA_VERSION
        if schema_version > supported:
            problems.append(
                f"the snapshot uses schema {schema_version}, newer than this Lore "
                f"supports ({supported}); upgrade Lore to import it"
            )

        counts = _read_counts(snapshot)
        for table, expected in manifest.counts.items():
            actual = counts.get(table)
            if actual is None:
                problems.append(f"the manifest lists table {table!r}, the snapshot has none")
            elif actual != expected:
                problems.append(f"{table} has {actual} rows, the manifest says {expected}")

        evidence = _evidence_summary(snapshot)
        for key in ("source_revisions", "source_artifacts", "digest"):
            expected = manifest.evidence.get(key)
            if expected != evidence[key]:
                problems.append(f"evidence {key} is {evidence[key]}, the manifest says {expected}")
        return problems


def _extract_member(bundle: Path, member: str, destination: Path) -> None:
    with zipfile.ZipFile(bundle) as archive, archive.open(member) as source:
        with open(destination, "wb") as target:
            shutil.copyfileobj(source, target)


def _merge_into(target_dir: Path, snapshot: Path, report: ImportReport, *, bundle_sha: str) -> None:
    """Merge the snapshot into the store at ``target_dir`` in one transaction."""
    conn = open_v2_connection(v2_db_path(target_dir))
    try:
        conn.execute("ATTACH DATABASE ? AS bundle", (str(snapshot),))
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _Merger(conn, report).run()
                if not report.dry_run:
                    _record_import(conn, bundle_sha, report)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.execute("DETACH DATABASE bundle")
    finally:
        conn.close()


class _Merger:
    """One merge of an attached portable snapshot into the live store.

    Everything happens in the caller's transaction: a failure leaves the store
    exactly as it was. Old ids are mapped to local ones as the merge proceeds,
    because a bundle's ``source_revisions.id``, ``sessions.id`` and ``memory.id``
    are only meaningful inside the archive they came from.
    """

    def __init__(self, conn: sqlite3.Connection, report: ImportReport) -> None:
        self.conn = conn
        self.report = report
        self.provenance = report.manifest.provenance
        self.revision_map: Dict[int, int] = {}
        self.session_map: Dict[str, str] = {}
        self.message_ids: Dict[Tuple[str, int], int] = {}
        self.memory_map: Dict[int, int] = {}
        self._parent_refs: List[Tuple[str, Optional[str]]] = []
        self._supersedes: List[Tuple[int, int]] = []
        self._origins: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"sessions": 0, "revisions": 0}
        )

    def run(self) -> None:
        self._revisions()
        self._artifacts()
        self._sessions()
        self._session_parents()
        self._annotations()
        self._memory()
        self._evidence_links()
        self._tombstones()
        self._source_health()
        self.report.origins = {origin: dict(counts) for origin, counts in self._origins.items()}

    # -- raw evidence -------------------------------------------------------

    def _revisions(self) -> None:
        rows = self.conn.execute(
            "SELECT id, source_identity, content_sha256, revision_seq FROM bundle.source_revisions "
            "ORDER BY id"
        ).fetchall()
        next_seq: Dict[str, int] = {}
        for row in rows:
            identity = row["source_identity"]
            existing = self.conn.execute(
                "SELECT id FROM main.source_revisions "
                "WHERE source_identity = ? AND content_sha256 = ?",
                (identity, row["content_sha256"]),
            ).fetchone()
            if existing is not None:
                self.revision_map[int(row["id"])] = int(existing["id"])
                self.report.revisions_deduplicated += 1
                continue

            current = next_seq.get(identity)
            if current is None:
                stored = self.conn.execute(
                    "SELECT COALESCE(MAX(revision_seq), 0) FROM main.source_revisions "
                    "WHERE source_identity = ?",
                    (identity,),
                ).fetchone()[0]
                current = int(stored)
                if current == 0:
                    # A source this store has never seen keeps the archive's
                    # numbering; an unchanged one continues where the live
                    # history stopped, so the sequence stays append-only.
                    current = max(1, int(row["revision_seq"])) - 1
            next_seq[identity] = current + 1

            cursor = self.conn.execute(
                """
                INSERT INTO main.source_revisions (
                    source_identity, tool, source_path, content_sha256, byte_size,
                    encoding, content, source_mtime_ns, source_inode, revision_seq,
                    captured_at, decode_status, decode_note, media_type
                )
                SELECT source_identity, tool, source_path, content_sha256, byte_size,
                       encoding, content, source_mtime_ns, source_inode, ?,
                       captured_at, decode_status, decode_note, media_type
                FROM bundle.source_revisions WHERE id = ?
                """,
                (next_seq[identity], int(row["id"])),
            )
            self.revision_map[int(row["id"])] = int(cursor.lastrowid)
            self.report.revisions_added += 1
            self._origin(_namespace_of_identity(identity))["revisions"] += 1

    def _artifacts(self) -> None:
        rows = self.conn.execute(
            "SELECT id, revision_id, artifact_key, content_sha256 FROM bundle.source_artifacts "
            "ORDER BY id"
        ).fetchall()
        for row in rows:
            revision_id = self.revision_map.get(int(row["revision_id"]))
            if revision_id is None:
                continue
            existing = self.conn.execute(
                "SELECT content_sha256 FROM main.source_artifacts "
                "WHERE revision_id = ? AND artifact_key = ?",
                (revision_id, row["artifact_key"]),
            ).fetchone()
            if existing is not None:
                if existing["content_sha256"] == row["content_sha256"]:
                    self.report.artifacts_deduplicated += 1
                else:
                    # A key is a slot inside one revision; the local bytes win and
                    # the divergence is reported instead of overwritten.
                    self.report.artifacts_kept += 1
                continue
            self.conn.execute(
                """
                INSERT INTO main.source_artifacts (
                    revision_id, artifact_key, content_sha256, byte_size, encoding,
                    content, media_type, decode_status, decode_note, captured_at
                )
                SELECT ?, artifact_key, content_sha256, byte_size, encoding,
                       content, media_type, decode_status, decode_note, captured_at
                FROM bundle.source_artifacts WHERE id = ?
                """,
                (revision_id, int(row["id"])),
            )
            self.report.artifacts_added += 1

    # -- sessions -----------------------------------------------------------

    def _sessions(self) -> None:
        columns = _shared_columns(self.conn, "sessions", _SESSION_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {', '.join(columns)} FROM bundle.sessions ORDER BY created, id"
        ).fetchall()
        for row in rows:
            self._import_session(row, columns)

    def _import_session(self, row, columns: Tuple[str, ...]) -> None:
        bundle_id = str(row["id"])
        record_id = str(row["source_record_id"] or bundle_id)
        namespace = str(row["source_namespace"] or f"{row['tool']}:{self.provenance.namespace}")
        messages = self.conn.execute(
            "SELECT role, kind, content, timestamp, model, native_id, tool_calls_json "
            "FROM bundle.messages WHERE session_id = ? ORDER BY seq",
            (bundle_id,),
        ).fetchall()
        digest = str(row["content_sha256"] or content_hash_from_rows(row, messages))

        # A source identity that is already here under another local id (a copy
        # stored by an earlier import of this bundle) is not new content.
        twin = self.conn.execute(
            "SELECT id FROM main.sessions WHERE content_sha256 = ? "
            "ORDER BY copy_of_session_id IS NOT NULL, id LIMIT 1",
            (digest,),
        ).fetchone()
        if twin is not None:
            self.session_map[bundle_id] = str(twin["id"])
            self.report.sessions_identical += 1
            self._remember_message_ids(bundle_id, str(twin["id"]))
            return

        store_id, copy_of = resolve_store_identity(
            self.conn,
            record_id=record_id,
            tool=str(row["tool"]),
            namespace=namespace,
            content_sha256=digest,
        )
        existing = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM main.messages WHERE session_id = sessions.id) AS present "
            "FROM main.sessions WHERE id = ?",
            (store_id,),
        ).fetchone()

        if existing is not None and int(existing["present"]) > 0:
            self.session_map[bundle_id] = store_id
            self.report.sessions_identical += 1
            self._remember_message_ids(bundle_id, store_id)
            return

        if existing is not None:
            # A metadata-only local row: the archive is the only source of its
            # messages, so filling them in adds evidence without replacing any.
            self.session_map[bundle_id] = store_id
            self._copy_messages(bundle_id, store_id)
            self._index_session(store_id, str(row["tool"]), row["project"], row["title"])
            self.report.sessions_messages_refilled += 1
            return

        values = {column: row[column] for column in columns}
        values.update(
            id=store_id,
            source_revision_id=(
                self.revision_map.get(int(values["source_revision_id"]))
                if values.get("source_revision_id") is not None
                else None
            ),
            source_namespace=namespace,
            source_record_id=record_id,
            content_sha256=digest,
            copy_of_session_id=copy_of,
        )
        names = tuple(values)
        self.conn.execute(
            f"INSERT INTO main.sessions ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            tuple(values[name] for name in names),
        )
        self.session_map[bundle_id] = store_id
        self._copy_messages(bundle_id, store_id)
        self._index_session(store_id, str(row["tool"]), row["project"], row["title"])
        if copy_of is None:
            self.report.sessions_added += 1
        else:
            self.report.sessions_copies += 1
        self._origin(namespace)["sessions"] += 1
        if values.get("parent_session_id"):
            self._parent_refs.append((store_id, str(values["parent_session_id"])))

    def _session_parents(self) -> None:
        for store_id, bundle_parent in self._parent_refs:
            mapped = self.session_map.get(bundle_parent)
            if mapped and mapped != bundle_parent:
                self.conn.execute(
                    "UPDATE main.sessions SET parent_session_id = ? WHERE id = ?",
                    (mapped, store_id),
                )

    def _copy_messages(self, bundle_id: str, store_id: str) -> None:
        columns = _shared_columns(self.conn, "messages", _MESSAGE_COLUMNS)
        cursor = self.conn.execute(
            f"INSERT INTO main.messages (session_id, {', '.join(columns)}) "
            f"SELECT ?, {', '.join(columns)} FROM bundle.messages "
            "WHERE session_id = ? ORDER BY seq",
            (store_id, bundle_id),
        )
        self.report.messages_added += max(0, cursor.rowcount)
        self._remember_message_ids(bundle_id, store_id)

    def _remember_message_ids(self, bundle_id: str, store_id: str) -> None:
        """Map ``(bundle session, seq)`` to the local ``messages.id``.

        ``message_id`` is a volatile local rowid, so an evidence link from the
        archive can only be resolved through the identity that travels with it.
        """
        rows = self.conn.execute(
            "SELECT id, seq FROM main.messages WHERE session_id = ?", (store_id,)
        ).fetchall()
        for row in rows:
            self.message_ids[(bundle_id, int(row["seq"]))] = int(row["id"])

    def _index_session(
        self, session_id: str, tool: str, project: Optional[str], title: Optional[str]
    ) -> None:
        body_parts = [title or ""]
        body_parts.extend(
            content
            for (content,) in self.conn.execute(
                "SELECT content FROM main.messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
            if content
        )
        body = "\n".join(part for part in body_parts if part)
        self.conn.execute(
            "DELETE FROM main.search_index WHERE entity_type = 'session' AND entity_id = ?",
            (session_id,),
        )
        self.conn.execute(
            "INSERT INTO main.search_index (entity_type, entity_id, tool, project, title, body) "
            "VALUES ('session', ?, ?, ?, ?, ?)",
            (session_id, tool, project or "", title or "", body),
        )

    # -- annotations --------------------------------------------------------

    def _annotations(self) -> None:
        for row in self.conn.execute(
            "SELECT session_id, tag, created FROM bundle.session_tags"
        ).fetchall():
            target = self.session_map.get(str(row["session_id"]))
            if target is None:
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO main.session_tags (session_id, tag, created) "
                "VALUES (?, ?, ?)",
                (target, row["tag"], row["created"]),
            )
            self.report.tags_added += max(0, cursor.rowcount)

        for row in self.conn.execute(
            "SELECT session_id, fingerprint, model, prompt_version, payload_json, created "
            "FROM bundle.session_knowledge"
        ).fetchall():
            target = self.session_map.get(str(row["session_id"]))
            if target is None:
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO main.session_knowledge "
                "(session_id, fingerprint, model, prompt_version, payload_json, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    target,
                    row["fingerprint"],
                    row["model"],
                    row["prompt_version"],
                    row["payload_json"],
                    row["created"],
                ),
            )
            self.report.knowledge_added += max(0, cursor.rowcount)

        for row in self.conn.execute(
            "SELECT level, scope_key, session_id, fingerprint, model, prompt_version, "
            "compressor_version, payload_json, input_tokens, output_tokens, created "
            "FROM bundle.session_summaries"
        ).fetchall():
            session_id = row["session_id"]
            target = self.session_map.get(str(session_id), session_id) if session_id else None
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO main.session_summaries "
                "(level, scope_key, session_id, fingerprint, model, prompt_version, "
                "compressor_version, payload_json, input_tokens, output_tokens, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["level"],
                    row["scope_key"],
                    target,
                    row["fingerprint"],
                    row["model"],
                    row["prompt_version"],
                    row["compressor_version"],
                    row["payload_json"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["created"],
                ),
            )
            self.report.summaries_added += max(0, cursor.rowcount)

    # -- memory -------------------------------------------------------------

    def _memory(self) -> None:
        rows = self.conn.execute(
            "SELECT id, kind, title, body, scope_project, scope_tool, author, created, updated, "
            "superseded_by, metadata_json, status FROM bundle.memory ORDER BY id"
        ).fetchall()
        for row in rows:
            existing = self.conn.execute(
                "SELECT id FROM main.memory WHERE kind = ? AND title = ? AND body = ? "
                "AND scope_project IS ? AND scope_tool IS ? AND created = ?",
                (
                    row["kind"],
                    row["title"],
                    row["body"],
                    row["scope_project"],
                    row["scope_tool"],
                    row["created"],
                ),
            ).fetchone()
            if existing is not None:
                self.memory_map[int(row["id"])] = int(existing["id"])
                self.report.memories_identical += 1
                continue
            cursor = self.conn.execute(
                "INSERT INTO main.memory (kind, title, body, scope_project, scope_tool, author, "
                "created, updated, superseded_by, metadata_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    row["kind"],
                    row["title"],
                    row["body"],
                    row["scope_project"],
                    row["scope_tool"],
                    row["author"],
                    row["created"],
                    row["updated"],
                    row["metadata_json"],
                    row["status"],
                ),
            )
            memory_id = int(cursor.lastrowid)
            self.memory_map[int(row["id"])] = memory_id
            self.report.memories_added += 1
            self.conn.execute(
                "INSERT INTO main.search_index (entity_type, entity_id, tool, project, title, body) "
                "VALUES ('memory', ?, ?, ?, ?, ?)",
                (
                    str(memory_id),
                    row["scope_tool"] or "",
                    row["scope_project"] or "",
                    row["title"],
                    row["body"],
                ),
            )
            if row["superseded_by"] is not None:
                self._supersedes.append((memory_id, int(row["superseded_by"])))

        # A supersede chain is only meaningful once both rows exist locally.
        for memory_id, bundle_target in self._supersedes:
            target = self.memory_map.get(bundle_target)
            if target is not None:
                self.conn.execute(
                    "UPDATE main.memory SET superseded_by = ? WHERE id = ?",
                    (target, memory_id),
                )

        for row in self.conn.execute("SELECT memory_id, tag FROM bundle.memory_tags").fetchall():
            target = self.memory_map.get(int(row["memory_id"]))
            if target is None:
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO main.memory_tags (memory_id, tag) VALUES (?, ?)",
                (target, row["tag"]),
            )
            self.report.memory_tags_added += max(0, cursor.rowcount)

        for row in self.conn.execute(
            "SELECT memory_id, rater, score, created FROM bundle.memory_feedback"
        ).fetchall():
            target = self.memory_map.get(int(row["memory_id"]))
            if target is None:
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO main.memory_feedback (memory_id, rater, score, created) "
                "VALUES (?, ?, ?, ?)",
                (target, row["rater"], row["score"], row["created"]),
            )
            self.report.memory_feedback_added += max(0, cursor.rowcount)

    def _evidence_links(self) -> None:
        """Import the memory→evidence links, resolving every id locally.

        A link carries the stable identity of its message (``message_seq`` or
        ``message_native_id``) precisely so it survives a move between stores;
        the archive's own ``message_id`` rowid is discarded.
        """
        if not self.memory_map:
            return
        columns = _shared_columns(self.conn, "memory_sources", _EVIDENCE_LINK_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {', '.join(columns)} FROM bundle.memory_sources"
        ).fetchall()
        for row in rows:
            link = {column: row[column] for column in columns}
            memory_id = self.memory_map.get(int(link["memory_id"]))
            if memory_id is None:
                continue
            bundle_session = link.get("session_id")
            session_id = self.session_map.get(str(bundle_session), bundle_session)
            message_id = self._resolve_message_id(str(bundle_session), session_id, link)
            revision = link.get("source_revision_id")
            source_revision_id = self.revision_map.get(int(revision)) if revision else None

            existing = self.conn.execute(
                "SELECT 1 FROM main.memory_sources WHERE memory_id = ? AND session_id IS ? "
                "AND message_id IS ? AND message_seq IS ? AND message_native_id IS ? "
                "AND message_sha256 IS ?",
                (
                    memory_id,
                    session_id,
                    message_id,
                    link.get("message_seq"),
                    link.get("message_native_id"),
                    link.get("message_sha256"),
                ),
            ).fetchone()
            if existing is not None:
                self.report.evidence_links_deduplicated += 1
                continue
            self.conn.execute(
                "INSERT INTO main.memory_sources (memory_id, session_id, message_id, note, "
                "message_seq, message_native_id, message_sha256, source_revision_id, "
                "session_namespace) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    session_id,
                    message_id,
                    link.get("note"),
                    link.get("message_seq"),
                    link.get("message_native_id"),
                    link.get("message_sha256"),
                    source_revision_id,
                    link.get("session_namespace"),
                ),
            )
            self.report.evidence_links_added += 1

    def _resolve_message_id(
        self, bundle_session: str, session_id: Optional[str], link: Dict[str, Any]
    ) -> Optional[int]:
        seq = link.get("message_seq")
        if seq is not None:
            mapped = self.message_ids.get((bundle_session, int(seq)))
            if mapped is not None:
                return mapped
        native_id = link.get("message_native_id")
        if native_id and session_id:
            row = self.conn.execute(
                "SELECT id FROM main.messages WHERE session_id = ? AND native_id = ? "
                "ORDER BY seq LIMIT 1",
                (session_id, native_id),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        return None

    # -- deletion and health ------------------------------------------------

    def _tombstones(self) -> None:
        for row in self.conn.execute(
            "SELECT session_id, deleted_at, reason FROM bundle.deleted_sessions"
        ).fetchall():
            session_id = str(row["session_id"])
            if (
                self.session_map.get(session_id)
                or self.conn.execute(
                    "SELECT 1 FROM main.sessions WHERE id = ?", (session_id,)
                ).fetchone()
            ):
                # The archive says deleted; this store holds the row. Local
                # content is never deleted implicitly.
                self.report.tombstones_skipped += 1
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO main.deleted_sessions (session_id, deleted_at, reason) "
                "VALUES (?, ?, ?)",
                (session_id, row["deleted_at"], row["reason"]),
            )
            self.report.tombstones_added += max(0, cursor.rowcount)

    def _source_health(self) -> None:
        """Merge per-source health; the local counters are never overwritten.

        Counters are observations made on one host, so importing another host's
        numbers would corrupt them. The *state* is not additive, and a newer
        capture is worth knowing, so a newer ``last_captured_at`` updates the
        state of an identity both stores tracked.
        """
        rows = self.conn.execute(
            "SELECT source_identity, tool, source_path, state, sessions_captured, "
            "sessions_filtered, sessions_unsupported, sessions_failed, last_captured_at, "
            "last_error, detail_json FROM bundle.source_health"
        ).fetchall()
        for row in rows:
            existing = self.conn.execute(
                "SELECT last_captured_at FROM main.source_health WHERE source_identity = ?",
                (row["source_identity"],),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO main.source_health (source_identity, tool, source_path, state, "
                    "sessions_captured, sessions_filtered, sessions_unsupported, sessions_failed, "
                    "last_captured_at, last_error, detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["source_identity"],
                        row["tool"],
                        row["source_path"],
                        row["state"],
                        row["sessions_captured"],
                        row["sessions_filtered"],
                        row["sessions_unsupported"],
                        row["sessions_failed"],
                        row["last_captured_at"],
                        row["last_error"],
                        row["detail_json"],
                    ),
                )
                self.report.health_added += 1
                continue
            newer = str(row["last_captured_at"] or "") > str(existing["last_captured_at"] or "")
            if newer:
                self.conn.execute(
                    "UPDATE main.source_health SET state = ?, last_error = ?, "
                    "last_captured_at = ?, detail_json = ? WHERE source_identity = ?",
                    (
                        row["state"],
                        row["last_error"],
                        row["last_captured_at"],
                        row["detail_json"],
                        row["source_identity"],
                    ),
                )
                self.report.health_updated += 1
            else:
                self.report.health_kept += 1

    def _origin(self, namespace: str) -> Dict[str, int]:
        return self._origins[namespace]


def _record_import(conn: sqlite3.Connection, bundle_sha: str, report: ImportReport) -> None:
    """Append one row to ``bundle_imports``; the log is never updated in place."""
    conn.execute(
        "INSERT INTO main.bundle_imports (bundle_sha256, format_version, mode, imported_at, "
        "provenance_json, counts_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            bundle_sha,
            report.manifest.format_version,
            report.mode,
            _now(),
            json.dumps(report.manifest.provenance.to_dict(), sort_keys=True),
            json.dumps(report.to_dict(), sort_keys=True),
        ),
    )


def _install_snapshot(snapshot: Path, store: Path) -> None:
    """Put the archived store in place of the live one, atomically."""
    if store.exists():
        _discard_store(store)
    temp_path = store.with_name(f".{store.name}.import-{uuid4().hex}")
    try:
        shutil.copyfile(snapshot, temp_path)
        os.replace(temp_path, store)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    restrict_file(store)


def _count_replaced_rows(report: ImportReport, snapshot: Path) -> None:
    """Fill the report for ``--replace`` from the archive's own row counts.

    Nothing is merged in that mode: the archive *becomes* the store, so every row
    it carries is a row the store now holds.
    """
    counts = _read_counts(snapshot)
    report.sessions_added = counts.get("sessions", 0)
    report.messages_added = counts.get("messages", 0)
    report.tags_added = counts.get("session_tags", 0)
    report.knowledge_added = counts.get("session_knowledge", 0)
    report.summaries_added = counts.get("session_summaries", 0)
    report.memories_added = counts.get("memory", 0)
    report.memory_tags_added = counts.get("memory_tags", 0)
    report.memory_feedback_added = counts.get("memory_feedback", 0)
    report.evidence_links_added = counts.get("memory_sources", 0)
    report.tombstones_added = counts.get("deleted_sessions", 0)
    report.health_added = _table_count(snapshot, "source_health")
    evidence = _evidence_summary(snapshot)
    report.revisions_added = int(evidence["source_revisions"])
    report.artifacts_added = int(evidence["source_artifacts"])
    report.origins = _snapshot_origins(snapshot)


def _snapshot_origins(snapshot: Path) -> Dict[str, Dict[str, int]]:
    """Count the archive's sessions and revisions per originating namespace."""
    origins: Dict[str, Dict[str, int]] = defaultdict(lambda: {"sessions": 0, "revisions": 0})
    conn = sqlite3.connect(str(snapshot))
    try:
        for namespace, count in conn.execute(
            "SELECT COALESCE(source_namespace, '(unrecorded)'), COUNT(*) FROM sessions GROUP BY 1"
        ):
            origins[str(namespace)]["sessions"] += int(count)
        for identity, count in conn.execute(
            "SELECT source_identity, COUNT(*) FROM source_revisions GROUP BY source_identity"
        ):
            origins[_namespace_of_identity(str(identity))]["revisions"] += int(count)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return {origin: dict(counts) for origin, counts in origins.items()}


def _table_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None:
            return 0
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        conn.close()


def _namespace_of_identity(source_identity: str) -> str:
    """``<tool>:<namespace>`` of an identity like ``<tool>:<namespace>:<path>``.

    The same spelling a session row carries in ``source_namespace``, so an import
    report groups revisions and sessions of one origin under one key.
    """
    parts = source_identity.split(":", 2)
    if len(parts) > 2 and parts[1]:
        return f"{parts[0]}:{parts[1]}"
    return "local"


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> Set[str]:
    """Columns of ``schema.table`` (empty when the table does not exist)."""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({table})")}
    except sqlite3.Error:
        return set()


def _shared_columns(
    conn: sqlite3.Connection, table: str, columns: Tuple[str, ...]
) -> Tuple[str, ...]:
    """The column subset both the archive and the live store have.

    An archive written by a newer Lore may carry columns this store lacks; copying
    only what both sides have keeps an older importer working instead of failing
    on a column it has never seen.
    """
    bundle_columns = _table_columns(conn, "bundle", table)
    main_columns = _table_columns(conn, "main", table)
    return tuple(
        column for column in columns if column in bundle_columns and column in main_columns
    )
