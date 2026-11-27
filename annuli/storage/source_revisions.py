"""Immutable source revisions — the raw evidence behind every parsed session (#151).

An extractor reads provider files and turns them into ``UnifiedSession`` objects.
If a parser misses an event, or a provider rewrites a transcript, the original
input used to be gone. This module stores that input: one row per
``(source identity, content checksum)`` with the exact bytes, its revision number
and its decode status. Writes are append-only — a new checksum becomes a new
revision, never an overwrite.

Contract: ``docs/superpowers/specs/2026-10-26-source-revision-and-identity-design.md``.
"""

from __future__ import annotations

import hashlib
import logging
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: Decode statuses. ``stale`` means the bytes stored are not provably the bytes
#: the linked session was parsed from (the file changed in between).
DECODE_STATUSES = (
    "parsed",
    "partial",
    "unknown",
    "malformed",
    "stale",
    "unparsed",
)

#: Exact file names that are never captured (credentials and provider config).
CREDENTIAL_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        ".env",
        ".envrc",
        "auth.json",
        "tokens.json",
        "token.json",
        "cookies",
        "cookies.json",
        "keychain.json",
        "secrets.json",
    }
)

#: File-name prefixes that are never captured.
CREDENTIAL_PREFIXES = (".env.", "id_rsa", "id_ed25519", "id_ecdsa")

#: File-name suffixes that are never captured.
CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

#: Substrings that mark a file as credential material regardless of suffix.
CREDENTIAL_MARKERS = ("credential", "secret", "password", "apikey", "api_key")

_MEDIA_TYPES = {
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".txt": "text",
    ".log": "text",
    ".pb": "protobuf",
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".vscdb": "sqlite",
    ".csv": "csv",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a capture attempt.

    ``revision_id`` is ``None`` when nothing was stored; ``status`` then explains
    why (``excluded`` for credential material, ``unavailable`` for a missing or
    unreadable source).
    """

    status: str
    revision_id: Optional[int] = None
    created: bool = False
    decode_status: str = "parsed"
    content_sha256: Optional[str] = None


def is_credential_source(path: str | Path) -> bool:
    """Return True when ``path`` is credential or provider-config material.

    The rule is name-based and content-independent: a capture path must never
    need to open a file to decide whether it may store it.
    """
    name = Path(path).name.lower()
    if not name:
        return False
    if name in CREDENTIAL_NAMES or name.startswith(CREDENTIAL_PREFIXES):
        return True
    if name.endswith(CREDENTIAL_SUFFIXES):
        return True
    return any(marker in name for marker in CREDENTIAL_MARKERS)


def media_type_for(path: str | Path) -> str:
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "binary")


def source_identity_for(tool: str, source_path: str | Path, namespace: str = "local") -> str:
    """Stable identity of a source file, independent of its content.

    ``<tool>:<namespace>:<absolute path>`` — the namespace separates machines or
    home scopes that happen to use the same path.
    """
    return f"{tool}:{namespace}:{Path(source_path).expanduser().absolute()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _captured_mtime_is_current(
    conn, source_identity: str, mtime_ns: int, byte_size: int
) -> Optional[int]:
    """Return the revision id of an identical ``(mtime, size)`` capture, if any.

    This is the cost guard for large archives: re-syncing a thousand unchanged
    files must not re-read and re-hash them. The authoritative dedupe key stays
    the checksum, so a miss only costs one extra read.
    """
    row = conn.execute(
        "SELECT id, source_mtime_ns, byte_size FROM source_revisions "
        "WHERE source_identity = ? ORDER BY revision_seq DESC LIMIT 1",
        (source_identity,),
    ).fetchone()
    if row is None:
        return None
    if int(row["byte_size"]) != int(byte_size):
        return None
    if row["source_mtime_ns"] is None or int(row["source_mtime_ns"]) != int(mtime_ns):
        return None
    return int(row["id"])


def capture_source_revision(
    conn,
    *,
    tool: str,
    source_path: str | Path,
    content: bytes,
    decode_status: str = "parsed",
    decode_note: Optional[str] = None,
    source_mtime_ns: Optional[int] = None,
    source_inode: Optional[int] = None,
    source_size: Optional[int] = None,
    namespace: str = "local",
    force_checksum: bool = False,
) -> CaptureResult:
    """Store ``content`` as a revision of ``source_path`` (idempotent by checksum).

    Returns the existing revision when the same bytes were captured before.
    ``force_checksum`` bypasses the ``(mtime, size)`` cache and hashes the bytes.
    """
    path = Path(source_path).expanduser()
    if is_credential_source(path):
        # Never store, never log the name's content — only the decision.
        return CaptureResult(status="excluded")

    identity = source_identity_for(tool, path, namespace)
    if not force_checksum and source_mtime_ns is not None and source_size is not None:
        cached = _captured_mtime_is_current(conn, identity, source_mtime_ns, source_size)
        if cached is not None:
            return CaptureResult(
                status="reused",
                revision_id=cached,
                created=False,
                decode_status=decode_status,
            )

    if decode_status not in DECODE_STATUSES:
        raise ValueError(f"unknown decode status: {decode_status!r}")

    digest = _checksum(content)
    existing = conn.execute(
        "SELECT id, decode_status FROM source_revisions "
        "WHERE source_identity = ? AND content_sha256 = ?",
        (identity, digest),
    ).fetchone()
    if existing is not None:
        return CaptureResult(
            status="reused",
            revision_id=int(existing["id"]),
            created=False,
            decode_status=str(existing["decode_status"]),
            content_sha256=digest,
        )

    next_seq = conn.execute(
        "SELECT COALESCE(MAX(revision_seq), 0) + 1 FROM source_revisions WHERE source_identity = ?",
        (identity,),
    ).fetchone()[0]
    compressed = zlib.compress(content, 6)
    cursor = conn.execute(
        """
        INSERT INTO source_revisions (
            source_identity, tool, source_path, content_sha256, byte_size,
            encoding, content, source_mtime_ns, source_inode, revision_seq,
            captured_at, decode_status, decode_note, media_type
        ) VALUES (?, ?, ?, ?, ?, 'zlib', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity,
            tool,
            str(path.absolute()),
            digest,
            len(content),
            compressed,
            source_mtime_ns,
            source_inode,
            int(next_seq),
            _now(),
            decode_status,
            decode_note,
            media_type_for(path),
        ),
    )
    return CaptureResult(
        status="created",
        revision_id=int(cursor.lastrowid),
        created=True,
        decode_status=decode_status,
        content_sha256=digest,
    )


def capture_session_source(
    conn,
    *,
    tool: str,
    source_path: Optional[str | Path],
    parsed_mtime_ns: Optional[int] = None,
    namespace: str = "local",
) -> CaptureResult:
    """Capture the file a parsed session came from, flagging a stale window.

    A missing/unreadable file is ``unavailable`` (never fatal). A file whose mtime
    no longer matches the mtime the extractor saw is stored with
    ``decode_status='stale'`` and a note: the bytes are evidence, but they are not
    provably the bytes the session was parsed from.
    """
    if not source_path:
        return CaptureResult(status="unavailable", decode_status="unparsed")
    path = Path(source_path).expanduser()
    if is_credential_source(path):
        return CaptureResult(status="excluded", decode_status="unparsed")
    try:
        stat = path.stat()
        content = path.read_bytes()
    except OSError as exc:
        logger.debug("Source capture unavailable for %s: %s", path, exc)
        return CaptureResult(status="unavailable", decode_status="unparsed")

    status = "parsed"
    note = None
    if parsed_mtime_ns is not None and int(parsed_mtime_ns) != stat.st_mtime_ns:
        status = "stale"
        note = f"parsed mtime_ns={parsed_mtime_ns}, captured mtime_ns={stat.st_mtime_ns}"

    return capture_source_revision(
        conn,
        tool=tool,
        source_path=path,
        content=content,
        decode_status=status,
        decode_note=note,
        source_mtime_ns=stat.st_mtime_ns,
        source_inode=stat.st_ino,
        source_size=stat.st_size,
        namespace=namespace,
    )


def read_source_revision(conn, revision_id: int) -> Optional[bytes]:
    """Return the exact bytes captured for ``revision_id`` (``None`` if unknown)."""
    row = conn.execute(
        "SELECT content, encoding FROM source_revisions WHERE id = ?", (revision_id,)
    ).fetchone()
    if row is None:
        return None
    if row["encoding"] == "zlib":
        return zlib.decompress(row["content"])
    return bytes(row["content"])


def get_source_revision(conn, revision_id: int) -> Optional[dict]:
    """Metadata of one revision (no content)."""
    row = conn.execute(
        "SELECT id, source_identity, tool, source_path, content_sha256, byte_size, "
        "encoding, source_mtime_ns, source_inode, revision_seq, captured_at, "
        "decode_status, decode_note, media_type FROM source_revisions WHERE id = ?",
        (revision_id,),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def revisions_for_source(conn, source_identity: str) -> list[dict]:
    """All revisions of one source, oldest first (no content)."""
    rows = conn.execute(
        "SELECT id, source_identity, tool, source_path, content_sha256, byte_size, "
        "source_mtime_ns, source_inode, revision_seq, captured_at, decode_status, "
        "decode_note, media_type FROM source_revisions "
        "WHERE source_identity = ? ORDER BY revision_seq",
        (source_identity,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def revision_for_session(conn, session_id: str) -> Optional[dict]:
    """The captured revision a session row points at, if any."""
    row = conn.execute(
        "SELECT source_revision_id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or row["source_revision_id"] is None:
        return None
    return get_source_revision(conn, int(row["source_revision_id"]))


def capture_artifact(
    conn,
    *,
    revision_id: int,
    artifact_key: str,
    content: bytes,
    media_type: Optional[str] = None,
    decode_status: str = "unparsed",
    decode_note: Optional[str] = None,
) -> int:
    """Attach an artifact (attachment, undecodable blob) to a revision.

    Idempotent per ``(revision_id, artifact_key)``: an identical re-capture keeps
    the existing row, changed bytes replace the artifact of that key (a key is a
    slot inside an immutable revision, not a new revision).
    """
    digest = _checksum(content)
    row = conn.execute(
        "SELECT id, content_sha256 FROM source_artifacts WHERE revision_id = ? AND artifact_key = ?",
        (revision_id, artifact_key),
    ).fetchone()
    if row is not None:
        if row["content_sha256"] == digest:
            return int(row["id"])
        conn.execute(
            "UPDATE source_artifacts SET content_sha256 = ?, byte_size = ?, content = ?, "
            "media_type = ?, decode_status = ?, decode_note = ?, captured_at = ? WHERE id = ?",
            (
                digest,
                len(content),
                zlib.compress(content, 6),
                media_type,
                decode_status,
                decode_note,
                _now(),
                int(row["id"]),
            ),
        )
        return int(row["id"])

    cursor = conn.execute(
        """
        INSERT INTO source_artifacts (
            revision_id, artifact_key, content_sha256, byte_size, encoding,
            content, media_type, decode_status, decode_note, captured_at
        ) VALUES (?, ?, ?, ?, 'zlib', ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            artifact_key,
            digest,
            len(content),
            zlib.compress(content, 6),
            media_type,
            decode_status,
            decode_note,
            _now(),
        ),
    )
    return int(cursor.lastrowid)


def artifacts_for_revision(conn, revision_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, artifact_key, content_sha256, byte_size, media_type, decode_status, "
        "decode_note, captured_at FROM source_artifacts WHERE revision_id = ? "
        "ORDER BY artifact_key",
        (revision_id,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def undecoded_revisions(conn, limit: int = 200) -> list[dict]:
    """Revisions whose decode status says the parser did not fully understand them."""
    rows = conn.execute(
        "SELECT id, source_identity, tool, source_path, revision_seq, decode_status, "
        "decode_note, captured_at FROM source_revisions "
        "WHERE decode_status IN ('partial', 'unknown', 'malformed', 'unparsed') "
        "ORDER BY captured_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def capture_many(
    conn,
    tool: str,
    paths: Iterable[str | Path],
    *,
    namespace: str = "local",
) -> list[CaptureResult]:
    """Capture a batch of source files (best effort, never fatal)."""
    results: list[CaptureResult] = []
    for path in paths:
        results.append(
            capture_session_source(conn, tool=tool, source_path=path, namespace=namespace)
        )
    return results
