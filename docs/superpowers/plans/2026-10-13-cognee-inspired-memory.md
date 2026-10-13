# Cognee-inspired memory lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Lore's generated knowledge real, reviewable and recallable with evidence: fix `lore knowledge`, add snippets, hybrid memory recall, feedback, a candidate review queue, `lore improve` and `lore context`.

**Architecture:** Additive SQLite migrations 14-16 on the v2 store; new small modules in `lore/storage` (persistence) and `lore/services` (orchestration shared by `lore knowledge` and `lore improve`); MCP/CLI/web gain additive keys only.

**Tech Stack:** Python 3.11+, SQLite FTS5 + sqlite-vec (optional `semantic` extra), Flask, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-13-cognee-inspired-memory-design.md` (issue DnaMes/lore#197). Read it first; this plan only records decisions the spec leaves open.

## Global Constraints

- Python 3.11+, ruff format/check, 100-char lines, no bare `except`, specific exceptions.
- mypy must not exceed the 24 pre-existing errors; measure the baseline first.
- Migrations additive and idempotent; a v2 DB at version 13 must open on new code and keep its data.
- The optional `semantic` extra must keep degrading honestly to FTS; no new hard dependency, no cloud dependency, no mandatory model download.
- Backward compatible: MCP tool names and existing result keys stay; new keys are additive.
- No agent-harness hook registration (harness freeze, window 2026-11-01).
- Snippets contain untrusted chat text: escape before rendering.
- Tests: `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest <paths> -p no:cacheprovider -q -o addopts=""`.
- Commits go through `git-ops` (the main thread cannot `git commit`); branch `feat/197-<phase>`; Conventional Commits; stage explicit paths.

## Review Focus

1. A session with `messages_synced = 0` or no message rows: never sent to the LLM, counted as skipped, run still exits 0.
2. Queries containing quotes, `*`, `-`, `NEAR`, or one unbalanced `"`: no FTS syntax error and no traceback; unbalanced quote is rejected with a clear message.
3. Chat text containing `<img src=x onerror=...>` or `</script>`: escaped in the dropdown, `/memory` and any HTML output.
4. Provider returns non-JSON, fenced JSON, or raises: no `session_knowledge` row, next run retries, `lore knowledge` does not crash.
5. A v2 DB created at migration 13 (and one opened read-only by a running web server): migration applies without data loss; `database is locked` yields a one-line error, not a traceback.

---

## File structure

| File | Responsibility |
|---|---|
| `lore/storage/schema.py` (modify) | migrations 14, 15, 16 |
| `lore/storage/knowledge.py` (create) | persist/list per-session extractions, prompt fingerprint |
| `lore/storage/fts.py` (create) | `build_fts_query`, snippet markers |
| `lore/services/knowledge.py` (create) | `run_extraction`, shared by `lore knowledge` and `lore improve` |
| `lore/llm/tasks/knowledge.py` (modify) | expose conversation text, `KnowledgeEntry.from_dict` |
| `lore/storage/memory.py` (modify) | snippet, sources, hybrid recall, feedback, candidates, supersedes |
| `lore/storage/feedback.py` (create, phase 2) | `memory_feedback` table access, derived weight |
| `lore/services/improve.py` (create, phase 3) | stage pipeline and report |
| `lore/services/context.py` (create, phase 4) | `build_context_block` |
| `lore/utils/security.py` (modify) | `validate_search_query` |
| `lore_cli.py`, `lore/interfaces/mcp_tools/*.py`, `lore/interfaces/web.py`, `lore/templates/*.html` (modify) | surfaces |

---

## Phase 1 — foundations and evidence

### Task 0: Baseline

**Files:** none (record results in the PR description).

- [ ] **Step 1:** Run the full suite with the test command above; record passed/skipped/failed.
- [ ] **Step 2:** `ruff check . && ruff format --check .` and `mypy lore/ --ignore-missing-imports | tail -1`; record the mypy error count (expected 24).
- [ ] **Step 3:** Create branch `feat/197-phase1-evidence` from `main`.

### Task 1: `lore knowledge` sends the real conversation

**Files:**
- Modify: `lore/llm/tasks/knowledge.py`, `lore_cli.py:682-742` (`cmd_knowledge`)
- Test: `tests/test_knowledge_extraction.py` (create)

**Interfaces:**
- Produces: `KnowledgeExtractor.conversation_text(session: UnifiedSession, max_messages: int = 20) -> str` (the exact text placed in the prompt: role-prefixed lines, content cut at 500 chars) and `KnowledgeEntry.from_dict(data: dict) -> KnowledgeEntry`.
- Consumes: `load_session_messages_v2(output_dir: Path, session_id: str) -> Optional[list]` from `lore.storage`.

- [ ] **Step 1: Write failing tests** in `tests/test_knowledge_extraction.py` using a `FakeProvider(LLMProvider)` that records prompts and returns valid JSON:
  - `test_prompt_contains_message_text`: build a v2 DB in `tmp_path` with one synced session whose first message is `"use sqlite-vec for vectors"`, run `cmd_knowledge`-level helper (see Task 2) or `KnowledgeExtractor.extract_from_session` on a session with messages; assert the recorded prompt contains that text.
  - `test_conversation_text_caps_messages_and_chars`: 30 messages of 600 chars → at most 20 lines, each content ≤ 500 chars.
  - `test_entry_roundtrip`: `KnowledgeEntry.from_dict(e.to_dict()) == e` (compare `to_dict()` outputs).
- [ ] **Step 2:** Run them; expect FAIL (`conversation_text` missing).
- [ ] **Step 3:** Extract the conversation-building lines of `extract_from_session` into `conversation_text` and call it from there; add `from_dict` (missing keys default like the dataclass; `timestamp` parsed with `datetime.fromisoformat`, falling back to `datetime.now()`).
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit via git-ops: `fix(knowledge): expose conversation text and entry roundtrip (#197)`.

### Task 2: Persisted, idempotent extraction (migration 14)

**Files:**
- Modify: `lore/storage/schema.py` (append migration 14), `lore/storage/__init__.py` (exports), `lore_cli.py` (`cmd_knowledge`)
- Create: `lore/storage/knowledge.py`, `lore/services/knowledge.py`
- Test: `tests/test_session_knowledge.py` (create), extend `tests/test_knowledge_extraction.py`

**Interfaces:**
- Produces (storage):
  - `prompt_fingerprint(conversation: str, model: str, prompt_version: int) -> str` (sha256 hex)
  - `get_session_knowledge(output_dir: Path, session_id: str) -> Optional[dict]` (keys `fingerprint`, `model`, `prompt_version`, `payload`)
  - `save_session_knowledge(output_dir: Path, session_id: str, fingerprint: str, model: str, prompt_version: int, payload: dict) -> None` (upsert)
  - `list_session_knowledge(output_dir: Path) -> list[dict]` (keys `session_id`, `payload`, `created`)
- Produces (service): `KNOWLEDGE_PROMPT_VERSION = 1` and
  `run_extraction(output_dir: Path, sessions: list[UnifiedSession], extractor: KnowledgeExtractor, model: str) -> ExtractionReport` where `ExtractionReport` is a dataclass `{extracted: int, unchanged: int, skipped_no_messages: int, failed: int}`.
- Migration 14 DDL is the one in the spec (1.2).

- [ ] **Step 1: Write failing tests:**
  - `test_migration_14_from_v13`: create a DB, apply migrations only up to 13 (use `apply_migrations` with the existing helper pattern in `tests/test_storage_schema*.py`), insert a memory row, open with current code, assert `session_knowledge` exists and the memory row is intact.
  - `test_run_extraction_skips_unchanged`: two runs with the same session; provider call count is 1 after both; second report has `unchanged == 1`.
  - `test_changed_session_is_reextracted`: change a message; provider call count 2.
  - `test_failed_call_stores_nothing_and_retries`: provider returns `"not json"` then valid JSON; after run 1 `get_session_knowledge` is `None` and `failed == 1`; run 2 stores a row.
  - `test_session_without_messages_is_skipped`: `messages_synced = 0` session → `skipped_no_messages == 1`, provider never called.
  - `test_knowledge_base_json_is_complete_after_partial_run`: after run 2 only re-extracts one session, `knowledge_base.json` still contains all sessions' entries, built from `list_session_knowledge`.
  - `test_cmd_knowledge_exit_zero_when_nothing_new`: `cmd_knowledge` returns 0 and prints that nothing changed.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement migration 14, `lore/storage/knowledge.py`, `lore/services/knowledge.py`. `extract_from_session` returns `None` on failure today; `run_extraction` treats `None` as `failed` and writes nothing. Sessions come from `index.json` entries as before; messages via `load_session_messages_v2` set on the session before extraction.
- [ ] **Step 4:** Switch `cmd_knowledge` to `run_extraction`; rebuild `knowledge_base.json` from `list_session_knowledge` via the existing `build_knowledge_base`; print one line naming the provider and model before any text is sent (spec 3.2 privacy note); return 0 unless the provider cannot be initialised.
- [ ] **Step 5:** Run new tests, then `tests/` files touching schema (`pytest tests -k "schema or storage" -q`); expect PASS.
- [ ] **Step 6:** Commit via git-ops: `feat(knowledge): persist extractions and skip unchanged sessions (#197)`.

### Task 3: Quoted phrases and the query validator

**Files:**
- Create: `lore/storage/fts.py`
- Modify: `lore/utils/security.py`, `lore/storage/search.py:36-100`, `lore/storage/memory.py` (`recall_memory`), the five entry points that validate `query` (`grep -rn "validate_search_param" lore | grep -i "query\|q\b"`: MCP `search_history`, MCP `memory_recall`, `web.py` `/api/search`, `/api/v1/search`, `/memory`, CLI search)
- Test: `tests/test_fts_query.py` (create)

**Interfaces:**
- Produces: `build_fts_query(query: str) -> Optional[str]` (None for empty); `validate_search_query(query: str, max_length: int = 256) -> bool`.
- Rules: `validate_search_query` = `validate_search_param` semantics except a query may contain exactly one pair of `"` that wraps the **whole** query. `build_fts_query('"foo bar"')` returns `'"foo bar"'`; otherwise the existing per-term `'"t"*'` join. Inner `"` doubled for FTS5.

- [ ] **Step 1: Write failing tests** (`test_fts_query.py`): phrase builds one quoted token; plain terms unchanged (`'"a"* "b"*'`); `'"foo'` and `'a "b" c'` rejected by `validate_search_query`; `project`/`tool` still rejected with `"` by `validate_search_param`; a search over a fixture DB with `"exact phrase"` returns only the session containing that phrase, and `a"b` never raises.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement `fts.py` and `validate_search_query`; use `build_fts_query` in `search_sessions` and `recall_memory` (replace the inline `" ".join(...)`); switch `query`/`q` validation at the entry points to `validate_search_query`, leave `project`/`tool` strict.
- [ ] **Step 4:** Run new tests plus `tests/test_storage_search.py tests/test_mcp_scoped_search.py tests/test_api_v1_search_scope.py tests/test_storage_memory.py`; expect PASS.
- [ ] **Step 5:** Commit via git-ops: `feat(search): exact-phrase queries with a dedicated validator (#197)`.

### Task 4: Snippets on session search and memory recall

**Files:**
- Modify: `lore/storage/fts.py` (markers), `lore/storage/search.py`, `lore/storage/memory.py` (`MemoryEntry.snippet`, `to_dict`), `lore/interfaces/mcp_tools/search.py`, `lore/interfaces/web.py` (`/api/search` ~l.1735, `/api/v1/search`), `lore/templates/base.html` (dropdown JS ~l.1070), `lore/templates/memory.html`
- Test: `tests/test_snippets.py` (create)

**Interfaces:**
- Produces: in `fts.py` `SNIPPET_OPEN = "\x02"`, `SNIPPET_CLOSE = "\x03"` and `snippet_to_text(snippet: str) -> str` (replaces the sentinels with `«` and `»`). `search_sessions` results gain `"snippet": Optional[str]` in **sentinel form** next to `session` and `score` (storage layer only). Every payload builder (MCP `search_history`, `/api/search`, `/api/v1/search`, `MemoryEntry.to_dict`) applies `snippet_to_text`, so every JSON/MCP `snippet` is text with `«`/`»` and the dropdown only ever sees that form. `MemoryEntry` gains `snippet: Optional[str] = None` (already text form) and `source: str = "memory"`; session results expose `source: "session"` in the API payloads.
- Query: `snippet(search_index, 5, '\x02', '\x03', '…', 24)` (column 5 is `body`).
- `rrf_merge` keeps the first-seen result, so a hybrid result keeps the FTS snippet; semantic-only hits have none. `snippet` is therefore optional everywhere, and the legacy `SearchEngine` fallback does not produce it.

- [ ] **Step 1: Write failing tests:** `search_sessions` over a fixture whose body is long returns a snippet containing the sentinel-wrapped term; `snippet_to_text` output has `«`/`»`; `/api/search` JSON includes `snippet`; MCP `search_history` payload includes `snippet`; `memory_recall` entry `to_dict()` includes `snippet` and `source`; a memory whose body contains `<img src=x onerror=alert(1)>` is rendered by `/memory` with `&lt;img` in the HTML and no raw `<img`; the highlight step is `escape_snippet_html(text: str) -> str` in `lore/utils/formatting.py` (escape first, then turn `«`/`»` into `<mark>`/`</mark>`), used by `memory.html`, and `base.html` repeats exactly that order in JS (`escapeHtml` first, then replace).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement in storage and the three payload builders (additive keys only). In `base.html` render `escapeHtml(s.snippet)` first, then replace `«`/`»` with `<mark>`/`</mark>`; add the snippet as a second line under the title.
- [ ] **Step 4:** Run `tests/test_snippets.py` and the existing snapshot/web tests (`pytest tests -k "web or api or snapshot or memory_page" -q`); regenerate snapshots only if the diff is exactly the new markup.
- [ ] **Step 5:** Commit via git-ops: `feat(search): snippets and source tags on session and memory hits (#197)`.

### Task 5: Hybrid memory recall with sources

**Files:**
- Modify: `lore/storage/memory.py` (`recall_memory`, `_semantic_recall`, `MemoryEntry`), `lore/interfaces/mcp_tools/memory.py`, `lore_cli.py` (memory search), `lore/interfaces/web.py` + `memory.html` (checkbox)
- Test: extend `tests/test_storage_memory.py`, `tests/test_mcp_memory.py`

**Interfaces:**
- `recall_memory(..., semantic: Optional[bool] = None)`: `None` = auto (hybrid when `embeddings_available()` and a stored vector exists), `False` = FTS only, `True` = today's behaviour. Hybrid uses `rrf_merge(fts_results, semantic_results, key=lambda r: str(r["id"]), limit=limit)` over `{"id": int, "entry": MemoryEntry, "score": float}` dicts.
- `MemoryEntry.sources: List[Dict[str, str]]` = `[{"session_id": ..., "note": ...}]` from `memory_sources`; `message_id` is not exposed.

- [ ] **Step 1: Write failing tests:** with a fake embedding backend (see `tests/test_storage_embeddings.py` for the monkeypatch pattern) a memory without a vector is still returned in auto mode; `semantic=False` never calls the embedder; entry from `add_memory(..., source_session="s1")` has `sources == [{"session_id": "s1", "note": "recorded from this session"}]`; MCP `memory_recall` without `semantic` returns the same shape as before plus `sources`.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement. MCP: `semantic` absent → `None`; present → `bool`. Web checkbox checked → `True`, unchecked → `None`. CLI `--semantic` → `True`, otherwise `None`.
- [ ] **Step 4:** Run memory, MCP and web memory-page tests; expect PASS; then full suite vs Task 0 baseline.
- [ ] **Step 5:** Update `docs/API_REFERENCE.md` (new keys `snippet`, `source`, `sources`, quoted phrases, `semantic` tri-state) and `README` if it lists memory tools; one commit via git-ops: `feat(memory): hybrid recall by default and source provenance (#197)`.
- [ ] **Step 6:** Open the phase PR through git-ops (`feat/197-phase1-evidence` → main via review-gate); fix findings until APPROVE.

---

## Phase 2 — feedback and supersession candidates (`feat/197-phase2-feedback`)

### Task 6: Feedback storage and derived weight (migration 15)

**Files:** `lore/storage/schema.py`, create `lore/storage/feedback.py`, modify `lore/storage/__init__.py`; test `tests/test_memory_feedback.py`.

**Interfaces — Produces:**
- `record_feedback(output_dir: Path, memory_id: int, rater: str, score: int) -> None` (upsert on `(memory_id, rater)`; `ValueError` unless `1 <= score <= 5`, rater ≤ 64 chars, memory exists)
- `feedback_weight(output_dir: Path, memory_id: int) -> tuple[float, int]` returning `(w, n)` with `w = (sum((s-1)/4) + 0.5*3) / (n + 3)`; no rows → `(0.5, 0)`
- `ranking_factor(w: float, influence: float) -> float` = `1 + influence * ((0.75 + 0.5*w) - 1)`; influence read from `LORE_FEEDBACK_INFLUENCE` (default `0.0`, clamped to `[0, 1]`).

Tests: re-rating 50× equals once; any number of 5-star raters keeps `0.75 + 0.5*w ≤ 1.25` and 1-star `≥ 0.75`; influence 0 leaves order unchanged; migration from v14 keeps data. Commit: `feat(memory): feedback ratings with a derived, bounded weight (#197)`.

### Task 7: `memory_feedback` tool, recall payload, ranking hook

**Files:** `lore/interfaces/mcp_tools/memory.py`, `lore/storage/memory.py`, `lore_cli.py` (`lore memory rate <id> <1-5>`), `memory.html`; tests extend `tests/test_mcp_memory.py`.

**Interfaces:** MCP `memory_feedback(memory_id: int, score: int, rater: str = <author or "agent">)` → `{"status": "recorded", "weight": float, "count": int}`; `MemoryEntry.feedback_weight: float`, `feedback_count: int` always populated; the factor multiplies the fused score in `recall_memory` only when influence > 0.

Tests: tool validates ranges and unknown id; payload always shows weight/count; with influence set, a rated-up memory outranks an otherwise tied one. Commit: `feat(memory): memory_feedback tool and recall ranking factor (#197)`.

### Task 8: Similar-memory candidates and atomic `supersedes`

**Files:** `lore/storage/memory.py` (`add_memory(..., supersedes: Optional[int] = None) -> int`, `find_similar_memories(output_dir: Path, text: str, *, scope_project: Optional[str], limit: int = 3) -> list[dict]`), MCP `memory_write`; test `tests/test_memory_supersede_atomic.py`.

Rules from the spec (2.3), as far as the schema allows in this phase: target exists, not already superseded, same `scope_project`; violation → `ValueError`, nothing written. The `confirmed` condition (for the target and for `similar` candidates) is added in Task 9, once `memory.status` exists, together with its tests. Insert and `superseded_by` update in **one** transaction. `memory_write` returns `similar` and `similar_available`; threshold from `LORE_SIMILARITY_THRESHOLD` (default 0.85; to be calibrated with #92).

Tests: each violated rule writes nothing; injected failure after the insert (monkeypatch the update statement) leaves no orphan row; near-duplicate fixture appears in `similar`; no embedding backend → `similar_available == false`. Docs: `API_REFERENCE.md`. Commit and open the phase PR via git-ops.

---

## Phase 3 — `lore improve` and the review queue (`feat/197-phase3-improve`)

### Task 9: Candidate status (migration 16)

**Files:** `lore/storage/schema.py`, `lore/storage/memory.py`; test `tests/test_memory_status.py`.

**Interfaces:** `memory.status` default `'confirmed'`; this task also adds the `status == 'confirmed'` condition to `add_memory(..., supersedes=)` (target must be confirmed) and to `find_similar_memories` (Task 8), with tests for both; `add_memory(..., status: str = "confirmed")` accepts `candidate`; `set_memory_status(output_dir: Path, memory_id: int, status: str) -> bool` (only `candidate → confirmed|rejected`); `recall_memory`, `list_memory`, `find_similar_memories` filter `status == 'confirmed'` by default, `include_candidates: bool = False` / `status: Optional[str] = None` to override. FTS rows exist for candidates but are filtered by status in Python like `kind`.

Tests: default recall hides candidates and rejected; `set_memory_status` rejects other transitions; v15 DB migrates with all rows `confirmed`.

### Task 10: `lore improve`

**Files:** create `lore/services/improve.py`, modify `lore_cli.py` (`improve` subcommand in `COMMANDS` and `build_parser`), tests `tests/test_improve_pipeline.py`.

**Interfaces — Produces:**
- `StageResult` dataclass `{stage: str, status: Literal["completed","already_completed","skipped","errored"], reason: Optional[str], counts: dict[str, int], duration_ms: int}`
- `run_improve(output_dir: Path, *, provider: Optional[LLMProvider], model: Optional[str], limit: int = 50) -> list[StageResult]` running `embed_memories`, `extract_knowledge` (via `run_extraction`), `distill_lessons` in that order; one stage's error is recorded and the run continues.
- `distill_lessons(output_dir: Path, *, per_session_cap: int = 5, per_run_cap: int = 50) -> StageResult` per spec 3.2: decisions → kind `decision`, key points → kind `lesson`, status `candidate`, `source_session` set; skip reasons `already_known`, `empty`, `too_long`.
- CLI: `lore improve [--provider P] [--model M] [--limit N] [--json]`; provider from flag or `LORE_LLM_PROVIDER`, else `extract_knowledge` is `skipped(no_llm)`; exit 0 unless a stage `errored`.

Tests (from the spec's acceptance): no LLM and no embeddings → all stages skipped with reasons, exit 0; second run → `already_completed`; injection-style session text still yields only `candidate` rows; rejected lesson and its exact duplicate are not proposed again; caps hold; one stage raising does not stop the others.

### Task 11: Review queue

**Files:** `lore_cli.py` (`lore memory review|accept|reject`), `lore/interfaces/web.py` (`POST /memory/<id>/accept|reject`, candidates listing), `lore/templates/memory.html`; tests `tests/test_memory_review.py`, extend `tests/test_memory_page.py`.

Behavior: list shows candidate title, body, source session link and snippet; accept → `confirmed`; reject stores optional `reason` in `metadata_json["reject_reason"]`; web actions are POST with the app's existing CSRF/same-origin handling (follow `delete memory`). Docs: `README`, `API_REFERENCE.md`, `HANDOFF.md` entry. Phase PR via git-ops.

---

## Phase 4 — context block and UI (`feat/197-phase4-context`)

### Task 12: `lore context`

**Files:** create `lore/services/context.py`, modify `lore_cli.py`; test `tests/test_context_block.py`.

**Interfaces:** `build_context_block(output_dir: Path, *, project: Optional[str], tool: Optional[str] = None, budget: int = 1200) -> str`. Scope rule and ranking per spec 4.1; project default resolved from the cwd with the same key the sessions table uses; unresolvable → global memories only. Rendering oldest → newest, stops adding entries before exceeding `budget` characters.

Tests: never returns another project's memory; omits candidates, rejected and superseded; respects budget; `scope_tool` filtering; empty store → empty string.

### Task 13: Web touches

**Files:** `lore/interfaces/web.py` (`/session/<id>` adds `memories_from_session`), `lore/templates/session.html`, `memory.html` (feedback display, Candidates tab); tests extend `tests/test_memory_page.py`, snapshot tests if present.

Behavior: "Memories from this session" list from `memory_sources` reverse lookup (confirmed only); feedback weight and count on cards. No mindmap, no chat search.

### Task 14: Closeout

- [ ] Full suite vs the Task 0 baseline, ruff, mypy ≤ 24; record numbers in `HANDOFF.md`.
- [ ] File a board item tagged `harness-idea` for wiring `lore context` into SessionStart (window 2026-11-01); do not register any hook.
- [ ] Close #197 with the commit references; cross-link #187 (candidate/confirmed states realised, observation/hypothesis open) and #92 (threshold calibration).

## Self-review

- **Spec coverage:** 1.1 → T1; 1.2 → T2; 1.3 → T4; 1.4 → T3; 1.5 → T5; 2.1/2.2 → T6, T7; 2.3 → T8; 3.1 → T9; 3.2 → T10; 3.3 → T11; 4.1 → T12; 4.2 → T13. Acceptance items each map to a named test above.
- **Type consistency:** `run_extraction`, `ExtractionReport`, `StageResult`, `record_feedback`, `feedback_weight`, `set_memory_status`, `find_similar_memories`, `build_context_block` are defined once and referenced by the same names.
- **Known soft spots:** the similarity default 0.85 is a placeholder until measured; the sentinel/marker characters `«»` are a builder-settable detail.
