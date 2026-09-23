# Archive Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an interrupted, empty, failed, or partial archive sync preserve the last committed session content while allowing healthy sessions to be refreshed.

**Architecture:** Replace the v2 store's destructive full-replace transaction with transaction-scoped session upserts. A refreshed session rewrites only its own dependent messages and FTS row; a reused or preserved session updates metadata without deleting existing messages. The extraction service merges prior derived entries that were not observed in the current run, excluding explicit tombstones, so an unavailable source cannot erase archived content.

**Tech Stack:** Python 3, SQLite with foreign keys and FTS5, pytest, existing `UnifiedSession`/`IndexBuilder` streaming pipeline.

**Spec:** `docs/audits/2026-09-12-lore-archive-audit.md` (A#02 / GitHub #152), plus the approved #152 preservation design.

## Global Constraints

- Preserve existing archive content when an extractor fails, returns no sessions, is unavailable, or produces only a partial result.
- Source disappearance is not authorization to delete archived content; only explicit existing tombstones exclude sessions from derived indexes.
- Keep source revision capture, structured message roundtrip, evidence references, and verified backups out of this scope.
- Keep v2 failures non-fatal to the legacy index path and roll back an uncommitted v2 transaction on cancellation or write failure.
- Run all tests against `tmp_path` fixtures and an isolated `HOME`; never use a real user archive.
- Do not change labels, close issues, commit, push, deploy, or publish.

---

### Task 1: Lock down transaction-scoped v2 preservation

**Files:**
- Modify: `tests/test_storage_writer.py`
- Modify: `tests/test_storage_session_vectors.py`
- Modify: `lore/storage/writer.py`

**Interfaces:**
- Consumes: Existing `write_sessions`, `StreamingV2Writer.add_full`, and `StreamingV2Writer.add_reused_entry` interfaces.
- Produces: Existing public interfaces with upsert/preservation behavior; no caller-facing signature changes.

- [x] **Step 1: Write the failing tests**

Replace the full-replace expectation with preservation assertions and add session-scoped rewrite assertions:

```python
def test_write_sessions_preserves_sessions_from_prior_commits(tmp_path):
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("old", n_messages=2)])
    write_sessions(db, [_session("new", n_messages=1)])

    conn = sqlite3.connect(db)
    assert sorted(row[0] for row in conn.execute("SELECT id FROM sessions")) == ["new", "old"]
    assert conn.execute(
        "SELECT content FROM messages WHERE session_id='old' ORDER BY seq"
    ).fetchall() == [("message body 0",), ("message body 1",)]
    assert conn.execute(
        "SELECT entity_id FROM search_index WHERE search_index MATCH 'message'"
    ).fetchall() == [("old",), ("new",)]


def test_write_sessions_replaces_only_a_refreshed_session(tmp_path):
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("old", n_messages=2), _session("fresh", n_messages=1)])
    write_sessions(db, [_session("old", title="Refreshed", n_messages=3)])

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT title FROM sessions WHERE id='old'").fetchone() == ("Refreshed",)
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='old'"
    ).fetchone() == (3,)
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='fresh'"
    ).fetchone() == (1,)


def test_reused_entry_does_not_delete_existing_messages(tmp_path):
    db = tmp_path / "v2.sqlite"
    write_sessions(db, [_session("kept", n_messages=2)])
    write_sessions(
        db,
        [],
        reused_entries=[
            {
                "id": "kept",
                "tool": "claude-code",
                "project": "/home/u/proj",
                "title": "Kept metadata",
                "created": "2026-01-01",
                "updated": "2026-01-02",
                "messages": 2,
                "search_text": "kept metadata",
            }
        ],
    )

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='kept'"
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT messages_synced FROM sessions WHERE id='kept'"
    ).fetchone() == (1,)
```

Keep the existing metadata-only reused-entry test for an id that does not yet exist; a new row must still have `messages_synced = 0` and no message rows. Update its wording/docstrings so they describe metadata-only rows without claiming the entire store is replaced.

- [x] **Step 2: Run the focused tests and verify the expected red failures**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/test_storage_writer.py::test_write_sessions_preserves_sessions_from_prior_commits tests/test_storage_writer.py::test_write_sessions_replaces_only_a_refreshed_session tests/test_storage_writer.py::test_reused_entry_does_not_delete_existing_messages -p no:cacheprovider -q -o addopts=""
```

Expected: the new preservation tests fail because `StreamingV2Writer.begin()` deletes all sessions/search rows and `INSERT OR REPLACE` cascades existing messages.

- [x] **Step 3: Implement the minimal v2 upsert behavior**

In `lore/storage/writer.py`, replace `_SESSION_INSERT` with `INSERT ... ON CONFLICT(id) DO UPDATE SET ...`, preserving the primary-key row. Remove the global deletes from `StreamingV2Writer.begin()`. Before `_write_full_session` writes a refreshed id, delete only that id's `messages` and session FTS row, then upsert the session and insert its current message/FTS rows. Make `_write_reused_entry` upsert metadata and replace only that id's FTS row; when an existing row has `messages_synced = 1`, preserve that flag and its message rows, while a new row remains metadata-only with `messages_synced = 0`. Update comments/docstrings from “full replace” to transaction-scoped upsert/preservation language.

- [x] **Step 4: Run the focused storage tests and verify green**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/test_storage_writer.py tests/test_storage_session_vectors.py -p no:cacheprovider -q -o addopts=""
```

Expected: all tests in both files pass. If the old vector replacement test asserts that an absent session is removed, update only that expectation to match preservation while keeping vector creation for new sessions covered.

### Task 2: Preserve full message rows for reused streamed sessions

**Files:**
- Modify: `tests/test_storage_writer.py`
- Modify: `tests/test_index_builder_streaming.py`
- Modify: `lore/exporters/index.py`
- Modify: `lore/storage/writer.py`

**Interfaces:**
- Consumes: `IndexBuilder.build_index(..., reused_ids=...)` and `_MultiWriter.add_reused_session`.
- Produces: Reused full sessions retain existing v2 messages and metadata without an unnecessary message rewrite; public builder signature remains unchanged.

- [x] **Step 1: Write the failing reused-stream regression test**

Add a two-build test using the existing `_session` helper:

```python
def test_reused_stream_preserves_existing_v2_messages(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("reused", n_messages=2)], {})
    prior = {
        "id": "reused",
        "tool": "claude-code",
        "project": "/proj",
        "thread_id": None,
        "title": "Reused One",
        "created": "2025-06-15T10:00:00",
        "updated": "2025-06-15T10:00:00",
        "messages": 2,
        "prompts": 0,
        "keywords": [],
        "search_text": "message body",
    }
    IndexBuilder(tmp_path).build_index(
        [_session("reused", n_messages=2)],
        {},
        reused_entries=[prior],
        reused_ids={"reused"},
    )

    conn = sqlite3.connect(v2_db_path(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='reused'"
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT messages_synced FROM sessions WHERE id='reused'"
    ).fetchone() == (1,)
```

- [x] **Step 2: Run the new test and verify red**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/test_index_builder_streaming.py::test_reused_stream_preserves_existing_v2_messages -p no:cacheprovider -q -o addopts=""
```

Expected: fail because the current `_MultiWriter.add_reused_session` routes the session through `add_full`, which rewrites its message rows.

- [x] **Step 3: Implement per-id reused routing**

Add a focused writer operation for a full `UnifiedSession` whose JSON/legacy representation is already supplied by a reused entry. It must inspect the existing v2 row inside the open transaction; if `messages_synced = 1`, update only session metadata and the session FTS row, leaving `messages` untouched; if absent or incomplete, perform the normal full session write for that id. Append an embedding input with `text=None` for an unchanged reused row so its vector is retained. Change `_MultiWriter.add_reused_session` to call this operation. Track full reused ids as a set rather than a single `_had_reused_session` boolean, so `_seed_reused_entries` decides per entry whether the full v2 row was supplied.

- [x] **Step 4: Run the streaming and storage regression tests**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/test_index_builder_streaming.py tests/test_storage_writer.py -p no:cacheprovider -q -o addopts=""
```

Expected: all tests pass, including existing #35 full-message coverage and the new reused-preservation test.

### Task 3: Preserve prior derived entries across incomplete extractor runs

**Files:**
- Modify: `tests/test_services_extraction.py`
- Modify: `lore/services/extraction.py`
- Modify: `lore/exporters/index.py`

**Interfaces:**
- Consumes: Existing `build_search_index` extractor iteration, `deleted_ids`, `incremental`, and `IndexBuilder` streaming inputs.
- Produces: Existing report list and progress callbacks, with prior non-tombstoned entries merged when unseen in the current run.

- [x] **Step 1: Write failing service-level preservation tests**

Add tests for failed, empty, partial, tombstoned, and cancelled runs using two consecutive builds and `tmp_path`:

```python
def test_failed_extractor_preserves_prior_entries(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [], raises=RuntimeError("gone"))])

    errors = extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)

    assert [row["id"] for row in json.loads((tmp_path / "index.json").read_text())["sessions"]] == ["old"]
    assert errors == [{"extractor": "claude-code", "error": "gone"}]


def test_empty_extractor_preserves_prior_entries_and_adds_healthy_rows(
    tmp_path, patched_extractors
):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors(
        [_StubExtractor(Tool.CLAUDE_CODE, []), _StubExtractor(Tool.CODEX, [_session("new")])]
    )

    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)

    ids = [row["id"] for row in json.loads((tmp_path / "index.json").read_text())["sessions"]]
    assert sorted(ids) == ["new", "old"]


def test_explicit_tombstone_is_not_reintroduced(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [])])

    extraction.build_search_index(
        tmp_path, tmp_path / "index.json", deleted_ids={"old"}, incremental=False
    )

    assert json.loads((tmp_path / "index.json").read_text())["sessions"] == []


def test_cancelled_build_leaves_previous_index_untouched(tmp_path, patched_extractors):
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("old")])])
    extraction.build_search_index(tmp_path, tmp_path / "index.json", incremental=False)
    before = (tmp_path / "index.json").read_bytes()
    patched_extractors([_StubExtractor(Tool.CLAUDE_CODE, [_session("new")])])

    with pytest.raises(extraction.ActionJobCancelledError):
        extraction.build_search_index(tmp_path, tmp_path / "index.json", should_stop=lambda: True)

    assert (tmp_path / "index.json").read_bytes() == before
```

- [x] **Step 2: Run the service tests and verify the expected red failures**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/test_services_extraction.py::test_failed_extractor_preserves_prior_entries tests/test_services_extraction.py::test_empty_extractor_preserves_prior_entries_and_adds_healthy_rows tests/test_services_extraction.py::test_explicit_tombstone_is_not_reintroduced tests/test_services_extraction.py::test_cancelled_build_leaves_previous_index_untouched -p no:cacheprovider -q -o addopts=""
```

Expected: the failed/empty runs lose `old` because `existing_by_id` is currently loaded only for incremental builds; cancellation must remain before finalization.

- [x] **Step 3: Implement prior-entry merging at the service boundary**

In `build_search_index`, load existing JSON entries whenever an existing `index.json` is present; track `seen_ids` only for sessions yielded into the current build and never add ids filtered by `deleted_ids`; after the extractor stream drains successfully, append prior entries whose ids are not in `seen_ids` and not in `deleted` to `reused_entries`. Preserve their original dict/search text and route them through the existing reused-entry path. Keep cancellation and unexpected outer build exceptions before `IndexBuilder` finalization. Ensure a healthy session with a matching id is not duplicated. In `IndexBuilder._MultiWriter`, make reused-entry v2 seeding work per id so preserved entries coexist with freshly extracted sessions and reused full sessions. Do not add source-health persistence or tombstone-table migration here.

- [x] **Step 4: Run service and end-to-end focused checks**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/test_services_extraction.py tests/test_index_builder_streaming.py tests/test_storage_writer.py tests/test_storage_session_vectors.py -p no:cacheprovider -q -o addopts=""
```

Expected: all focused tests pass, including extractor error collection, incremental mtime reuse, deletion filtering, streaming order, v2 non-fatal behavior, and vector retention.

### Task 4: Update affected behavior documentation and verify the full change

**Files:**
- Modify: `lore/storage/writer.py` docstrings/comments as needed
- Modify: `lore/services/extraction.py` docstrings/comments as needed
- Modify: `lore/exporters/index.py` docstrings/comments as needed

**Interfaces:**
- Consumes: The implementation and focused regression suite from Tasks 1–3.
- Produces: Accurate maintainer documentation without changing the audit files or issue metadata.

- [x] **Step 1: Review and update inline documentation**

State clearly that v2 writes are transaction-scoped upserts, refreshed ids replace only their own message/FTS rows, existing complete reused ids retain their message rows, and unseen prior entries are preserved unless explicitly tombstoned.

- [x] **Step 2: Run repository checks in an isolated environment**

Run:

```bash
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/python -m pytest tests/ -p no:cacheprovider -q -o addopts=""
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/ruff check lore/storage/writer.py lore/exporters/index.py lore/services/extraction.py tests/test_storage_writer.py tests/test_storage_session_vectors.py tests/test_index_builder_streaming.py tests/test_services_extraction.py
HOME=/tmp/opencode/lore-152-test-home /home/dnames/projects/ai-stack/lore/.venv/bin/mypy lore/storage/writer.py lore/exporters/index.py lore/services/extraction.py --ignore-missing-imports
```

Expected: the full isolated suite passes; focused lint and mypy report no new errors. The known unrelated `tests/test_storage_embeddings.py:139` unused `os` import remains untouched.

- [x] **Step 3: Inspect the final diff and worktree state**

Run:

```bash
git diff --check
git status --short --branch
git diff --stat
```

Confirm only the issue-152 worktree’s plan, implementation, tests, and inline documentation changed. Do not stage, commit, push, deploy, or publish.
