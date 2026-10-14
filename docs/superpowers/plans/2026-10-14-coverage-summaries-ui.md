# Coverage, summaries and session view — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkboxes. One task per iteration; the plan file is the loop's memory.

**Goal:** Read every relevant AI tool correctly, add redaction-safe AI session summaries, ship the redesigned session view and finish #197.

**Spec:** `docs/superpowers/specs/2026-10-14-coverage-summaries-ui-design.md`
**Research (schemas, paths, numbers live here, not in this plan):** `docs/research/2026-10-14-*.md`
**Parent issue:** DnaMes/lore#197; follow-ups #198 (exit codes), #199 (phase 1/2 review notes).

## Global constraints

- Python 3.11+, ruff format + check clean, 100-char lines, no bare `except`.
- mypy errors not above the baseline measured at the start of each block (24 at 139a341).
- Migrations additive and idempotent; last existing is 16 (on `main` since 139a341); this plan adds 17 (contract) and 18 (summaries); renumber if something landed meanwhile. The summaries design document says 17: use the number from this plan.
- Persistence decision: `messages` stays one row per turn. Tool calls and results stay inline in `content` in the existing `[Tool: ...]` text format (FTS and summaries read it); migration 17 adds `kind` (`text` | `injected_context` | `compaction_summary`), `parent_id`, `active`, `tool_events_json` (compact `[{name, status, duration_ms, is_error}]`, no arguments or results) and `reasoning` (thinking text, excluded from the FTS body). `UnifiedMessage.tool_calls`/`.reasoning` map onto these.
- Reuse invalidation: `sessions.parser_version` (int, migration 17) and `BaseExtractor.PARSER_VERSION`; an indexed session whose stored parser version is lower than the extractor's is re-extracted even when the source mtime is unchanged. Every extractor task bumps its version.
- Automated sessions (`origin` exec/cron/subagent) are excluded by default from `lore knowledge`, `lore improve` and `lore summarize` as well as from list/search; `--include-automated` widens them.
- SQLite sources of other tools: copy the database and its `-wal`/`-shm` files into a scratch directory and open the copy; never open the original, even read-only (opening can create `-wal`/`-shm` next to it). A copy of a live database is not an atomic snapshot: accept a state that may lag the live one, retry once on a corruption error, never write, and delete the copy afterwards.
- Extractors are read-only on user data; never log message content; never touch auth/creds/token files; open SQLite sources with `mode=ro`.
- Tests: synthetic fixtures built from the documented schemas, under `tests/fixtures/<tool>/` or generated in `tmp_path`. No real home access, no network, no model download, providers faked.
- Public repo: no personal paths, names, project slugs or secrets in code, fixtures, docs or commit messages.
- Test command: `HOME=$TEST_HOME <main-checkout>/.venv/bin/python -m pytest <paths> -p no:cacheprovider -q -o addopts=""` where `$TEST_HOME` is a scratch directory reserved for the suite. Never run the CLI manually with that HOME; use `HOME=$(mktemp -d)`.
- Backward compatible: JSON keys and CLI flags additive; existing exports unchanged.
- Whoever touches `lore/templates/*` or `lore/static/*` also runs `tests/test_vendored_assets.py` and `tests/test_template_render_snapshots.py`: the vendored Tailwind bundle has no rule for classes that are not already used elsewhere (for example `underline`); reuse compiled classes or define semantic CSS in the template style block.

## Night-loop protocol (what each iteration does)

0. **Never develop in the main checkout.** A user-level `lore-sync.timer` may run `lore_cli.py export --all` from the main checkout against the real archive every two hours, so whatever is checked out there runs unreviewed overnight. The main checkout stays on `main`. All work happens in a worktree: `git fetch github && git worktree add <cache>/lore-night/wt-<block> -b feat/197-<block> github/main`; run tests from inside the worktree with the main checkout's interpreter (`cd <worktree> && <main-checkout>/.venv/bin/python -m pytest ...`, the working directory wins on `sys.path`). Remove the worktree when the block has shipped.
1. `git status` and `git log --oneline -5` in the worktree; find the first unchecked task below whose dependencies are checked. The committed docs under `docs/research/`, the spec and this plan are part of the program, not foreign work. If the worktree is dirty with something that is not from the current task, stop and report.
2. Branch: one branch and worktree per block (`feat/197-<block>`), created from `github/main` on the first task of the block.
3. Dispatch ONE `implementer` subagent with: the task text, the Global constraints, the paths to the spec and research docs, the verification commands, and "do not run git write commands; do not run the whole suite". Never run two implementers at once.
4. Verify yourself: run the task's tests, ruff, mypy; read the diff; for tasks tagged **[R]** dispatch a read-only `code-reviewer` on the working tree (security, transactions, migrations, redaction, privacy) and fix every FIX before committing.
5. Hand the explicit paths to `git-ops` for `commit+push` of the branch (never the gate for a single task).
6. Tick the task's checkbox in this file in the same commit as the task, so a restart never repeats finished work.
7. Gate cadence: tasks are committed and pushed to the block branch without the gate; the gate runs once per block, on the branch tip, after the full suite (so reviewers see the block as a whole). End of block: full suite once, record counts, `review-gate` through `git-ops` for the branch tip; on BLOCK fix and ship again, up to 4 rounds, then record in `docs/TODO.md` with a recommended decision and move on to the next independent block.
8. Stop conditions: the same failure survives 4 rounds; a task needs something outside the constraints (cloud call, deploy, data deletion, secret); free disk below 10 GB; the suite baseline gets worse for reasons outside the task. Record the reason in `docs/TODO.md` and stop the loop with a summary.
9. Do not: touch branding, publish, deploy, change global agent configs or hooks, run `lore` against the real archive for writing, force-push, skip hooks.
10. Resource rules: full suite at most once per block; delete scratch dirs you create. **Real-data counts** are taken with a small script under `/tmp` that instantiates the extractor and counts what `extract_sessions()` yields (nothing is written to the archive); `lore check --deep` and every other `lore` command run only with `HOME=$(mktemp -d)`, never against the real `~/.lore` (it would run migrations on the real archive).
11. `docs/TODO.md` is created by the first task that needs it (sections: Open decisions with a recommended decision, Stopped tasks).

## Block B0 — quick wins

### B0.1 Escape `data-session-id` correctly
**Files:** `lore/templates/session.html` (and every template that puts `|tojson` inside an HTML attribute — `grep -rn "tojson" lore/templates`), `tests/test_session_page_attrs.py`.
**Done when:** the attribute value equals the session id exactly after HTML parsing, for ids containing `"`, `'`, `<`, `&`; the page's tag UI JavaScript reads it as a plain string; a test parses the rendered HTML and compares.
- [x] done

### B0.2 Repair the Gemini CLI extractor **[R]**
**Files:** `lore/extractors/gemini.py`, `tests/test_gemini_extractor.py` (update what pins the old `.json` behaviour), new `tests/test_extractor_gemini_jsonl.py`, fixtures built in `tmp_path`.
**Spec:** dossier section 2 "Gemini CLI". Accept `session-*.jsonl` (and legacy `session-*.json`); directory names are slugs, not 64-char hashes; replay `$set`, `$patch` (`updates`, `removeIds`, `orderIds`, `toolCalls`) and `$rewindTo`, last write per message id wins; `content` is `str | list[{text}]`; map `type` user/gemini/info/error/warning; tool calls, thoughts, tokens, model; header `kind` main/subagent and subagent files under `chats/<parent>/`; project path from `<tmp>/<slug>/.project_root`, then `~/.gemini/projects.json`, then the old hash walk; `.jsonl` malformed line is skipped and counted.
**Done when:** fixtures for set-only, set+patch, rewind, subagent, malformed line and a version bump pass; a read-only run on the real home reports sessions > 0 (report the number).
- [x] done

## Block B1 — contract

### B1.1a Model fields and migration 17 **[R]**
**Files:** `lore/core/models.py`, `lore/storage/schema.py`, `lore/storage/writer.py`, `lore/storage/reader.py`, `tests/test_contract_fields.py`.
**Interfaces:** `UnifiedSession.origin: str = "unknown"`, `.parent_session_id: Optional[str] = None`, `.relation: Optional[str] = None`, `.source_version: Optional[str] = None`, `.parser_version: int = 0`; `UnifiedMessage.kind: str = "text"`, `.parent_id: Optional[str] = None`, `.active: bool = True` (existing `tool_calls`/`reasoning` fields get persisted through `tool_events_json`/`reasoning`). Migration 17 adds nullable/defaulted columns on `sessions` and `messages` as in the persistence decision. Allowed values are constants in `lore/core/models.py`.
**Done when:** a v16 DB with data opens at v17 unchanged; writer → reader round trip preserves every field including `reasoning` and `tool_events_json`; old extractors need no change; `load_session_messages_v2` returns the new fields; existing suites stay green.
- [x] done

### B1.1b Index JSON, reuse paths and parser-version invalidation **[R]**
**Files:** `lore/exporters/index.py`, `lore/storage/writer.py` (`_write_reused_entry`, `_refresh_complete_session`), `lore/extractors/base.py` (`PARSER_VERSION`, `_source_unchanged`), `tests/test_parser_version_reuse.py`.
**Done when:** new keys appear additively in the index JSON and in the web/MCP payloads; a session indexed with parser version 0 is re-extracted when the extractor declares version 1 although its source mtime is unchanged; an up-to-date session is reused without a parse; reused entries carry the new fields.
- [x] done

### B1.2 Discovery telemetry and `lore check` warnings
**Files:** `lore/extractors/base.py`, every extractor (counters only), `lore_cli.py` (`cmd_check`), `tests/test_discovery_telemetry.py`.
**Interfaces:** `BaseExtractor.discovery: dict[str, int]` with keys `files_matched`, `files_parsed`, `records_unknown`; `skip_counts` stays as is. `lore check` (availability only, as today) gains `--deep`, which runs every extractor without writing the archive and prints per tool: sources found, sessions parsed, skipped by reason, unknown records, and a `WARNING` line when sources exist and sessions parsed is 0, naming the glob. The sync job records the same counters in `store_meta` so a normal `lore check` can show the last run.
**Done when:** a fixture with a stale glob produces the warning; counts match the fixture.
- [x] done

### B1.3 Origin policy and "show automated"
**Files:** `lore/extractors/base.py` (`should_import_session`, `_effective_thresholds`), `lore/services/index.py`, `lore/interfaces/web.py` + templates, `lore_cli.py`, MCP `list_sessions`/`search_history`, `tests/test_origin_policy.py`.
**Rules:** sessions with `origin` in (`exec`, `cron`, `subagent`) bypass `MIN_USER_PROMPTS` but keep `MIN_MESSAGES`; every list/search surface and every LLM stage (`lore knowledge`, `improve`, `summarize`) skips them unless `include_automated` (CLI `--include-automated`, web query param and a toggle, MCP argument). Counts in dashboards say how many are hidden.
**Done when:** default listings are unchanged for interactive-only fixtures; an `exec` fixture appears only with the switch; documented in README.
- [x] done

### B1.4 Gate follow-ups for block B1 **[R]**
From the B1 review gate notes: (1) `_index_entry_to_session` in `lore_cli.py` keeps only `origin`; keep `parent_session_id`, `relation`, `source_version`, `parser_version` too; (2) MCP `include_automated` must be parsed strictly (`bool("false")` is True today): accept real booleans and the strings true/false/1/0, anything else is an error string; apply to every MCP tool and web/CLI parameter that takes it; (3) cap the over-fetch loop in `lore/search/engine.py` (for example 8 rounds or 2000 rows); (4) `files_parsed` is counted for a `None` parse result in `cursor.py` and `records_unknown` is double counted in `vscode.py`: count only real results; (5) set `parent_session_id` and `relation="subagent"` for Claude and Gemini subagent sessions when the parent id is known from the path; (6) `automated_kept` is incremented before message/content limits are checked: increment only when the session is actually imported; (7) `LORE_PARSER_REEXTRACT_BUDGET=0` means "no re-extraction this run" (stale sources reused, changed sources still parsed), invalid values fall back to 150, negative is unlimited; update tests and README; (8) a thread consisting only of automated sessions requested by id must be returned (test), and the "show" URL must be autoescaped (test with `"><script>` in the query string); (9) README note: the first sync after the upgrade re-extracts sessions in batches of the budget; (10) test hygiene from the gate: lengthen the `ok` fixture session to pass `MIN_CONTENT_SIZE`, replace the vacuous `payload is not other` assert with a real identity assertion, drop the dead `setenv("3")`, use `monkeypatch` instead of writing `os.environ`, assert a stable field instead of log wording where cheap.

(4) vscode double count not reproducible; pinned by a test.
- [x] done

## Block B2 — extractors (each task also adds: `Tool` enum value, alias in `normalize_tool_name`, CLI/web filter entry, README coverage row)

Common acceptance for B2.x: fixtures cover user/assistant text, tool call with result, usage/model where the tool records them, a malformed line, an unknown record type, and a format version bump; tree/branch tools prove the active branch; a read-only count on the real home is reported in the commit body; no content is logged. All **[R]** because they parse untrusted files.

### B2.1 Pi (version 3 tree) — dossier "Pi"
Map `session`, `message` (roles user/assistant/toolResult/system/bashExecution), `model_change`, `thinking_level_change`, `compaction` (→ message kind `compaction_summary`, relation info), `branch_summary`, `context_edit` (target `active=False`), `custom_message` (kind `injected_context`), `custom`, `label`, `session_info`; active branch from the last leaf by `parentId`; usage and cost; sibling roots `~/.omp/agent/sessions`, `~/.prime/agent/sessions` when present; skip `subagent-artifacts`.
- [x] done

### B2.2a Codex content enrichment — dossier "Codex"
`custom_tool_call` + `custom_tool_call_output`, `function_call_output`, `agent_message`, `event_msg.token_count`, `turn_context` model/effort/cwd, `compacted`; injected AGENTS.md/environment context typed `injected_context` and excluded from the user-prompt count; bump `PARSER_VERSION`.
- [x] done

### B2.2b Codex origin and layout
Subagent parent and `origin` from `session_meta.source`/`originator` (`exec` → exec), `archived_sessions/`, optional `.jsonl.zst` only if a zstd module imports, `state_5.sqlite` titles via a scratch copy.
- [x] done

### B2.3 Antigravity from plaintext transcripts — dossier "Antigravity"
Read `brain/<id>/.system_generated/logs/transcript_full.jsonl` (else `transcript.jsonl`) under `antigravity`, `antigravity-cli`, `antigravity-ide`; map `source` USER_EXPLICIT/MODEL/SYSTEM, `type`, `thinking`, `tool_calls`; titles and workspace from `conversation_summaries.db` (read-only). Do not decode `.pb`; keep the old `task.md` path as fallback.
- [x] done

### B2.4 Command Code — dossier "Command Code"
`projects/<slug>/<uuid>.jsonl` (v3-style and flat variant), `.meta.json`, `.checkpoints.jsonl` for print-mode runs without a transcript (prompts from `prompt`); usage with `costUsd`; never read `auth.json`.
- [x] done

### B2.5 Hermes — dossier "Hermes"
`~/.hermes/profiles/*/state.db` and `~/.hermes/state.db`: sessions + messages, skip `active=0`, `parent_session_id` relation, per-model usage, `origin` from `source`; work on a scratch copy of the database and its WAL/SHM files; a per-session fingerprint derived from `last_activity_at` is stored in the existing `source_mtime_ns` field, so the base contract does not change. Invariant, with a test: the value changes whenever the session's content changes, and nothing but equality is ever done with it for this source.
- [x] done

### B2.6 Claude Code enrichment — dossier "Claude Code"
Thinking blocks, `message.model`, `ai-title`/`custom-title`, `isMeta`, `isCompactSummary` (kind `compaction_summary`), `system.compact_boundary`, `cost-state`, `isSidechain` + `agentId` relation, `tool-results/` sidecars when referenced.
- [x] done

### B2.7a Cursor agent transcripts — dossier "Cursor"
`~/.cursor/projects/*/agent-transcripts/*/*.jsonl` (no timestamps, no results; plain-text legacy variant) as a new source next to the existing IDE reader; `origin` from the source kind.
- [x] done

### B2.7b Cursor IDE store enrichment
Bubble `createdAt`/`modelInfo`/`thinking`/`toolFormerData`, `composerHeaders` for timestamps and archive flags (scratch copy of `state.vscdb`); never persist `blobEncryptionKey` or other key fields. A test asserts that no field whose name contains `key`, `secret`, `token` or `encryption` from composer data reaches the database or the index JSON.
- [x] done

### B2.8 Wave 2 (separate small tasks; fixtures from the documented schemas, flagged "not verified on real data" in the README row)
- [x] B2.8a Cline / Roo Code / Kilo (shared task JSON, VS Code globalStorage and Cline CLI)
- [x] B2.8b OpenCode v2 tables (`session_v2`, `session_message`) alongside the existing reader
- [x] B2.8c Goose (`sessions.db`) and Crush (`crush.db` via `projects.json`)
- [x] B2.8d Kimi CLI / Kimi Code (`wire.jsonl`) and Qwen Code (`chats/*.jsonl`)
- [x] B2.8e Factory Droid (`sessions/<slug>/<uuid>.jsonl`) and Zed (`threads.db`, zstd optional)

## Block B3 — safety layer

### B3.1 Redaction module **[R]**
**Files:** `lore/utils/redact.py`, `tests/test_redact.py`.
**Interfaces:** `redact(text: str) -> RedactionResult` with `text: str`, `count: int`, `kinds: dict[str, int]`; patterns documented in the module docstring; idempotent; never raises; long input handled in linear time.
**Done when:** fixtures with fake keys of each kind are replaced by `[REDACTED:<kind>]`; ordinary code, hashes shorter than 40 chars and UUIDs are not touched; a property-style test confirms `redact(redact(x)) == redact(x)`.
- [x] done

### B3.2 Provider hardening **[R]**
**Files:** `lore/llm/gemini.py`, `lore/llm/ollama.py`, `lore/llm/base.py`, `tests/test_llm_provider_privacy.py`.
**Rules:** `LLMConfig.cache_enabled` already exists for Gemini: it is set to False for transcript-bearing tasks and a test proves no cache file is written; the Ollama provider replaces its hard-coded `options` with a passthrough that accepts `num_ctx`; `lore knowledge` and the summary task call `redact()` on the prompt text before sending and report the redaction count.
**Done when:** tests prove no cache file is written for a transcript task and that `num_ctx` reaches the request.
- [x] done

### B3.3 Redaction for exports and sharing
`lore export --redact` (markdown/HTML/JSON) and every future share feature call `redact()`; plain exports stay unredacted by design because they are local files the user asked for, and the README says so.
- [x] done

## Block B4 — summaries (design: `docs/research/2026-10-14-session-summaries-design.md`)

### B4.1 Deterministic artifacts and compressor
`lore/services/summary_input.py`: `extract_artifacts(messages) -> Artifacts(files_read, files_modified, commands, errors)` and `compress(messages, version=1) -> CompressedTranscript` with the rules in the design (tool results head/tail, caps, collapse runs of tool-only messages, native compaction kept and flagged). Pure functions, fixtures with noisy tool output.
- [x] done

### B4.2 Storage (migration 18) **[R]**
`lore/storage/summaries.py` with `session_summaries` as in the design; `get_summary`, `save_summary` (only after validation), `delete_summaries_for_session`, `list_summaries`; tombstoned sessions never returned. Wire `delete_summaries_for_session` (and `session_knowledge` cleanup) into the same code path that records a deletion tombstone in the web, MCP and CLI, with a test per path, plus a test that a tombstoned session's summary stays deleted after a re-sync (`_write_reused_entry` must not resurrect it).
- [x] done

### B4.3a Single-pass pipeline and validator **[R]**
`lore/services/summarize.py`: candidate selection (interactive only by default), redaction before send, single pass, validation (schema, cites exist, lengths, no secret-shaped strings), write only after validation, `unchanged` counting; fake-provider tests.
- [x] done

### B4.3b Chunking, merge, repair and budgets **[R]**
Chunks cut at user messages and never inside a call/result pair, merge call, one repair retry then a smaller chunk size, max 8 chunks with sampling and `truncated`, budget caps and dry-run estimate, a growing session re-runs only its last chunk and the merge; native compaction reuse.
- [x] done

### B4.4 CLI `lore summarize`
Flags as in the design (`--session`, `--project`, `--since`, `--tool`, `--provider`, `--model`, `--allow-cloud`, `--dry-run`, `--force`, `--max-sessions`, `--max-input-tokens`, `--level`, `--json`); provider announced before any send; exit 1 when every attempted call failed.
- [x] done

### B4.5 MCP tools and docs **[R]**
`get_session_summary` (read-only, staleness), `summarize_session` gated by `LORE_MCP_ALLOW_SUMMARIZE=1` and local-only providers; `docs/API_REFERENCE.md`, README section.
- [x] done

## Block B5 — session view (order from the UX report, section 11)

### B5.1 Fixes and compact rows
Compact two-line rows (use a list/listitem structure with real links and buttons; the mock's `role=option` rows with interactive children are not a valid pattern) with tokens, duration, time and project chip; drop the per-row delete (menu only); remove the duplicated outline line; fix the 390 px clipping; snapshot tests updated only where the diff is the intended markup.
- [x] done

### B5.2 Code blocks, tool status, thinking
Single-frame code block with language, line count, Copy and collapse above ~24 lines; tool cards show ok/failed and duration instead of "N CHARS"; thinking in `<details>`.
- [ ] done

### B5.3 Day grouping and sticky header
Day headers (with the pagination hand-off so a header is not repeated), date chips, group toggle; sticky compact detail header with Resume primary and the rest in a menu.
- [ ] done

### B5.4 Summary card and list line (after B4.4)
Detail card from `session_summaries` when present, deterministic "at a glance" otherwise. The Generate button works only with a local provider configured through `LORE_SUMMARY_PROVIDER`/`LORE_SUMMARY_MODEL`; without it the button is disabled and shows the equivalent `lore summarize` command. Cloud providers are never reachable from the web. ✦ one-liner under the title only when a summary exists.
- [ ] done

### B5.5 Tool-group folding and merged toggles
Consecutive tool calls grouped into one `<details>` with counts and status; merge the five view toggles into Tools [hide|fold|open], Thinking [fold|show], Density.
- [ ] done

### B5.6 Mobile sheets and bulk select
Port the mock's keyboard handlers with guards: ignore events with ctrl/meta/alt modifiers and targets that are inputs, textareas or contenteditable; carry the CSP nonce on any script/style block.
Outline and filters as bottom sheets with focus trap; explicit select mode with bulk actions.
- [ ] done

### B5.7a In-session find
Find across all pages with highlight and n/N.
- [ ] done

### B5.7b Peek pane (≥1280 px)
- [ ] done

### B5.7c Keyboard shortcuts (`j k e c / ?`)
- [ ] done

## Block B6 — remaining #197

### B6.1 Ratings only on confirmed memories
`record_feedback` raises `ValueError` unless the memory is confirmed; update the test that pins the old behaviour; docs.
- [ ] done

### B6.2 `lore improve` **[R]** (after B4.3b)
`lore/services/improve.py` per the 2026-10-13 design 3.2 with `embed_memories`, `extract_knowledge`, `distill_lessons` and a new `from_summaries` stage (outcome, decisions, errors → candidates with `memory_sources`); stage report table and `--json`; model output only ever creates candidates.
- [ ] done

### B6.3 Review queue
`lore memory review|accept|reject` and the web "Candidates" tab; rejected rows kept and used by dedupe.
- [ ] done

### B6.4 `lore context`
Per the 2026-10-13 design 4.1; never another project's memory.
- [ ] done

## Block B7 — small, high-evidence

### B7.1 Retention warning in `lore check`
Read the Claude Code settings for `cleanupPeriodDays` (read-only); warn when the default 30 days applies and print the setting to raise it. Never write the user's settings.
- [ ] done

### B7.2 Cost columns
Per-session tokens and cost from stored usage and an offline price table (`lore/utils/pricing.py`, fixture-tested, unknown model → no cost, never a guess); show in list/detail and `lore list`.
- [ ] done

## Self-review

- Spec coverage: A → B1; B → B0.2, B2; C → B3; D → B4; E → B0.1, B5; F → B6; G → B7. Owner decisions are not tasks.
- Dependencies: B1.1a and B1.1b before every B2.x; B3 before B4.3a; B4.2 before B4.3a; B4.4 before B5.4; B4.3b before B6.2; B5.2 and B5.5 rely on `tool_events_json` from B1.1a.
- Known soft spots: wave-2 extractors are unverified on real data; hiding automated sessions changes defaults (B1.3 documents it).
