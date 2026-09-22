# Lore archive audit and implementation backlog

Date: 2026-09-12  
Repository: `DnaMes/lore`  
Audited revision: `015b5e0`  
Scope: local archive durability, transcript fidelity, presentation, search, AI
processing, provider boundaries, packaging, deployment, and release QA.

## Executive summary

Lore has a useful local-first foundation, but the current storage contract is
still a rebuild-oriented session viewer rather than a loss-resistant archive.
The most important risk is that `StreamingV2Writer.begin()` deletes all session
rows and dependent messages before a sync has demonstrated complete source
coverage. A missing source, partial extractor failure, or filtered sync can
therefore remove data that Lore had already captured. The representation also
does not round-trip native message IDs, reasoning, tool-call arguments/results,
or arbitrary provider fields through the v2 SQLite store.

The second major boundary is conceptual: `BaseExtractor.should_import_session`
applies a quality filter during capture and defaults to three non-empty user
prompts. That is appropriate for a curated view, but not for an archive that
promises to retain short tasks and subagent runs. Filtering must become a
reversible view over captured source material.

The rest of the backlog makes those guarantees usable: source revisions and
health reporting, verified backups, stable evidence links, complete exports,
faithful rendering, bounded loading, honest provider execution, reviewable
learnings, correct distribution identity, portable deployment, and browser
release checks.

This report is an implementation handoff, not a claim that all source devices,
backups, or historical files have been reconciled. The previously reported
local snapshot of 606 sessions, 119,857 messages, and 1,409 ignored entries
was not re-read during this audit because no user archive was placed in scope.

## Method and reproduced evidence

- Read the current storage, model, extractor, service, exporter, interface,
  packaging, and deployment code at the audited revision.
- Ran the focused v2 writer tests in an isolated temporary `HOME`: 3 passed.
  The existing `test_write_sessions_is_full_replace` intentionally confirms
  the destructive behavior, so it is a characterization test, not an archive
  guarantee.
- Ran a synthetic `UnifiedMessage` through the v2 writer. The live schema was
  `id, session_id, seq, role, content, timestamp, model, tokens_json`; the
  stored row retained role/content/model/tokens but had no place for the native
  message ID, reasoning, or tool-call structure.
- Ran a synthetic one-prompt session through the default filter. It returned
  `False` and recorded `{'too_few_user_prompts': 1}`.
- Existing tests run against isolated homes; no test touched a real user
  archive. Embedding and tool directories remain outside the test scope.

## Product decisions carried into the issues

1. Original source records and relevant attachments are captured before
   interpretation, with checksum, provenance, and revision metadata. Unknown
   records remain recoverable and are reported as unsupported rather than
   discarded. Credential files are excluded from import.
2. The archive is append-only/upsert-oriented; views, quality filters,
   summaries, search indexes, and AI artifacts are derived and rebuildable.
3. Every message and derived claim has a stable identity and evidence link.
4. Existing Python, SQLite, Flask/Jinja, CLI names, `lore` imports, and export
   compatibility remain unless an explicitly versioned migration says
   otherwise.
5. Native installed CLIs and local models are opt-in adapters. Authentication
   stays with the selected tool; there is no credential copying or implicit
   paid API fallback.

## Dependencies and existing issues

The storage redesign must be coordinated with #112 (true upserts and
incremental processing), #48 (JSON retirement only after parity and restore),
#92 (semantic relevance calibration), #119 (Blueprint refactor), #33 (shared
memory direction), and #29 (MCP transport). The issue numbers below are audit
backlog identifiers, not GitHub numbers. Triage should classify duplicates and
assign labels/priorities in a separate pass.

## Issue-ready backlog

## 01 — Capture immutable source revisions before parsing

**Evidence / need:** Extractors currently yield `UnifiedSession` objects from
provider files (`lore/extractors/*`), while the v2 writer stores only a source
path and current mtime (`lore/storage/writer.py`). There is no raw-record or
revision table. An unknown event or a parser bug can therefore make the
original input unrecoverable.

**Acceptance criteria:**

- Capture raw source records and relevant non-secret attachments before
  interpretation, keyed by source identity, checksum, byte size, and revision.
- Preserve unknown or unsupported events and expose their decode status.
- Re-running against an unchanged source is idempotent; divergent content
  creates a new revision instead of overwriting the old one.
- Explicitly exclude credential/config files from the capture boundary.

**Dependencies:** #02, #04, #05, #08, #11.

**Implementation prompt:** Audit every extractor's source boundary and design an
append-only SQLite/source-artifact store. Add synthetic fixtures for known,
unknown, malformed, and attachment records; verify reconstruction after the
source files are removed. Keep raw data separate from rendered/AI data.

## 02 — Preserve archived content when sources disappear or extraction fails

**Evidence / reproduction:** `StreamingV2Writer.begin()` in
`lore/storage/writer.py` executes `DELETE FROM sessions` and
`DELETE FROM search_index` before the stream is complete. `build_search_index`
in `lore/services/extraction.py` continues after extractor errors, so a
partial or empty result can be committed as the new archive.

**Acceptance criteria:**

- A missing source, partial extractor exception, empty filtered sync, or
  cancelled write never deletes previously captured sessions/messages.
- A source is marked unavailable/failed with error metadata rather than
  interpreted as an authorized deletion.
- Only an explicit user deletion creates a tombstone and removes derived views.
- A failed transaction leaves the last committed archive and search index
  intact.

**Dependencies:** #01, #09, #11, #12, #112.

**Implementation prompt:** Reproduce the failure with an isolated SQLite store
and two syncs, then replace full-replace semantics with a transaction-safe
upsert/reconciliation policy. Add tests for missing, failed, filtered, empty,
cancelled, and explicit-delete cases. Do not change user archives in tests.

## 03 — Separate archival capture from session-quality filtering

**Evidence / reproduction:** `lore/extractors/base.py:17` sets
`MIN_USER_PROMPTS = 3`, and `should_import_session` returns false before the
session reaches storage. The synthetic one-prompt session is rejected as
`too_few_user_prompts`.

**Acceptance criteria:**

- Every successfully decoded session, including short tasks and subagent
  runs, is retained in the archive.
- Quality filtering is a reversible query/view concern with an explicit
  profile, not a destructive capture gate.
- Existing curated list behavior remains available as a named filter/profile.
- Sync reports captured, filtered, unsupported, and failed counts separately.

**Dependencies:** #01, #02, #12, #14.

**Implementation prompt:** Add a failing fixture for one-message, one-prompt,
subagent, and metadata-only sessions. Move the threshold decision after raw and
canonical capture; preserve CLI/UI compatibility by making the current relaxed
or strict view explicit and documented.

## 04 — Round-trip structured messages through SQLite without information loss

**Evidence / reproduction:** `lore/core/models.py` defines `message_id`,
`tool_calls`, and `reasoning`, but `lore/storage/schema.py:72-81` has no columns
for them. `lore/storage/writer.py` inserts only role, content, timestamp, model,
and `tokens_json`; `lore/storage/reader.py` reads the same reduced shape.

**Acceptance criteria:**

- Native message IDs, roles, timestamps, model, tokens, reasoning, tool calls,
  status, and provider-specific fields survive write/read round trips.
- Unknown fields are preserved in a versioned JSON extension payload.
- Null, malformed, and very large structured fields have defined behavior and
  are never silently dropped.
- Legacy rows migrate safely and remain readable.

**Dependencies:** #05, #06, #07, #10.

**Implementation prompt:** Define a versioned canonical message envelope and
SQLite migration. Use fixtures from at least Claude, Codex, and OpenCode and
assert literal field equality after a write/read cycle, including unknown
extensions and failed tool calls.

## 05 — Preserve divergent session copies and namespace source identities

**Evidence / need:** `sessions.id` is the sole primary key and the writer uses
`INSERT OR REPLACE`. Provider IDs can collide across tools, projects, machines,
or divergent copies, while `source_path` is only a nullable column.

**Acceptance criteria:**

- The canonical identity includes provider/source namespace and a stable
  source-record identity, not just an unqualified provider ID.
- Byte-identical copies deduplicate while divergent histories remain separately
  addressable and linked as copies.
- Local annotations and evidence links survive merges.
- Existing session IDs remain readable through a compatibility resolver.

**Dependencies:** #01, #04, #10, #13.

**Implementation prompt:** Build fixtures with same IDs from two providers,
identical copies, and divergent revisions. Specify identity/merge rules before
implementing schema changes; test that no `INSERT OR REPLACE` silently destroys
the earlier copy.

## 06 — Preserve Codex tool results and original message roles

**Evidence / need:** `lore/extractors/codex.py` maps rollout events into the
`UnifiedMessage` model, while tool calls/results can be interleaved, failed, or
unmatched. The reduced v2 writer cannot pair or preserve these events.

**Acceptance criteria:**

- Tool calls and results pair by native identity when available.
- Interleaved, failed, cancelled, and unmatched events remain visible in
  original order with their original roles and status.
- Arguments, output, errors, and truncation metadata survive export and read.
- Unknown Codex event types remain attached to their source revision.

**Dependencies:** #01, #04, #18, #16.

**Implementation prompt:** Create rollout fixtures for paired, failed,
interleaved, and orphaned events. Test extractor output, storage round trip,
Markdown/HTML export, and UI preparation independently.

## 07 — Preserve Claude structured content and model metadata

**Evidence / need:** `lore/extractors/claude.py` parses JSONL records with
structured content blocks, persisted thinking, usage, attachments, and model
metadata. The v2 message schema stores only flattened text plus a model field.

**Acceptance criteria:**

- Text, thinking/reasoning, non-text blocks, attachments, usage, model, and
  native IDs survive extraction and storage.
- Content block order and block type are stable across exports.
- Redaction/secret policy is explicit and tested; no credential file is imported.
- Older flattened rows remain renderable without pretending they are complete.

**Dependencies:** #01, #04, #15, #19.

**Implementation prompt:** Use representative Claude JSONL fixtures, including
thinking, tool use, tool results, image/document blocks, and malformed lines.
Compare canonical output to hand-written expected envelopes, not to extractor
helpers.

## 08 — Report Antigravity coverage accurately and preserve undecoded conversations

**Evidence / need:** `lore/extractors/antigravity.py` can report availability
based on a directory while finding no transcript records. The audit requirement
is to distinguish task artifacts from actual conversations.

**Acceptance criteria:**

- Availability means readable conversation data exists, not merely an install
  directory.
- Task artifacts, transcript files, and unsupported formats are separately
  counted and described.
- Undecoded conversations are preserved as source artifacts and never counted
  as successfully parsed sessions.
- A diagnostic reports availability, captured, parsed, unsupported, and failed.

**Dependencies:** #01, #12, #14, #30.

**Implementation prompt:** Inspect realistic Antigravity fixtures and compare
with the VS Code storage discovery pattern. Add tests for empty install trees,
valid conversations, and unsupported artifacts; update the live probe without
claiming coverage that was not observed.

## 09 — Keep memory search entries intact during session rebuilds

**Evidence / need:** `search_index` is cleared by the v2 full-replace writer,
while `lore/storage/memory.py` stores agent-authored memory and its search rows
in the same database. A session rebuild can remove searchable memory entries.

**Acceptance criteria:**

- Full, incremental, empty, failed, and cancelled session syncs preserve all
  memory rows, tags, embeddings, and searchable entries.
- Session-derived FTS rows can be rebuilt independently of memory rows.
- A repair command detects and reconstructs missing memory search rows without
  altering memory content.
- Tests prove memory search survives every supported sync path.

**Dependencies:** #02, #11, #33, #48.

**Implementation prompt:** Reproduce the shared-FTS deletion in an isolated DB.
Split rebuild ownership or use scoped deletes/upserts, then test memory writes,
semantic entries, and search repair before and after session syncs.

## 10 — Keep message evidence references stable across reindexing

**Evidence / need:** `memory_sources.message_id` is an integer loose reference
(`lore/storage/schema.py:157-162`), while messages use an autoincrement SQLite
row ID and the model has a separate optional native `message_id`. Rebuilding
rows can change the integer reference.

**Acceptance criteria:**

- Evidence references use a stable canonical session/message identity and
  source revision, not a volatile rowid.
- Reindexing, backup/restore, and archive merge preserve resolvable citations.
- Missing legacy references are reported with an explicit unresolved state.
- Evidence exports include enough provenance to verify the cited span.

**Dependencies:** #01, #04, #05, #11, #37.

**Implementation prompt:** Add fixtures with reindex and restore cycles. Resolve
references through a public API and assert that a cited message remains the
same after row order or SQLite row IDs change.

## 11 — Add versioned, SQLite-consistent backups and verified restoration

**Evidence / need:** No archive backup/restore contract currently exists; the
dual JSON/SQLite paths can be at different revisions. A file copy without a
consistent snapshot cannot prove message/evidence integrity.

**Acceptance criteria:**

- CLI/API creates a versioned bundle containing SQLite data, source manifests,
  checksums, schema version, and migration metadata, excluding credentials.
- Restore targets an isolated directory and never overwrites a live archive by
  default.
- Verification checks checksums, counts, message identities, FTS rebuildability,
  annotations, and evidence links.
- Corrupt, incomplete, incompatible, and interrupted bundles fail clearly.

**Dependencies:** #01, #02, #10, #12, #13.

**Implementation prompt:** Use SQLite backup or a consistent read transaction,
not a raw copy of a live WAL database. Add round-trip, tamper, interruption,
and version-compatibility tests with temporary directories only.

## 12 — Expose archive completeness and source health

**Evidence / need:** `build_search_index` returns extractor error/skip reports,
but there is no durable source-health model. Users cannot distinguish captured,
parsed, filtered, unsupported, failed, unavailable, and intentionally deleted.

**Acceptance criteria:**

- CLI/API exposes counts and per-source records for captured, parsed, filtered,
  unsupported, failed, unavailable, and explicitly deleted inputs.
- Health includes last attempt, source revision/checksum, error type/message,
  and whether prior content remains available.
- Partial results are visibly partial and do not claim archive completeness.
- The report survives restart and is included in backup/restore.

**Dependencies:** #01, #02, #03, #08, #11, #14.

**Implementation prompt:** Define status transitions and a stable JSON contract.
Exercise each state with synthetic source fixtures and verify that a failed
source leaves its previous canonical content searchable.

## 13 — Import portable archive bundles with source provenance

**Evidence / need:** There is no import/merge API for moving an archive between
machines. Existing session IDs and loose memory references are not enough to
merge divergent copies safely.

**Acceptance criteria:**

- A bundle from another machine imports source revisions, canonical sessions,
  messages, annotations, health, and evidence provenance.
- Identical revisions deduplicate; divergent revisions remain recoverable.
- Local content is never overwritten implicitly; conflicts are reported with a
  reviewable resolution choice.
- Credentials and machine-private paths are not imported as secrets.

**Dependencies:** #01, #05, #10, #11, #12.

**Implementation prompt:** Specify merge precedence and namespace rules, then
test two temporary archives containing identical, divergent, deleted, and
locally annotated sessions. Verify import is idempotent.

## 14 — Capture Pi sessions through a tested extractor

**Evidence / need:** `lore/extractors/factory.py` registers current tools, but
there is no tested Pi extractor despite Pi sessions being in the stated target
coverage. Unknown local Pi data has no explicit unsupported path.

**Acceptance criteria:**

- Discover the existing local Pi source layout and document tested coverage.
- Supported records extract with stable source/message identities and complete
  structured payloads.
- Unsupported records remain as source artifacts with honest diagnostics.
- The extractor participates in the common contract and completeness report.

**Dependencies:** #01, #03, #04, #12.

**Implementation prompt:** Research only local fixtures or documented public
formats, avoid credentials, and add positive, malformed, empty, and unknown
record tests before registering the extractor.

## 15 — Make transcript formatting complete and independent of AI availability

**Evidence / need:** `lore/exporters/markdown.py` and `lore/exporters/html.py`
are derived from canonical sessions, while current AI summaries and title
paths can use truncated excerpts. A transcript export must not depend on an
optional provider or silently omit content.

**Acceptance criteria:**

- Markdown and HTML export every canonical message and structured event in
  order, including empty, failed, tool, and system messages.
- Exact code whitespace and line endings are preserved in clipboard/export
  content.
- Summaries/titles are clearly marked as derived and never replace transcript
  content.
- Exports work with all AI providers unavailable.

**Dependencies:** #04, #06, #07, #16, #19.

**Implementation prompt:** Create long and structured fixtures, compare exported
message counts and literal code blocks, and disable all AI adapters in tests.

## 16 — Export canonical tool calls and complete tool results

**Evidence / need:** Tool-call shapes have historically diverged (`name` vs
`tool`, `arguments` vs `input`), and rendering currently depends on derived
normalization. Results/errors/truncation must be first-class export content.

**Acceptance criteria:**

- Markdown and HTML include tool name, canonical arguments, output, error,
  native call/result identity, status, and truncation metadata.
- Unmatched and failed calls remain visible and distinguishable.
- Structured arguments are readable without losing their original JSON value.
- Export output is deterministic and works offline.

**Dependencies:** #04, #06, #15, #18.

**Implementation prompt:** Define the canonical tool-call envelope, then use
fixtures for success, failure, malformed arguments, large/truncated output,
and unmatched results. Assert both semantic fields and literal code output.

## 17 — Preserve transcript content during display normalization

**Evidence / need:** `lore/interfaces/web_formatting.py` normalizes and folds
content for display. The audit identified line suppression as a risk: display
normalization must not become a second lossy parser.

**Acceptance criteria:**

- Folding/normalization changes only the view; original canonical content is
  available unchanged.
- Every substantive line, including blank lines and code indentation, remains
  recoverable in expanded view and export.
- Repeated normalization is idempotent.
- Tests cover headings, command noise, code fences, tool output, and Unicode.

**Dependencies:** #04, #15, #18, #20.

**Implementation prompt:** Add a fixture whose lines differ only by whitespace
and markers. Assert expanded text and export against hand-written expected
content, and keep the original message object immutable.

## 18 — Render original roles and tool execution states faithfully

**Evidence / need:** `Role` includes system/tool/info, but presentation helpers
and fold heuristics can collapse role and execution-state distinctions. A
reader must be able to tell who emitted content and whether execution failed.

**Acceptance criteria:**

- System, user, assistant, tool, and info messages have distinct accessible
  labels and visual states.
- Pending, running, succeeded, failed, cancelled, and unmatched tool events
  remain distinguishable when folded.
- Original role/status metadata is present in HTML attributes and exports.
- No heuristic fold removes an error or identity.

**Dependencies:** #04, #06, #16, #17, #20.

**Implementation prompt:** Add role/state fixtures and test prepared view data,
rendered HTML, keyboard expansion, and exports. Keep state mapping centralized.

## 19 — Render full Markdown in standalone HTML exports

**Evidence / need:** `lore/exporters/html.py` must be usable without the Flask
runtime or network assets. Lists, tables, headings, links, code, and hostile
HTML need a deterministic offline rendering/sanitization contract.

**Acceptance criteria:**

- Standalone HTML renders headings, lists, tables, links, inline formatting,
  fenced code, and tool blocks offline.
- Untrusted HTML/scripts/javascript URLs remain inert after sanitization.
- CSS/JS assets required for reading and copying are embedded or vendored.
- Export includes source/revision/message provenance without external requests.

**Dependencies:** #15, #16, #17, #26.

**Implementation prompt:** Build an offline export fixture and inspect it with a
  parser. Test hostile input, every supported Markdown construct, no-network
  loading, and copy behavior.

## 20 — Create a cohesive, accessible transcript reading experience

**Evidence / need:** Templates in `lore/templates/` and the CSS/JS layer have
  grown through incremental fixes. Long transcripts, narrow layouts, keyboard
  users, and zoomed text require one deliberate reading contract.

**Acceptance criteria:**

- Roles, timestamps, tools, code, and derived content use consistent typography
  and affordances across themes.
- Keyboard users can navigate, expand, copy, search, and return focus.
- Narrow layouts and 200% zoom remain usable without clipped transcript content.
- Focus, contrast, labels, and live status meet the project’s accessibility
  target and are covered by automated checks.

**Dependencies:** #17, #18, #21, #26, #44.

**Implementation prompt:** Use populated synthetic sessions, run a keyboard and
  responsive browser pass, and add focused HTML assertions. Keep visual changes
  scoped to transcript reading rather than unrelated redesign.

## 21 — Add durable message permalinks and bounded transcript loading

**Evidence / need:** Session routes currently prepare a whole session for
  display, while v2 already has ordered message rows. There is no durable
  message permalink or bounded page contract.

**Acceptance criteria:**

- Every canonical message has a stable URL fragment/route resolvable after
  reindexing and restore.
- Page/limit APIs load and format only the requested bounded segment.
- A deep link opens the exact message and supplies surrounding context without
  fetching the whole transcript.
- Missing/deleted/unresolved message identities return explicit responses.

**Dependencies:** #04, #10, #18, #20, #25.

**Implementation prompt:** Add API and route contract tests for first/middle/last
  pages, deep links, deleted messages, and restored archives. Measure that the
  bounded path does not materialize preceding pages.

## 22 — Fix date filtering across time zones and inclusive end dates

**Evidence / need:** Date parsing/filtering spans `lore/utils/datetime.py` and
  web/service query paths. Mixed naive/aware timestamps and date-only end
  values can exclude sessions on DST boundaries or the selected final day.

**Acceptance criteria:**

- Inputs define a timezone and normalize timestamps consistently.
- Date-only ranges include the complete end date in the selected timezone.
- DST transitions, UTC offsets, naive legacy values, and mixed provider formats
  have tested behavior.
- CLI, web, API, and MCP filters agree on the same result set.

**Dependencies:** #12, #24, #25.

**Implementation prompt:** Write table-driven tests with literal UTC/local
  expectations around DST boundaries and midnight. Centralize parsing rather
  than adding route-specific corrections.

## 23 — Connect saved tags and bookmarks to archive filtering

**Evidence / need:** `lore/storage/tags.py` persists session tags, but list and
  search filters are still primarily derived from flat index payloads. Tags and
  bookmarks must survive rebuilds and affect counts/results.

**Acceptance criteria:**

- Saved tags/bookmarks appear in CLI, API, web, and MCP filters.
- Counts, pagination, and empty states reflect tag constraints.
- Tag updates are durable across full/incremental sync, reindex, backup, and
  restore.
- Unknown/deleted session tags are handled without losing the annotation.

**Dependencies:** #02, #11, #24, #25.

**Implementation prompt:** Add an isolated v2 fixture, apply tag mutations, run
  every list/search surface, then rebuild/restore and assert exact tags and
  result counts.

## 24 — Apply search filters before limiting candidate results

**Evidence / need:** Search paths in `lore/services/index.py`,
`lore/storage/search.py`, and web/MCP callers combine ranking, filters, and
limits. Limiting an unfiltered candidate list can hide matching projects/tools.

**Acceptance criteria:**

- Tool, project, date, tag, role, and scope filters constrain candidates before
  ranking/limit.
- A query whose top unfiltered results belong elsewhere still returns the best
  matching filtered sessions.
- FTS, semantic, hybrid, CLI, web, API, and MCP behavior is consistent.
- Invalid filters return explicit validation errors.

**Dependencies:** #22, #23, #25, #40.

**Implementation prompt:** Create a fixture where the first N ranked results do
  not satisfy the filter. Assert filtered results and total counts with literal
  expected IDs across every public search entry point.

## 25 — Show searchable message evidence and paginated results

**Evidence / need:** Existing search responses are session-oriented and have
  finite result limits; message-level evidence and complete pagination are not
  consistently exposed.

**Acceptance criteria:**

- Each hit includes matching message/source evidence, excerpt, stable message
  identity, and a permalink when available.
- Pagination/cursors make every match reachable beyond the first ten results.
- Filters apply before page boundaries and totals are explicit.
- Redaction/truncation is labelled and never presented as the full evidence.

**Dependencies:** #04, #10, #21, #24, #40.

**Implementation prompt:** Seed more than ten matching messages across sessions
and page through the API/CLI/web contracts. Assert exact excerpts and links,
including late-session matches.

## 26 — Unify code-block enhancement and preserve exact clipboard content

**Evidence / need:** Templates and delegated JavaScript enhance code blocks on
  initial and appended content. Multiple enhancement paths can duplicate
  controls or copy UI labels along with code.

**Acceptance criteria:**

- One idempotent enhancement path handles initial and paginated/appended blocks.
- Copy returns exactly the code text, including whitespace, without button
  labels or status text.
- Keyboard activation and success/failure feedback are accessible.
- CSP remains clean and no inline event handler is reintroduced.

**Dependencies:** #20, #21, #44.

**Implementation prompt:** Add browser tests for initial, appended, repeated,
and empty code blocks under both themes. Assert clipboard literal content and
console/CSP output.

## 27 — Hydrate archived transcripts before AI analysis and formatting

**Evidence / need:** `lore_cli.py`, `lore/llm/tasks/*`, and title/formatting paths
can receive session metadata or empty message lists instead of the archived
message body. Optional analysis must fail explicitly when hydration is not
possible.

**Acceptance criteria:**

- AI tasks receive the canonical archived messages, not an empty reconstruction
  or lossy excerpt by default.
- Source revision and message coverage are included in task input metadata.
- Missing/unavailable bodies produce an explicit failed/partial job state.
- Formatting and export remain fully functional when AI is disabled.

**Dependencies:** #04, #10, #15, #28.

**Implementation prompt:** Use a fake provider that records its complete input
and fixtures with late messages/tool results. Assert hydration, coverage, and
explicit unavailable behavior without contacting a network API.

## 28 — Process complete sessions with explicit analysis coverage

**Evidence / need:** Existing summaries/knowledge tasks in `lore/llm/tasks/`
and `lore/digest.py` use bounded excerpts or batch limits without a durable
coverage model. Late corrections can be missed silently.

**Acceptance criteria:**

- Long sessions are processed in bounded, ordered chunks covering all messages.
- The final synthesis includes late corrections and reports covered revision,
  message range/count, and any omitted content.
- Batch/token limits are honored and visible in job metadata.
- Partial coverage is never reported as complete analysis.

**Dependencies:** #04, #27, #29, #36.

**Implementation prompt:** Build a long synthetic session with a decisive late
message. Use a deterministic fake provider, force multiple chunks, and assert
ordered coverage and final synthesis metadata.

## 29 — Persist resumable analysis jobs without replacing previous results

**Evidence / need:** AI jobs are currently tied to in-process task execution;
  previous result artifacts and interrupted work do not have a durable,
  revision-aware job contract.

**Acceptance criteria:**

- Jobs persist request identity, source revision, chunk progress, provider,
  model, errors, and completion state.
- Interruption resumes unfinished chunks idempotently.
- New runs preserve old artifacts and do not replace them with partial output.
- Cancellation, timeout, provider failure, and retry states are distinguishable.

**Dependencies:** #11, #27, #28, #30, #36.

**Implementation prompt:** Add a temporary SQLite job fixture and fake provider
that fails mid-session. Kill/restart the worker, resume, and assert no duplicate
chunks and preservation of the earlier result.

## 30 — Introduce explicit native-CLI and local-model provider boundaries

**Evidence / need:** `lore/llm/factory.py`, `gemini.py`, and `ollama.py` mix
provider selection, invocation, and credential assumptions. The product
decision requires explicit destination and no implicit paid fallback.

**Acceptance criteria:**

- Provider identity, model, auth mode, timeout, cancellation, and execution
  policy are explicit persisted request fields.
- Native CLI adapters invoke the selected executable with native auth; tokens
  are never copied into API requests or another tool's config.
- Local Ollama remains a supported opt-in path.
- Missing binaries, auth failures, timeouts, and non-zero exit codes are clear.

**Dependencies:** #29, #31, #32, #33, #34, #35.

**Implementation prompt:** Define an adapter interface and use fake executables
and local endpoints for tests. Assert command/env boundaries and that selecting
one provider cannot route data to another.

## 31 — Add a native Codex CLI analysis adapter

**Evidence / need:** The extractor supports Codex records, but no explicit
native Codex analysis adapter validates installed CLI capabilities, structured
output, cancellation, or authentication mode.

**Acceptance criteria:**

- Detect and report the installed Codex CLI/version/capabilities.
- Invoke it through its documented native authentication and structured-output
  interface with bounded input and timeout/cancellation.
- Parse success, malformed, partial, and failed results into the common job
  contract.
- Fake executables cover command construction without real credentials.

**Dependencies:** #27, #29, #30, #36.

**Implementation prompt:** Implement only the adapter boundary first, with fake
executables for success, stderr, timeout, cancellation, and malformed JSON.
Document supported CLI versions and unsupported capability behavior.

## 32 — Add a native Claude Code analysis adapter

**Evidence / need:** Claude session extraction exists, but analysis currently
does not have a clearly isolated adapter using the unmodified installed CLI and
its native authentication.

**Acceptance criteria:**

- Detect CLI availability/version and supported structured output.
- Use native authentication without embedding subscription login or copying
  credentials into Lore.
- Enforce selected model, timeout, cancellation, and source-coverage metadata.
- Fake executable tests cover success and all failure states.

**Dependencies:** #27, #29, #30, #36.

**Implementation prompt:** Read the installed CLI's official interface at
implementation time, isolate subprocess policy, and test argv/env/output with
fake executables. Do not make network/API assumptions.

## 33 — Add a native Antigravity CLI analysis adapter

**Evidence / need:** Antigravity coverage is already uncertain (#08), and no
explicit native analysis adapter defines structured results, restricted
execution, or subscription-credit behavior.

**Acceptance criteria:**

- Adapter availability is false unless a supported CLI and capability are
  present.
- Invocation is restricted, bounded, cancellable, and uses native auth/credit
  settings only.
- Structured success, unsupported, auth, timeout, and malformed output states
  are persisted distinctly.
- Fake executable tests cover policy enforcement.

**Dependencies:** #08, #29, #30, #36.

**Implementation prompt:** Confirm the supported CLI contract before coding;
never infer it from the IDE data directory. Keep unsupported conversations
archived as source artifacts and return explicit adapter unavailability.

## 34 — Add a native Gemini CLI analysis adapter

**Evidence / need:** `lore/llm/gemini.py` includes provider-specific logic, but
the requirement is native authentication and no repurposing of CLI tokens for
API requests.

**Acceptance criteria:**

- Missing Gemini CLI is reported without a silent fallback.
- Native authentication and documented structured output are used.
- CLI credentials are not copied into Google API/client configuration.
- Fake executable tests cover success, auth failure, timeout, cancellation,
  malformed output, and non-zero exit.

**Dependencies:** #27, #29, #30, #36.

**Implementation prompt:** Separate CLI and API adapters explicitly. Test the
boundary with controlled environment variables and fake binaries; inspect that
no token material crosses the boundary.

## 35 — Route generated titles through the explicitly selected provider

**Evidence / need:** `lore/titles/generator.py` has provider-specific title
paths and optional fallbacks. Installing or detecting another CLI must not
silently change where transcript excerpts are sent.

**Acceptance criteria:**

- A title request records and honors the explicitly selected provider/model.
- No implicit provider fallback sends content elsewhere; unavailable selected
  providers return a clear local failure or use an explicit fallback setting.
- Excerpts, source revision, prompt version, and result status are recorded.
- Existing fast/local title behavior remains compatible and testable.

**Dependencies:** #27, #30, #36.

**Implementation prompt:** Add a fake-provider routing fixture with multiple
installed adapters and assert only the selected adapter sees the input.
Exercise missing-provider and explicit-fallback cases.

## 36 — Version AI caches by source revision and effective request identity

**Evidence / need:** Existing title/analysis outputs are derived from session
content but lack a uniform cache key covering source revision, provider, model,
parameters, and prompt version. Concurrent writes risk stale results.

**Acceptance criteria:**

- Cache keys include canonical source revision, effective provider/model,
  parameters, prompt version, task type, and coverage range.
- Source or request changes invalidate only affected artifacts.
- Old results remain queryable with their request metadata.
- Concurrent workers cannot corrupt or cross-associate results.

**Dependencies:** #01, #10, #28, #29, #30, #35.

**Implementation prompt:** Define literal cache-key fixtures and test mutations
of each component, concurrent writes, retries, and source revision changes.
Use SQLite transactions/unique constraints rather than process-local dicts.

## 37 — Promote generated knowledge into memory with reviewable evidence

**Evidence / need:** `lore/storage/memory.py` supports memory and
`memory_sources`, but generated summaries/rules do not have a review workflow
that distinguishes observation, hypothesis, confirmed rule, and supersession.

**Acceptance criteria:**

- Generated claims are stored as reviewable candidates with source/message
  spans, source revision, model/request metadata, and confidence/status.
- Observation, hypothesis, confirmed, rejected, and superseded states are
  distinct.
- Human review/accept/reject/supersede preserves prior artifacts and evidence.
- Export includes the evidence needed to audit each active rule.

**Dependencies:** #10, #29, #36, #38, #39.

**Implementation prompt:** Build a candidate-to-confirmed fixture with a
contradicting later message and assert provenance/state transitions. Do not
activate a harness rule as a side effect of generation.

## 38 — Build evidence-backed workflow analysis

**Evidence / need:** The product direction asks for workflow insights, but
metrics can easily confuse observed session behavior with inferred outcomes.
Current session metadata and git fields are insufficiently defined for such
claims.

**Acceptance criteria:**

- Every metric has a documented definition, source fields, and coverage limits.
- Observed events, computed metrics, and inferred outcomes are separate types.
- Git/session correlation reports ambiguity and missing data rather than
  inventing causation.
- Results link to supporting messages/source revisions and are reproducible.

**Dependencies:** #10, #12, #37, #40.

**Implementation prompt:** Define a small metric vocabulary and synthetic
fixtures with missing, conflicting, and late events. Assert calculations from
hand-derived expectations and label all inferences.

## 39 — Export reviewed harness recommendations with evaluation evidence

**Evidence / need:** `lore/utils/rules.py` can generate rules, but the durable
product requirement is project-scoped, evidence-backed recommendations that
need deliberate activation and account for corrections/contradictions.

**Acceptance criteria:**

- Recommendations export with scope, evidence links, generation request, and
  evaluation/correction history.
- Rules remain inactive until explicit user adoption.
- Contradictory or superseded evidence is visible and updates status.
- Export formats are portable and machine-readable as well as human-readable.

**Dependencies:** #10, #37, #38, #44.

**Implementation prompt:** Use a fixture with positive, corrected, and
contradictory observations. Test review, activate, supersede, and export paths;
assert no generated rule modifies harness configuration automatically.

## 40 — Add message-level semantic retrieval and multilingual evaluation

**Evidence / need:** Current hybrid search is session-level and the open
calibration issue #92 notes that vectors can return irrelevant nearest
neighbors. Long German/English sessions also need late-message retrieval.

**Acceptance criteria:**

- Message/chunk retrieval can find relevant late-session passages with stable
  evidence links.
- German, English, mixed-language, code, and negative-query fixtures measure
  recall, precision/false positives, and ranking behavior.
- Relevance thresholds are calibrated from evaluation data and configurable,
  not a magic constant.
- Missing local embedding support degrades honestly to FTS.

**Dependencies:** #04, #10, #24, #25, #92, #97.

**Implementation prompt:** Create a versioned evaluation set with hand-checked
relevance labels. Measure baseline FTS, semantic, and hybrid results before
implementing threshold/chunk changes; keep model downloads out of unit tests.

## 41 — Correct the Python distribution identity and installation instructions

**Evidence / need:** `pyproject.toml` currently declares `name = "lore"`, while
the audit found that PyPI's `lore` name resolves to an unrelated Instacart
project. README quickstart says `pip install lore` and can install the wrong
package, even though the Python import must remain `lore`.

**Acceptance criteria:**

- Select and verify an available distribution name (currently proposed:
  `lore-archive`) at release time.
- Clean-venv installation resolves to this project and provides all documented
  CLI entry points.
- `import lore` remains backward-compatible.
- README, Docker, CI, badges, and release docs use the corrected distinction.

**Dependencies:** #42, #44.

**Implementation prompt:** Check current package-name availability immediately
before release, update packaging metadata and install tests, and document the
migration from any prior package name. Do not claim availability without a
fresh index check.

## 42 — Ship a portable default Docker Compose deployment

**Evidence / need:** `docker-compose.yml` and the Dockerfile have historically
assumed workstation-specific paths, private DNS, external networks, and
optional middleware. A clean host must start the local-first app with no hidden
infrastructure.

**Acceptance criteria:**

- Clean-host `docker compose up` starts the app without private DNS, external
  networks, Redis/Postgres, or workstation-only middleware.
- Host archive mounts are explicit, least-privilege, and documented.
- Container runs as a non-root user and preserves archive permissions.
- Health/readiness checks, offline assets, and optional semantic dependencies
  are honest and tested.

**Dependencies:** #11, #30, #41, #44.

**Implementation prompt:** Test with a temporary empty host directory and no
private network. Build/run the image, exercise health and one archive sync,
then verify file ownership and offline asset behavior.

## 43 — Validate legitimate same-origin requests behind supported proxies

**Evidence / need:** Request origin/scheme/host handling in
`lore/interfaces/web_utils.py` and state-changing routes must work behind a
configured proxy without accepting unrelated origins.

**Acceptance criteria:**

- Supported proxy headers are trusted only under explicit configuration.
- Configured public scheme/host/port are accepted for legitimate same-origin
  requests, including non-default ports and TLS termination.
- Unrelated origins, malformed headers, and spoofed forwarded headers are
  rejected consistently for state-changing routes.
- Tests cover direct, trusted-proxy, untrusted-proxy, and mixed-header cases.

**Dependencies:** #20, #42, #44.

**Implementation prompt:** Model the proxy boundary with Flask test requests and
explicit config. Assert security decisions at the request boundary, not merely
that a helper was called.

## 44 — Require real browser interaction checks before release

**Evidence / need:** The existing suite covers templates and CSP contracts, but
the handoff records that a populated-session interactive browser pass remains
outstanding. Click flows can still fail despite static checks.

**Acceptance criteria:**

- A repeatable browser test fixture populates sessions/messages, tags, search
  matches, pagination, code blocks, and navigation.
- Search, copy, tags, pagination, themes, resume/navigation, and delete-confirm
  flows are exercised with keyboard and narrow viewport checks.
- No unexpected console errors, CSP violations, failed requests, or dead
  controls occur.
- The release checklist records browser version, fixture, command, and result.

**Dependencies:** #20, #21, #23, #25, #26, #43.

**Implementation prompt:** Use the repository's browser tooling and isolated
  synthetic data. Keep the app pointed at a temporary archive, capture failures
  with screenshots/logs, and run the full focused suite after fixing regressions.

## Shared implementation prompt

> Read the repository instructions, this audit evidence, and linked
> dependencies. Reproduce the stated failure with synthetic fixtures and an
> isolated archive. Implement the issue's acceptance criteria while preserving
> existing interfaces and unrelated work. Never run destructive tests against
> the user's archive. Keep original source data separate from presentation and
> AI-derived artifacts. Update affected documentation in the same change. Run
> focused checks and report observed results, limitations, and migration
> requirements. Do not commit, deploy, or publish unless separately authorized.

## Separate triage prompt

> Triage this Lore audit backlog using the repository's existing labels. Read
> issue bodies, comments, and linked dependencies before classifying. Distinguish
> reproduced defects, capability gaps, and research tasks. Detect duplicates,
> including overlap with #112, #48, #92, #119, #33, and #29. Propose type,
> priority, component, dependencies, and implementation readiness with a short
> rationale. Give preservation and verified restoration precedence over
> presentation and enrichment. Do not treat source disappearance as authorized
> archive deletion. Return a reviewable classification table; do not implement
> fixes, close issues, or apply labels in this pass.
