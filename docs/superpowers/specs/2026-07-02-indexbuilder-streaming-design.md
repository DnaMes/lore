# Design — Streaming IndexBuilder (issue #96)

## Problem

The reload/reindex path materialises **all ~960 `UnifiedSession` objects (with
full message bodies) in a Python list** before writing the index, peaking at
**~1740 MB RSS** — measured on 2026-07-02 with **zero embedding involved** (the
embedding model is only +263 MB on top). This is a pre-existing `IndexBuilder`
memory problem, unrelated to hybrid search (#87). On a small host or a
memory-limited container it is an OOM risk.

## Root cause

`IndexBuilder.build_index(sessions: List[UnifiedSession], …)`
(`lore/exporters/index.py`) consumes its input **three times**:

1. the JSON build loop → `index["sessions"]` dicts + keyword inverted index,
2. `_build_sqlite_index(sessions, …)` → legacy `index.sqlite` rows + FTS,
3. `write_sessions_safe(sessions, …)` → the v2 dual-write (rows + message rows
   + post-commit embed).

Because the API needs three passes, **every caller** first materialises a full
`List[UnifiedSession]` (`extraction.py`, and the `sessions.append` /
`sessions.extend` loops in `lore_cli.py`). Extractors already `yield`
(`extract_sessions() -> Iterator`), so the *source* is lazy — the callers force
it into a list solely to satisfy the three-pass consumer.

## Goal

Make extraction → index-write **single-pass streaming** so peak RSS is bounded
by *one* session's message bodies (+ the light accumulated JSON dicts we already
write to disk), regardless of archive size — **without changing behaviour**
(identical `index.json`, legacy `index.sqlite` rows/FTS, v2 rows/messages, and
vectors).

## Approach — `MultiWriter` fan-out, streamed from generator callers

Collapse the three passes into **one loop** over a single
`Iterable[UnifiedSession]`. A new `MultiWriter` object owns the two SQLite
connections and the in-memory JSON accumulator, and fans each session out to all
consumers as it arrives; the `UnifiedSession` is then dropped before the next is
pulled.

### Refinements from the steelman pass (do not skip)

1. **Ordering must stay reused-entries-first, then extraction order.** Multiple
   tests assert exact ordered id lists and `sessions[0]` (`test_storage_reader.py`,
   `test_services_extraction.py`). `MultiWriter.__init__` seeds the reused-entry
   dicts/rows **before** any `add()` runs, so the JSON `sessions` array and the
   legacy sqlite rows keep today's order byte-for-byte. `add()` appends in the
   order sessions are yielded (extraction order) — identical to today.
2. **Batch the SQLite writes (N=100), don't single-row per session.** The
   legacy `_build_sqlite_index` uses `executemany` today; keeping that shape
   means buffering ~100 rows, flushing with `executemany`, then dropping the
   buffer. This bounds RSS to a batch (~180 MB worst case) *and* preserves bulk
   insert performance, while avoiding a full transaction-ownership rewrite.
   Per-message v2 inserts are already single-row `execute` today (`writer.py`),
   so v2 message-write performance is unchanged. `BATCH_SIZE = 100`, tunable via
   a module constant.
3. **v2 stays best-effort with clean degradation.** A mid-stream v2 error
   rolls back v2 and disables further v2 writes for the run; the legacy index
   still completes and commits. Failure mode is *legacy-correct, v2-stale* —
   the same guarantee as today's `write_sessions_safe`. A regression test
   injects a `sqlite3.Error` mid-stream and asserts legacy `index.json` is
   complete while v2 is not half-written.

### `MultiWriter` (new, in `lore/exporters/index.py`)

```python
class MultiWriter:
    def __init__(self, output_dir, *, ignored_ids, reused_entries=None):
        # opens legacy index.sqlite conn (schema + DELETE),
        # opens v2 conn via initialise() and BEGINs its txn,
        # seeds index["sessions"] + keyword_index + legacy/v2 rows from
        #   reused_entries (light dicts — cheap to hold).
    def add(self, session, export_path=""):
        # compute title/outline/keywords/search_text ONCE, then:
        #   - append light JSON dict to index["sessions"] + keyword_index
        #   - INSERT legacy sqlite row + FTS row
        #   - INSERT v2 session row + message rows; record embed input
    def finalize(self):
        # stats from index["sessions"] dicts, json.dump (atomic tmp+replace),
        # commit legacy + v2, then embed_sessions() post-commit (unchanged).
```

`build_index` becomes a thin orchestrator:

```python
def build_index(self, sessions, export_paths, reused_entries=None):
    writer = MultiWriter(
        self.output_dir, ignored_ids=self._load_ignored(), reused_entries=reused_entries
    )
    for session in sessions:  # sessions may be a generator
        writer.add(session, export_paths.get(session.session_id, ""))
    writer.finalize()
```

### Key simplification — drop `reused_sessions`

Today the incremental path threads a **separate** `reused_sessions` list of full
`UnifiedSession` objects (the unchanged sessions) so the v2 store gets their
message rows (#35). On a warm incremental sync that is *most* of the 960
sessions, so it re-creates the 1740 MB even with a streaming `build_index`.

Observation: for the **v2 store**, reused and refreshed sessions are *identical*
full sessions — the reuse distinction only affects the **legacy JSON/sqlite**
side (reused → copy the prior dict verbatim; refreshed → recompute). So the
caller streams **every** extracted session once through `MultiWriter.add`,
passing along whether a prior JSON entry exists:

- prior entry exists **and** mtime unchanged → `add` copies the prior dict into
  the legacy JSON/sqlite (no recompute) **and** writes full v2 rows +
  `messages_synced = 1`. This is strictly *better* than today's behaviour, which
  wrote reused sessions to v2 but their legacy entry came from the prior dict —
  same net result, no `reused_sessions` list.
- otherwise → `add` recomputes everything (the refreshed path).

`reused_entries` (light dicts for sessions no longer present in the current
extraction, e.g. deleted source files) is still accepted and seeded up front —
those are ~KB each and safe to hold.

### Callers stream (no `all_sessions` list)

- **`lore/services/extraction.py::build_search_index`** — the loop already walks
  `extractor.extract_sessions()` per session for progress + incremental mtime
  checks. It becomes a **generator** that `yield`s each session (annotated
  reused/refresh) straight into `build_index`, instead of `sessions.extend(...)`
  + a trailing `reused_sessions` list.
- **`lore_cli.py`** — the `sessions.append` / `sessions.extend` loops feeding
  `build_index(all_sessions, …)` at lines ~298/454/518/784/1214 become
  generators. `export_paths` (a dict keyed by id) is small and stays as-is.

## Transaction & failure semantics (unchanged contracts)

- The **legacy** index write stays authoritative; the **v2** write stays
  best-effort. `MultiWriter` catches v2 errors, logs, and swallows them exactly
  as `write_sessions_safe` does today — a v2 failure never breaks the legacy
  index. Since v2 rows are now `INSERT`ed incrementally inside one transaction,
  a mid-stream v2 error triggers a v2 `ROLLBACK` and disables further v2 writes
  for the run, while the legacy path completes.
- `embed_sessions` still runs **after** the v2 commit, over the accumulated
  light `(id, fts_body, mtime)` inputs — never holding the write lock across
  model calls.
- Atomic JSON write (tmp file + `os.replace`) and file perms (`0700`/`0600`)
  unchanged.

## Testing

- **Behaviour-identical golden test:** build an index from a fixed set of
  sessions the old way vs the new `MultiWriter` and assert byte-identical
  `index.json` (modulo `generated_at`), identical legacy sqlite rows/FTS, and
  identical v2 rows/messages/vectors. (Existing `tests/test_index_builder.py`,
  `test_storage_reader.py`, `test_services_extraction.py` already assert most of
  these — they must stay green unchanged.)
- **Streaming-contract test (the #96 guarantee):** feed `build_index` a
  generator that increments a live counter on `yield` and decrements it when the
  session is released (via `weakref.finalize` / an explicit sentinel), and assert
  the number of *simultaneously-live* `UnifiedSession` objects never exceeds a
  small constant (1–2). This fails on the old list-materialising code and passes
  on the streamed code — it is the regression guard.
- **Real RSS before/after:** extract-all through `build_index` and read
  `/proc/<pid>/status` VmRSS at peak; target = peak stays roughly flat as
  session count grows (verify against the real ~960-session archive). Recorded
  in the PR, not asserted in CI.

## Findings during implementation (root cause was broader than the handoff)

Measured on the real ~980-session archive (peak `VmHWM` via `/proc/self/status`):

| Path | Before | After |
|---|---|---|
| cold full rebuild (Docker / OOM-risk case from #96) | **2084 MB** | **1404 MB** |
| real web-reload path (`build_search_index`, non-incremental) | 2040 MB | **1421 MB** |

The handoff framed #96 as "build_index holds the list". That was **one of three**
contributors, not the whole story:

1. **build_index list retention (~500–600 MB)** — fixed here (streaming
   `_MultiWriter` + generator callers). This is the *unbounded* term (grew with
   archive size); now bounded.
2. **Pathological large-file parse spikes (~1 GB transient)** — a single 139 MB
   VSCode chat session parsed whole via `json.load` drove `VmHWM` from 31 MB to
   1226 MB. Fixed here with a 25 MB per-session-file cap in the VSCode extractor
   (`MAX_SESSION_FILE_BYTES`), logged via `_record_skip` (no silent drop).
3. **Per-extractor internal materialisation (opencode ~425 MB, claude `seen`
   dict)** — `opencode.extract_sessions` dedups across file + sqlite sources into
   a dict, then sorts, so it holds all its sessions before yielding. This is
   inherent to its cross-source dedup contract; making it streaming needs a
   two-pass id-scan redesign. **Left as follow-up** (documented, not silently
   ignored).

### Warm incremental — fixed too (#103, merged stream)

The warm incremental path (`incremental=True`, most sessions unchanged) also
peaked ~2.0 GB because `build_search_index` collected every unchanged session's
full `UnifiedSession` into a `reused_sessions` list to re-write its v2 message
rows (#35).

A **steelman pass** rejected the obvious fix (diff-based v2 write keyed on
mtime) — it would make `source_mtime_ns` the sole re-index trigger for message
content and FTS, a correctness downgrade that today's DELETE-and-rebuild
self-heals. Instead the caller now yields reused sessions through the **same
generator** as refreshed ones, tagged via a `reused_ids` set. `build_index`
routes a tagged id to a **v2-only** write (its JSON/legacy entry still comes
from `reused_entries`), so every session's full message rows still reach v2 —
DELETE-and-rebuild consistency preserved — but the caller never holds a second
list. `reused_sessions` (list param) is kept only for the list-based
callers/tests.

Result: warm incremental **2077 → 1429 MB** (−31%), now matching the
cold-rebuild floor. Verified `index.json` ids, v2 session ids, v2 message count,
and search_index rows are identical between a pure full rebuild and a
cold-then-incremental build.

Subtle bug caught in review: `build_index(..., reused_ids=reused_ids)` must NOT
do `reused_ids = reused_ids or set()` — the caller passes a **live** set that is
empty at call time and fills as the generator runs; `or set()` would swap in a
new set and mis-route every reused session to a full write (duplicating it in
the JSON index). Guard with `if reused_ids is None` instead.

## Non-goals / out of scope

- No change to extractor logic, title generation, keyword rules, or search
  ranking.
- The ONNX-arena high-water-mark from the *embed* pass (the issue's original
  "Ideas" list) is a separate concern (#92 territory); this PR removes the
  session-body 1740 MB, which the 2026-07-02 investigation proved is the
  dominant term.
- No refactor of unrelated `IndexBuilder` helpers.
