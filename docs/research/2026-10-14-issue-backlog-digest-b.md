# Open-issue backlog digest, Part B: Archive #01-#20 (#151-#170)

Snapshot at HEAD `136b0e0` (2026-10-14). Index and caveats: `docs/research/2026-10-14-issue-backlog-digest.md`.

## Part B


## Clusters (ship together, shared files/schema)

1. **Capture & retention spine — #151, #152, #153, #159, #162** (order: 152 → 151 → 159 → 153 → 162).
   Shared: `lore/storage/schema.py`, `writer.py`, `services/extraction.py`, `extractors/base.py`. #152 already landed and is the transactional foundation; #151 adds the raw/revision tables on the same connection; #159 scopes FTS deletes; #153 moves the quality gate after capture; #162 persists health. **Schema change:** #151 (new source/revision tables), #162 (health persistence). #159 is repair-only.
2. **Identity, evidence & portability — #154, #155, #160, #161, #163** (order: 154 → 155 → 160 → 161 → 163).
   Shared: message envelope + canonical session/message identity + revision/checksum model. #154 defines the envelope; #155 namespaces it; #160 re-points evidence at it; #161 and #163 serialize/merge it. **Schema/migration:** #154 (versioned JSON extension), #155 (identity), #160 (evidence ref). **Export/format:** #161 backup bundle, #163 import bundle — both new versioned formats.
3. **Extractor fidelity — #156, #157, #158, #164** (order: 164 (done) → 158 → 156 → 157).
   Shared: `lore/extractors/*.py` + the #154 envelope. Mostly per-provider read/parse work; #164 is done and can be closed.
4. **Transcript reading & export — #165, #166, #167, #168, #169, #170** (order: 166 → 165 → 169 → 168 → 167 → 170).
   Shared: `lore/exporters/markdown.py`, `html.py`, `interfaces/web_formatting.py`, `templates/session.html`, `base.html`. **Export format:** #165, #166, #169 (Markdown/HTML content), #168 (HTML attributes/role-state metadata), #170 (template markup/CSS). All must stay backward compatible; new status/provenance fields are additive only.

**Export formats that must not break:** Markdown session export (`lore/exporters/markdown.py`), standalone HTML export (`lore/exporters/html.py`), JSON `index.json`, and the new backup/merge bundle formats (#161/#163).

---

### #151 Archive #01: Capture immutable source revisions before parsing
- **STATUS: OPEN.** No raw-record/revision store exists: `lore/storage/schema.py` `MIGRATIONS` ends at 18 (`schema.py:320-343`); `writer.py` stores only `source_path`/`source_mtime_ns`; `git log --grep="#151"` empty.
- **SCOPE:** Add an append-only source-artifact/revision store (identity, checksum, size, revision, decode status, attachments) before interpretation, excluding credential/config files.
- **ACCEPTANCE:** [ ] raw records + non-secret attachments captured pre-parse; [ ] unknown events preserved with decode status; [ ] idempotent on unchanged source, new revision on divergence; [ ] credential/config files excluded.
- **FILES:** `lore/storage/schema.py` (new migration), `lore/storage/writer.py`, new `lore/storage/source_revisions.py`, `lore/extractors/base.py`, new `tests/test_source_revisions.py`.
- **DEPENDS-ON:** #152, #154, #155, #158, #161.
- **SIZE:** L · **RISK:** data-loss, privacy.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_source_revisions.py -q -o addopts=`

### #152 Archive #02: Preserve archived content when sources disappear or extraction fails
- **STATUS: CLOSED 2026-10-14** (verified against `main`, evidence comment posted). Was DONE-IN-REPO. Commits `fe1ca50` + merge `45ae3f6`. `writer.py:186` `_SESSION_INSERT` uses `ON CONFLICT(id) DO UPDATE` (no cascade delete); `StreamingV2Writer.begin()` (`writer.py:544`) only `BEGIN`; `abort()` (`writer.py:596`) rolls back; `_write_full_session` deletes only its own messages/FTS (`writer.py:371-374`). Tests: `tests/test_storage_writer.py:75,95,107`, `tests/test_contract_fields.py:329`, `tests/test_services_extraction.py` (failed/empty/tombstone/cancel), plan all `[x]` in `docs/superpowers/plans/2026-09-12-archive-preservation.md`.
- **SCOPE:** Close after verifying release notes; no code change required.
- **ACCEPTANCE:** [x] missing/failed/empty/cancelled never delete prior rows; [x] unavailable source not treated as deletion; [x] only explicit tombstone removes derived views; [x] failed txn leaves last committed archive intact.
- **FILES:** `lore/storage/writer.py`, `lore/services/extraction.py`, `lore/exporters/index.py`, `tests/test_storage_writer.py`, `tests/test_services_extraction.py`.
- **DEPENDS-ON:** #151, #159, #161, #162, #112.
- **SIZE:** S (verify/close) · **RISK:** data-loss (mitigated).
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_storage_writer.py tests/test_services_extraction.py tests/test_index_builder_streaming.py -q -o addopts=`

### #153 Archive #03: Separate archival capture from session-quality filtering
- **STATUS: PARTIAL.** Profiles exist (`lore/extractors/base.py:278-297`: `LORE_IMPORT_PROFILE` relaxed/strict + `LORE_MIN_USER_PROMPTS`) but the gate is still destructive: `should_import_session` returns False before storage (`base.py:299-338`, `MIN_USER_PROMPTS=3` at `base.py:47`). Tests force `MIN_USER_PROMPTS=1` (`tests/conftest.py:17`), so archived short sessions are still dropped in production.
- **SCOPE:** Capture every decoded session, then expose quality as a reversible query/profile; add separate captured/filtered/unsupported/failed counters.
- **ACCEPTANCE:** [ ] all decoded sessions retained incl. subagents/short tasks; [ ] quality filter is view/query-only with named profile; [ ] existing curated list behavior preserved as a filter; [ ] sync reports captured/filtered/unsupported/failed.
- **FILES:** `lore/extractors/base.py`, `lore/services/extraction.py`, `lore/storage/writer.py`, `lore_cli.py`, `tests/test_extractors_contract.py`.
- **DEPENDS-ON:** #151, #152, #162, #164.
- **SIZE:** M · **RISK:** data-loss.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_extractors_contract.py -q -o addopts=` (with production default profile unchanged).

### #154 Archive #04: Round-trip structured messages through SQLite without information loss
- **STATUS: PARTIAL.** Migration 17 added `kind,parent_id,active,tool_events_json,reasoning` (`lore/storage/schema.py:300-317`); reader/writer round-trip them (`writer.py:412-429`, `reader.py:250-331`; `tests/test_contract_fields.py:118`). Still missing: native `message_id`, full `tool_calls` (arguments/results), and a versioned unknown-field JSON extension. `_tool_events_json` explicitly drops arguments/results (`writer.py:63-97`).
- **SCOPE:** Add a versioned canonical message envelope storing native ID, full tool calls, provider fields and unknown extensions, with migration and legacy readability.
- **ACCEPTANCE:** [ ] native id/role/ts/model/tokens/reasoning/tool-calls/status/provider fields survive round trip; [ ] unknown fields in versioned extension; [ ] null/malformed/huge fields defined, never silently dropped; [ ] legacy rows migrate and stay readable.
- **FILES:** `lore/storage/schema.py`, `lore/storage/writer.py`, `lore/storage/reader.py`, `lore/core/models.py`, `tests/test_contract_fields.py`.
- **DEPENDS-ON:** #155, #156, #157, #160.
- **SIZE:** M · **RISK:** format-compat, migration.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_contract_fields.py -q -o addopts=`

### #155 Archive #05: Preserve divergent session copies and namespace source identities
- **STATUS: OPEN.** `sessions.id` is still the sole PK (`schema.py:47`); writer is last-write-wins `ON CONFLICT(id)` (`writer.py:186-213`). No source namespace, copy-linking, or compatibility resolver (`grep` finds none).
- **SCOPE:** Namespace identity by provider/source-record, dedupe byte-identical copies, keep divergent revisions separately addressable and linked.
- **ACCEPTANCE:** [ ] identity includes namespace + stable source-record id; [ ] identical copies dedupe, divergent histories addressable+linked; [ ] annotations/evidence survive merges; [ ] old IDs readable via compatibility resolver.
- **FILES:** `lore/storage/schema.py`, `lore/storage/writer.py`, `lore/storage/reader.py`, `lore/core/models.py`, new `tests/test_session_identity.py`.
- **DEPENDS-ON:** #151, #154, #160, #163.
- **SIZE:** L · **RISK:** data-loss, migration.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_session_identity.py -q -o addopts=`

### #156 Archive #06: Preserve Codex tool results and original message roles
- **STATUS: PARTIAL.** `codex.py` now emits paired TOOL messages and `tool_events` with status/duration/is_error, incl. unmatched calls (`codex.py:395-433,435-452,472-500,566`), covered by `tests/test_codex_extractor.py` / `test_extractor_codex_enrichment.py`. Missing: v2 storage/export of arguments, output, error and truncation metadata (`_tool_events_json` drops them, `writer.py:63-97`).
- **SCOPE:** Carry full Codex call args/results/errors/truncation through the #154 envelope, storage and Markdown/HTML export.
- **ACCEPTANCE:** [ ] calls/results pair by native id; [ ] interleaved/failed/cancelled/unmatched stay visible in order; [ ] args/output/error/truncation survive read+export; [ ] unknown Codex events stay with source revision.
- **FILES:** `lore/extractors/codex.py`, `lore/storage/writer.py`, `lore/storage/reader.py`, `lore/exporters/markdown.py`, `lore/exporters/html.py`, `tests/test_extractor_codex_enrichment.py`.
- **DEPENDS-ON:** #151, #154, #168, #166.
- **SIZE:** M · **RISK:** format-compat.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_codex_extractor.py tests/test_extractor_codex_enrichment.py -q -o addopts=`

### #157 Archive #07: Preserve Claude structured content and model metadata
- **STATUS: PARTIAL.** `claude.py` collects thinking→reasoning, tool_use/tool_result pairing, usage tokens and model (`claude.py:35-52,256-322,362-433`), covered by `tests/test_extractor_claude_enrichment.py`; redaction exists (`lore/exporters/redaction.py`). Missing: message IDs, non-text blocks (images/documents) and block order survive storage/export (v2 drops them, `writer.py:63-97`).
- **SCOPE:** Preserve Claude text/thinking/non-text blocks/attachments/usage/model/IDs and block order through the #154 envelope and exports.
- **ACCEPTANCE:** [ ] text/thinking/non-text/attachments/usage/model/IDs survive; [ ] block order + type stable across exports; [ ] redaction policy explicit/tested, no credential import; [ ] legacy flattened rows render honestly.
- **FILES:** `lore/extractors/claude.py`, `lore/storage/writer.py`, `lore/storage/reader.py`, `lore/exporters/redaction.py`, `tests/test_extractor_claude_enrichment.py`.
- **DEPENDS-ON:** #151, #154, #165, #169.
- **SIZE:** M · **RISK:** format-compat, privacy.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_extractor_claude_enrichment.py tests/test_claude_extractor.py -q -o addopts=`

### #158 Archive #08: Report Antigravity coverage accurately and preserve undecoded conversations
- **STATUS: PARTIAL.** Plaintext transcript reader + telemetry landed (`lore/extractors/antigravity.py`; commit `d4466b1`; `tests/test_extractor_antigravity_transcripts.py` 20+ tests). Availability is still directory-based: `antigravity.py:187-188` returns `(path/"brain").exists()`, so an install dir with no readable conversation reports available. No source-artifact preservation for undecoded conversations (blocked on #151).
- **SCOPE:** Make availability mean readable conversation data, count task artifacts/transcripts/unsupported separately, preserve undecoded conversations.
- **ACCEPTANCE:** [ ] availability requires readable conversation; [ ] artifacts/transcripts/unsupported counted separately; [ ] undecoded conversations preserved as source artifacts, not counted parsed; [ ] diagnostic reports availability/captured/parsed/unsupported/failed.
- **FILES:** `lore/extractors/antigravity.py`, `lore/services/discovery.py`, `lore_cli.py` (`cmd_check`), `tests/test_antigravity_extractor.py`.
- **DEPENDS-ON:** #151, #162, #164, backlog #30.
- **SIZE:** M · **RISK:** none.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_antigravity_extractor.py tests/test_extractor_antigravity_transcripts.py -q -o addopts=`

### #159 Archive #09: Keep memory search entries intact during session rebuilds
- **STATUS: PARTIAL.** The destructive global delete is gone: session FTS deletes are scoped to `entity_type='session'` (`writer.py:226,371`), so memory rows survive (#152). Missing: a repair command that detects/rebuilds missing memory search rows (no `repair` in `lore/storage`, only `_repair_once` in `services/summarize.py`) and explicit tests that memory tags/embeddings/searchable entries survive every sync path.
- **SCOPE:** Add scoped rebuild ownership + a memory-search repair command; test memory survival across full/incremental/empty/failed/cancelled syncs.
- **ACCEPTANCE:** [ ] all memory rows/tags/embeddings/search survive every sync path; [ ] session FTS rebuilds independently of memory FTS; [ ] repair reconstructs missing memory rows without altering content; [ ] tests cover all sync paths.
- **FILES:** `lore/storage/memory.py`, `lore/storage/writer.py`, `lore/storage/fts.py`, `lore_cli.py`, new `tests/test_memory_search_survival.py`.
- **DEPENDS-ON:** #152, #161, #33, #48.
- **SIZE:** M · **RISK:** data-loss.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_memory_search_survival.py -q -o addopts=`

### #160 Archive #10: Keep message evidence references stable across reindexing
- **STATUS: PARTIAL.** `memory_sources.message_id` is still `INTEGER` (`schema.py:157-162`); `link_memory_to_session(message_id: Optional[int])` writes it (`memory.py:878-896`). The reader deliberately omits it from `MemoryEntry.sources` because `messages.id` changes on re-sync (`memory.py:124-127`), but there is no canonical identity/resolver and no unresolved-reference reporting.
- **SCOPE:** Reference evidence by canonical session/message identity + source revision; resolve through a public API and report unresolved legacy refs.
- **ACCEPTANCE:** [ ] references use stable canonical identity + revision, not rowid; [ ] reindex/restore/merge preserve resolvable citations; [ ] missing legacy refs reported as explicitly unresolved; [ ] evidence exports carry enough provenance to verify the span.
- **FILES:** `lore/storage/schema.py`, `lore/storage/memory.py`, `lore/storage/reader.py`, `lore_cli.py`, `tests/test_storage_memory.py`.
- **DEPENDS-ON:** #151, #154, #155, #161, backlog #37.
- **SIZE:** M · **RISK:** migration.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_storage_memory.py -q -o addopts=`

### #161 Archive #11: Add versioned, SQLite-consistent backups and verified restoration
- **STATUS: OPEN.** No backup/restore module or CLI command (`grep -rn "backup\|restore" lore/` finds only Gemini file-watching); no bundle format, checksums, or restore-into-isolated-dir path.
- **SCOPE:** Add a versioned bundle (SQLite snapshot via backup API/read txn + manifests/checksums/schema/migration metadata), restore to an isolated dir, verified on checksums/counts/identities/FTS/annotations/evidence.
- **ACCEPTANCE:** [ ] versioned bundle excludes credentials; [ ] restore never overwrites a live archive by default; [ ] verification checks checksums/counts/identities/FTS/annotations/evidence; [ ] corrupt/incomplete/incompatible/interrupted bundles fail clearly.
- **FILES:** new `lore/storage/backup.py`, `lore_cli.py`, `lore/storage/schema.py`, new `tests/test_backup_restore.py`.
- **DEPENDS-ON:** #151, #152, #160, #162, #163.
- **SIZE:** L · **RISK:** migration, data-loss.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_backup_restore.py -q -o addopts=`

### #162 Archive #12: Expose archive completeness and source health
- **STATUS: PARTIAL.** Discovery telemetry exists (`lore/services/discovery.py`: `discovery_snapshot`/`record_discovery`/`read_last_discovery`, stored in `store_meta`), surfaced by `lore check --deep` (`lore_cli.py:1641-1683`, commit `e8d5ee2`). Missing: a durable per-source health model with states captured/parsed/filtered/unsupported/failed/unavailable/deleted, last attempt, revision/checksum, error metadata, partial-vs-complete, and backup inclusion.
- **SCOPE:** Persist per-source health (stable JSON contract) and expose counts + records; mark partial results visibly.
- **ACCEPTANCE:** [ ] counts + per-source records for all seven states; [ ] health includes last attempt/revision/checksum/error/prior-content availability; [ ] partial results visibly partial; [ ] report survives restart and is in backup/restore.
- **FILES:** `lore/services/discovery.py`, `lore/storage/schema.py`, `lore/services/extraction.py`, `lore_cli.py`, `tests/test_discovery_telemetry.py`.
- **DEPENDS-ON:** #151, #152, #153, #158, #161, #164.
- **SIZE:** M · **RISK:** none.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_discovery_telemetry.py -q -o addopts=`

### #163 Archive #13: Import portable archive bundles with source provenance
- **STATUS: OPEN.** No import/merge bundle API exists (`grep` finds only unrelated bundle/warp refs).
- **SCOPE:** Add import that merges source revisions, sessions, messages, annotations, health and evidence; dedupe identical revisions, keep divergent ones, never overwrite local implicitly.
- **ACCEPTANCE:** [ ] bundle imports revisions/sessions/messages/annotations/health/evidence; [ ] identical revisions dedupe, divergent stay recoverable; [ ] local content never overwritten implicitly, conflicts reviewable; [ ] no credentials/machine-private paths imported.
- **FILES:** new `lore/storage/import_bundle.py`, `lore/storage/backup.py`, `lore_cli.py`, new `tests/test_import_bundle.py`.
- **DEPENDS-ON:** #151, #155, #160, #161, #162.
- **SIZE:** L · **RISK:** migration, privacy.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_import_bundle.py -q -o addopts=`

### #164 Archive #14: Capture Pi sessions through a tested extractor
- **STATUS: CLOSED 2026-10-14** (evidence comment posted; proof `pytest tests/test_extractor_pi.py` → 41 passed). Was DONE-IN-REPO. `lore/extractors/pi.py` (628 lines) from commit `d2b7d31`; registered; `tests/test_extractor_pi.py` has 35+ tests incl. registration (`:631`), malformed/unknown records (`:454`), thresholds (`:548`), parser-version reuse (`:574`). Focused suite passed (215 passed group).
- **SCOPE:** Close after a release-note entry; nothing left on the issue's listed criteria.
- **ACCEPTANCE:** [x] local Pi layout discovered/documented; [x] supported records extract with stable ids + structured payloads; [x] unsupported records counted with diagnostics; [x] participates in common contract/completeness.
- **FILES:** `lore/extractors/pi.py`, `lore/extractors/factory.py`, `tests/test_extractor_pi.py`.
- **DEPENDS-ON:** #151, #153, #154, #162.
- **SIZE:** S (close) · **RISK:** none.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_extractor_pi.py -q -o addopts=`

### #165 Archive #15: Make transcript formatting complete and independent of AI availability
- **STATUS: PARTIAL.** Exporters iterate canonical `session.messages` in order and work with no provider (no LLM calls in `lore/exporters/markdown.py:96-176` / `html.py:157-219`); Markdown quotes user text and emits reasoning/tool inputs; HTML escapes and emits empty `<p></p>`. Missing: tool results/status (see #166) and derived summaries/titles are not marked as derived (h1 uses raw title, `html.py:159`).
- **SCOPE:** Guarantee every canonical message/event exported in order, preserve exact code whitespace/line endings, mark derived titles/summaries, prove provider-independent.
- **ACCEPTANCE:** [ ] every message/event incl. empty/failed/tool/system; [ ] exact code whitespace/line endings; [ ] summaries/titles clearly derived, never replacing content; [ ] works with all AI providers unavailable.
- **FILES:** `lore/exporters/markdown.py`, `lore/exporters/html.py`, `lore/core/models.py`, `tests/test_markdown_exporter_extended.py`, `tests/test_html_export.py`.
- **DEPENDS-ON:** #154, #156, #157, #166, #169.
- **SIZE:** M · **RISK:** format-compat.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_markdown_exporter_extended.py tests/test_markdown_exporter.py tests/test_html_export.py -q -o addopts=`

### #166 Archive #16: Export canonical tool calls and complete tool results
- **STATUS: PARTIAL.** A canonical `normalize_tool_call` exists (`lore/core/models.py`) and the web UI renders output/error/status (`lore/interfaces/web_formatting.py:529+`, `tests/test_web_formatters.py:64`). But Markdown/HTML exporters emit only `name` + `input` (`markdown.py:145-160`, `html.py:190-200`); v2 `tool_events_json` stores only `{name,status,duration_ms,is_error}` with no args/results (`writer.py:63-97`).
- **SCOPE:** Define one canonical tool-call envelope and export name/args/output/error/native id/status/truncation; keep unmatched/failed visible and deterministic offline.
- **ACCEPTANCE:** [ ] MD+HTML include name/args/output/error/native id/status/truncation; [ ] unmatched/failed distinguishable; [ ] structured args readable without losing JSON; [ ] deterministic and offline.
- **FILES:** `lore/core/models.py`, `lore/exporters/markdown.py`, `lore/exporters/html.py`, `lore/storage/writer.py`, `lore/storage/reader.py`, `tests/test_web_formatters.py`.
- **DEPENDS-ON:** #154, #156, #165, #168.
- **SIZE:** M · **RISK:** format-compat.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_web_formatters.py tests/test_markdown_exporter_extended.py tests/test_html_export.py -q -o addopts=`

### #167 Archive #17: Preserve transcript content during display normalization
- **STATUS: PARTIAL.** Display uses a separate render path (`lore/interfaces/web_formatting.py:318-431` markdown+nh3 sanitize) while canonical text stays in `messages.content`; folding groups only identical consecutive calls and shows the multiplier (`lore/interfaces/session_view_prep.py:65-93`). No test asserts expanded view/export equals the original line-for-line; `format_message_content` transformations (tag stripping, ANSI strip, markdown nl2br) are lossy for display by design and not verified idempotent.
- **SCOPE:** Guarantee normalization is view-only, canonical content is recoverable unchanged in expanded view and export, and repeated normalization is idempotent.
- **ACCEPTANCE:** [ ] folding/normalization changes only the view; [ ] every line incl. blank/indent recoverable expanded + export; [ ] repeated normalization idempotent; [ ] tests cover headings/command noise/code/tool output/Unicode.
- **FILES:** `lore/interfaces/web_formatting.py`, `lore/interfaces/session_view_prep.py`, `lore/exporters/markdown.py`, `tests/test_formatting_utils.py`, `tests/test_web_formatters.py`.
- **DEPENDS-ON:** #154, #165, #168, #170.
- **SIZE:** S–M · **RISK:** none.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_formatting_utils.py tests/test_web_formatters.py tests/test_session_view_prep.py -q -o addopts=`

### #168 Archive #18: Render original roles and tool execution states faithfully
- **STATUS: PARTIAL.** Roles render with distinct classes/labels in the HTML exporter (`html.py:157-219`, `.role-*` CSS `html.py:28-45`) and web templates; tool states are distinguishable when folded (`tests/test_tool_groups.py`, `tests/test_session_detail_markup.py:118-145`). Missing: original role/status metadata in HTML attributes and exports (`grep data-role|data-status` finds nothing) and status in Markdown/HTML exports.
- **SCOPE:** Add accessible role/state labels and machine-readable `data-*` attributes; keep original status in exports; centralize state mapping.
- **ACCEPTANCE:** [ ] system/user/assistant/tool/info distinct labels + visual states; [ ] pending/running/succeeded/failed/cancelled/unmatched distinguishable folded; [ ] role/status metadata in HTML attrs + exports; [ ] no fold removes an error/identity.
- **FILES:** `lore/exporters/html.py`, `lore/templates/session.html`, `lore/templates/session_rows.html`, `lore/interfaces/web_formatting.py`, `tests/test_session_detail_markup.py`.
- **DEPENDS-ON:** #154, #156, #166, #167, #170.
- **SIZE:** M · **RISK:** none.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_session_detail_markup.py tests/test_tool_groups.py -q -o addopts=`

### #169 Archive #19: Render full Markdown in standalone HTML exports
- **STATUS: PARTIAL.** Standalone export is self-contained (inline CSS, no CDN/JS) and escapes hostile content (`lore/exporters/html.py:1-15,107-140`; `tests/test_html_export.py:42-140`). But `_render_content` handles only paragraphs and fenced code — headings, lists, tables, links and inline formatting are not rendered, even though the `markdown` package is a dependency (used only by the web view).
- **SCOPE:** Render full Markdown in the standalone HTML export offline, sanitize hostile input, embed assets, and include provenance.
- **ACCEPTANCE:** [ ] headings/lists/tables/links/inline/fenced code/tool blocks offline; [ ] untrusted HTML/scripts/javascript URLs inert after sanitize; [ ] CSS/JS embedded or vendored; [ ] export includes source/revision/message provenance without external requests.
- **FILES:** `lore/exporters/html.py`, `lore/interfaces/web_formatting.py`, `pyproject.toml`, `tests/test_html_export.py`.
- **DEPENDS-ON:** #165, #166, #167, backlog #26.
- **SIZE:** M · **RISK:** none (sanitization/security).
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_html_export.py tests/test_render_smoke.py -q -o addopts=`

### #170 Archive #20: Create a cohesive, accessible transcript reading experience
- **STATUS: PARTIAL.** Accessibility groundwork landed: ARIA labels, find bar with `aria-live`, focus trapping, keyboard shortcuts, bottom sheets (`lore/templates/session.html`, `lore/templates/base.html`; `tests/test_peek_and_shortcuts.py`, `tests/test_sessions_bulk_and_sheets.py:457`, `tests/test_session_detail_markup.py`). Missing: one deliberate reading contract covering typography across themes, 200% zoom/narrow-layout automated checks, focus/contrast/live-status coverage.
- **SCOPE:** Define and test a single accessible transcript reading contract (typography, keyboard, zoom/narrow layout, focus/contrast) without unrelated redesign.
- **ACCEPTANCE:** [ ] consistent typography/affordances across themes; [ ] keyboard navigate/expand/copy/search/return focus; [ ] narrow + 200% zoom usable without clipping; [ ] focus/contrast/labels/live status meet accessibility target with automated checks.
- **FILES:** `lore/templates/session.html`, `lore/templates/base.html`, `tailwind.input.css`, `lore/interfaces/web_formatting.py`, new `tests/test_a11y_transcript.py`.
- **DEPENDS-ON:** #167, #168, backlog #21, backlog #26, backlog #44.
- **SIZE:** L · **RISK:** none.
- **FIX COMMAND:** `HOME=/tmp/opencode/lore-test-home .venv/bin/python -m pytest tests/test_a11y_transcript.py tests/test_session_detail_markup.py -q -o addopts=`

---

## Summary table

| # | STATUS | SIZE | RISK | Scope |
|---|--------|------|------|-------|
| 151 | OPEN | L | data-loss, privacy | Append-only raw source/attachment revision store before parsing |
| 152 | CLOSED 2026-10-14 | S | data-loss | Verify/close transaction-scoped upserts (`fe1ca50`) |
| 153 | PARTIAL | M | data-loss | Capture all decoded sessions; make quality gate a view filter |
| 154 | PARTIAL | M | format-compat, migration | Add versioned message envelope (native id, full tool calls, extensions) |
| 155 | OPEN | L | data-loss, migration | Namespace session identity, link divergent copies, compat resolver |
| 156 | PARTIAL | M | format-compat | Persist/export full Codex args/results/errors/truncation |
| 157 | PARTIAL | M | format-compat, privacy | Preserve Claude blocks/images/attachments/order end-to-end |
| 158 | PARTIAL | M | none | Directory availability → readable-data availability; preserve undecoded |
| 159 | PARTIAL | M | data-loss | Add memory-search repair + sync-survival tests |
| 160 | PARTIAL | M | migration | Canonical evidence references + unresolved reporting |
| 161 | OPEN | L | migration, data-loss | Versioned SQLite-consistent backup + verified restore |
| 162 | PARTIAL | M | none | Durable per-source health/completeness model |
| 163 | OPEN | L | migration, privacy | Portable bundle import/merge with provenance |
| 164 | CLOSED 2026-10-14 | S | none | Verify/close tested Pi extractor (`d2b7d31`) |
| 165 | PARTIAL | M | format-compat | Complete, provider-independent transcript export; mark derived content |
| 166 | PARTIAL | M | format-compat | Canonical tool-call envelope in MD/HTML export |
| 167 | PARTIAL | S–M | none | View-only normalization guarantees + idempotency |
| 168 | PARTIAL | M | none | Role/state attributes in HTML + exports |
| 169 | PARTIAL | M | none | Full Markdown rendering in standalone HTML export |
| 170 | PARTIAL | L | none | Cohesive accessible transcript reading contract + automated checks |

### NOT VERIFIED
- Whether #152 should also update `CHANGELOG.md`/docs before closing (no release-note entry names it).
- Exact meaning of `#112` referenced by #152 — assumed real GitHub issue per audit doc; not inspected.
- Whether production `LORE_IMPORT_PROFILE` is set anywhere (no dotenv/default outside `base.py`); treated as unset/relaxed.
- No web access/tooling: any provider-format changes since HEAD were not checked against upstream docs.
