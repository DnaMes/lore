# Design — source revisions, canonical messages and session identity

Status: accepted, implemented incrementally (2026-10-26 backlog sweep).

This design covers the four issues of the "capture and identity spine" that the
archived plan sequences as spec-first, migration-carrying work:

- #151 capture immutable source revisions before parsing
- #154 round-trip structured messages through SQLite without information loss
- #155 preserve divergent session copies and namespace source identities
- #160 keep message evidence references stable across reindexing

It exists because the sweep ledger (`docs/triage/LEDGER.md`) refuses to ship a
schema migration without a written contract for what it stores, and because
each of these issues otherwise ends up half-implemented in a different way.

## Invariants

1. **Nothing that was read is lost.** Every byte handed to an extractor is
   recoverable from the store after the provider file is deleted or rewritten.
2. **Writes are append-only where identity is ambiguous.** A new revision, a
   second copy or a divergent history adds rows; it never overwrites the only
   copy of an earlier one.
3. **Interpretation is versioned.** Raw bytes, parsed messages and derived
   artefacts (summaries, embeddings, claims) each carry the version of the
   parser/schema that produced them, so a newer parser can re-read old data.
4. **Credentials stay out.** The capture boundary refuses credential and
   configuration files by name and by content-independent rule; no token,
   cookie or key file is ever stored.
5. **Old rows stay readable.** Every migration keeps legacy rows readable and
   never pretends a legacy row is complete.

## 1 — Source revisions (#151)

Table `source_revisions` (migration 19):

| column | meaning |
| --- | --- |
| `id` | rowid, the value `sessions.source_revision_id` points at |
| `source_identity` | stable identity of the source: `<tool>:<namespace>:<absolute path>` |
| `tool` | provider tool id (`claude-code`, `codex`, …) |
| `source_path` | absolute path at capture time |
| `content_sha256` | sha256 of the **uncompressed** bytes |
| `byte_size` | uncompressed size |
| `encoding` | `zlib` (always, so the archive stays compact) |
| `content` | the compressed raw bytes |
| `source_mtime_ns`, `source_inode` | stat of the file at capture time |
| `revision_seq` | 1, 2, 3 … per `source_identity`, monotonic |
| `captured_at` | UTC ISO timestamp |
| `decode_status` | `parsed`, `partial`, `unknown`, `malformed`, `stale`, `unparsed` |
| `decode_note` | free text: why the status is not `parsed` |
| `media_type` | coarse type derived from the suffix (`jsonl`, `json`, `html`, …) |

Uniqueness: `(source_identity, content_sha256)`. Re-capturing identical bytes is
a no-op that returns the existing row; different bytes insert the next
`revision_seq` and leave the old revision intact.

`source_artifacts` holds non-primary material of a revision (attachments,
undecodable blobs, task artefacts), keyed by `(revision_id, artifact_key)` with
its own checksum, size, media type, `decode_status` and `decode_note`. It
follows the same append-only rule.

### Capture timing and the stale window

Section 3 of #151 asks for capture *before* interpretation. Extractors read
files directly, so a capture hook inside every extractor would be 15 copies of
the same code and would still miss provider-specific formats. Instead the store
layer captures at write time and proves whether the bytes are the parsed ones:

- `UnifiedSession.source_mtime_ns` records the mtime the extractor saw.
- The writer stats the file again at capture time. Equal mtime → `parsed`.
- Different mtime → `stale` and a note naming both values. The bytes are still
  stored (they are the best available evidence), but the row does not claim to
  be the input of the parse it is linked to.

Reason: a parser bug on a file that is rewritten between parse and write is the
only case the stale flag cannot cover, and no provider rewrites a transcript in
that window in practice. The alternative — reading every source file twice per
sync with a checksum — doubles sync I/O for the whole archive.

### Credential boundary

`is_credential_source(path)` refuses, case-insensitively:

- exact names `credentials.json`, `credentials`, `.env`, `auth.json`,
  `tokens.json`, `cookies`, `cookies.json`, `keychain.json`
- names starting with `.env.`, `id_rsa`, `id_ed25519`, `id_ecdsa`
- suffixes `.pem`, `.key`, `.p12`, `.pfx`, `.keystore`, `.jks`
- any name containing `credential`, `secret` or `password`

Refused files are not stored; the sync records them as `excluded` in the source
health tally (#162) instead of silently ignoring them.

### Idempotence and cost

The capture path keeps a per-`source_identity` cache of the last
`(mtime_ns, byte_size)` → `revision_id`. A file whose mtime and size are
unchanged links to the cached revision without re-reading or re-hashing it.
`capture_source_revision(force_checksum=True)` bypasses the cache; the scheduled
verification job (not part of this change) uses it.

## 2 — Canonical message envelope (#154)

Migration 20 adds to `messages`:

| column | meaning |
| --- | --- |
| `native_id` | provider message id when the provider has one |
| `content_blocks_json` | ordered typed blocks (`text`, `thinking`, `tool_use`, `tool_result`, `image`, `document`, `unknown`) |
| `tool_calls_json` | `[{id, name, arguments, result, error, status, duration_ms, truncated}]` |
| `status` | `ok`, `failed`, `cancelled`, `pending`, `running`, `unmatched`, `unknown` |
| `extensions_json` | versioned bag: `{"envelope": 1, "<provider>": {…unknown fields…}}` |
| `envelope_version` | 0 for legacy rows, 1 for rows written by the envelope writer |

Rules:

- `content` stays the flattened text (FTS, exporters and the UI depend on it);
  the blocks are the lossless view and are ordered.
- Unknown provider fields go into `extensions_json` verbatim, including their
  original key names.
- Null and empty are distinct: a missing field is `NULL`, a present empty value
  is `""`/`[]`/`{}` as the provider sent it.
- Very large values are not silently dropped. A block or argument over
  `MAX_ENVELOPE_CHARS` (256 KiB) is replaced by
  `{"truncated": true, "sha256": …, "original_length": …}` plus a note, and the
  row's `status` stays unchanged.
- Legacy rows keep `envelope_version = 0`; readers must not present a legacy
  row as a complete envelope (a `legacy` marker in the reader's result).

## 3 — Namespaced session identity (#155)

`UnifiedSession.session_id` keeps its current, provider-derived value so every
existing reference (tags, memories, exports, bookmarks) keeps resolving. The
store adds the missing qualifiers as columns instead of re-keying the table:

- `source_namespace` — `<tool>:<machine-or-home-scope>`, derived from the
  extractor's tool and the source root, so two machines or two providers cannot
  collide silently.
- `source_record_id` — the raw provider record id (`session_id` before any
  normalisation).
- `content_sha256` — hash of the canonical serialised session (messages in
  order, roles, timestamps, model), so byte-identical copies are detectable.
- `copy_of_session_id` — set on the later arrival when a divergent copy exists.

Merge rules:

1. Same `source_namespace` + same `source_record_id` + same `content_sha256`
   → identical copy, deduplicated (one row, the second arrival is a no-op).
2. Same identity, different content → the existing row is kept and the new copy
   is stored under a qualified id `<tool>:<namespace>:<record_id>#<n>`,
   `copy_of_session_id` points at the first copy. No row is replaced.
3. Different namespace, same `source_record_id` → different sessions; the
   qualified id is used when the unqualified id is taken.
4. `resolve_session_id(conn, value)` resolves legacy ids, qualified ids and
   record ids to the canonical row and returns `None` for unknown values;
   all user-facing lookups (web route, CLI, memory evidence) go through it.

## 4 — Stable evidence references (#160)

`memory_sources` gets `session_namespace`, `message_native_id`,
`source_revision_id` and `message_sha256`. The legacy integer `message_id`
(rowid) stays for compatibility, but no new link is written without a stable
identity: `record_evidence()` resolves `(session_id, seq)` to
`(namespace, native_id | content hash, revision)` at write time.

A resolver maps a legacy `message_id` to the current row by matching
`(session_id, seq)` recorded alongside it, so a reindex that changes rowids does
not break evidence links.

## Migration order and safety

Each migration is append-only and idempotent (`MIGRATIONS` list, DDL guarded by
`_run_migration_sql`). Order: 19 (#151) → 20 (#154) → 21 (#155) → 22 (#160).
`lore backup` (#161) ships before any user-facing migration is deployed and the
restore path asserts the schema version, so a downgrade is possible from a
backup, not from the live file.

Tests: `tests/test_source_revisions.py`, `tests/test_message_envelope.py`,
`tests/test_session_identity.py`, `tests/test_evidence_references.py`, each
with legacy-row fixtures that must stay readable.
