<!-- Research report, agent-generated on 2026-10-14 for issue #197 follow-up work.
     Facts marked NV / UNVERIFIED were not verified by the author. Star counts and
     dates are a snapshot. Local-machine observations describe one developer machine. -->

# Lore coverage dossier

Method: I inspected the real local data read-only (structure only) and cloned open-source parsers with `--depth 1`: `wesm/agentsview`, `Dicklesworthstone/coding_agent_session_search` (cass), `Dicklesworthstone/franken_agent_detection` (fad), `badlogic/pi-mono`, `google-gemini/gemini-cli`. All clones are deleted. Anything marked NV was not verified on this machine.

## 1. Table

Parsers: A = agentsview (Go, MIT), C = cass/fad (Rust). Effort and risk are 1-5. Market relevance (Mkt) is H/M/L.

| Tool | Location (Linux; macOS and Windows noted where they differ) | Format | Parsers | Eff | Risk | Mkt |
|---|---|---|---|---|---|---|
| **Gemini CLI** (fix) | `~/.gemini/tmp/<slug>/chats/session-*.jsonl`; same under `~` on all OS | JSONL, `$set`/`$patch`/`$rewindTo` | A, C | 2 | 3 | H |
| **Antigravity** (fix) | `~/.gemini/antigravity{,-cli,-ide}/brain/<id>/.system_generated/logs/transcript.jsonl`; `conversations/<id>.db` | JSONL plus SQLite with protobuf blobs | A, C | 2 | 4 | M |
| **Codex** (fix) | `~/.codex/sessions/Y/M/D/rollout-*.jsonl`, `archived_sessions/`; `.jsonl.zst` | JSONL | A, C | 2 | 3 | H |
| **Cursor** (fix) | `~/.config/Cursor/User/globalStorage/state.vscdb`; `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl`; macOS `~/Library/Application Support/Cursor/...`; Windows `%APPDATA%\Cursor\...` | SQLite KV plus JSONL | A, C | 3 | 4 | H |
| **Claude Code** (enrich) | `~/.claude/projects/<slug>/*.jsonl` and `<sid>/subagents/*.jsonl` | JSONL | A, C, many | 2 | 2 | H |
| **OpenCode** (check) | `~/.local/share/opencode/opencode.db` or `storage/`; none on this machine | SQLite or JSON | A, C | 2 | 3 | M |
| **Pi** (new) | `~/.pi/agent/sessions/--<cwd>--/<ts>_<uuid>.jsonl` | JSONL v3 tree | A, C (also OMP, Prime, OMO) | 2 | 2 | H (here) |
| **Command Code** (new) | `~/.commandcode/projects/<slug>/<uuid>.{jsonl,meta.json,checkpoints.jsonl}` | JSONL (v3 tree, or flat variant) | A | 1 | 3 | M |
| **Hermes** (new) | `~/.hermes/profiles/<p>/state.db`, `~/.hermes/state.db` | SQLite | A, C | 2 | 3 | M |
| Cline | CLI `~/.cline/data/sessions/<id>.json` plus `.messages.json`; VS Code `globalStorage/saoudrizwan.claude-dev/tasks/<ts>/` | JSON | A (CLI), C | 2 | 3 | H |
| Roo Code | `globalStorage/rooveterinaryinc.roo-cline/tasks/<id>/{history_item,ui_messages}.json` | JSON | A | 2 | 3 | M |
| Kilo | new `~/.local/share/kilo/` (OpenCode format); legacy `globalStorage/kilocode.kilo-code/tasks/` | SQLite or JSON | A | 2 | 3 | M |
| Zed | `~/.local/share/zed/threads/threads.db`; empty here | SQLite, `data` zstd or JSON | A | 2 | 3 | M |
| Goose | `~/.local/share/goose/sessions/sessions.db` (v1.20+); `%APPDATA%\Block\goose\data\sessions\`; legacy `~/.goose/sessions/*.jsonl` | SQLite | A, C | 2 | 2 | M |
| Crush | `~/.local/share/crush/projects.json` pointing at `<proj>/.crush/crush.db` | SQLite, `parts` JSON | A, C | 2 | 2 | M |
| Kimi CLI / Kimi Code | `~/.kimi/sessions/<ws>/<id>/wire.jsonl`; `~/.kimi-code/sessions/.../agents/<a>/wire.jsonl` | JSONL | A, C | 2 | 3 | M |
| Qwen Code | `~/.qwen/projects/<p>/chats/*.jsonl`, `subagents/`; legacy `tmp/<hash>/chats/session-*.json` | JSONL | A, C | 2 | 3 | M |
| Factory Droid | `~/.factory/sessions/<ws-slug>/<uuid>.jsonl` plus `.settings.json` | JSONL, Claude-like | C | 2 | 3 | M |
| Amp | `~/.local/share/amp/threads/T-<uuid>.json` (A marks "deprecated") | JSON | A, C | 1 | 4 | L |
| OpenHands | `~/.openhands/conversations/<id>/{base_state.json,events/event-NNNNN-<uuid>.json}` | JSON events | A, C | 2 | 2 | M |
| Kiro CLI / IDE | `~/.kiro/sessions/cli/<uuid>.jsonl` plus `.json`; IDE `.chat` / `workspace-sessions/*.json` | JSONL (`{version,kind,data}`) | A, C | 2 | 4 | M |
| Continue.dev | `~/.continue/sessions/<id>.json` (full snapshot) | JSON | C | 1 | 2 | M |
| Junie (CLI only) | `~/.junie/sessions/<id>/events.jsonl` plus `index.jsonl`; IDE chats not exposed | JSONL | A | 2 | 4 | M |
| JetBrains AI Assistant | per-project XML under the IDE config dir, `<component name="ChatSessionStateTemp">` (forum report; exact path NV) | XML | none known | 4 | 5 | M |
| Windsurf | legacy chat in `~/.config/Windsurf/User/workspaceStorage/<h>/state.vscdb` (ItemTable `...aichat.chatdata`); Cascade is `~/.codeium/windsurf/cascade/*.pb`, proprietary (single forum report, NV) | SQLite / protobuf | A (legacy only) | 3 / 5 | 4 / 5 | H |
| Trae IDE | `~/.config/Trae/User/`; modern layout encrypted (A only detects it); TRAE CLI is Codex-format rollouts in `~/.trae/cli/sessions/` | SQLite / JSONL | A | 3 / 1 | 5 / 2 | M |
| Augment | Auggie `~/.augment/` holds `session.json` (credentials) and `settings.json`; the history path is undocumented (`auggie session list --json` exists). No open parser | NV | none | 4 | 5 | L |
| Copilot Chat (VS Code) | `workspaceStorage/<id>/chatSessions/*.{json,jsonl}` | JSON / JSONL | Lore has it; A, C | done | | H |
| ChatGPT / Claude desktop | cloud history; exports only (`conversations*.json`); ChatGPT macOS v1 is plain JSON, v2/v3 encrypted (cass) | JSON | A (import-only), C | 2 | 2 | H |

No local history was found for Goose on this machine (only `config.yaml`) or for VS Code (no `~/.config/Code/User/*Storage`). Claude Desktop keeps no local chat bodies, only macOS Cowork sidecars.

## 2. Drift findings, group A

**Gemini CLI: stale, extracts nothing today**
- `gemini.py:116` globs `session-*.json`, but current files are `session-<ts>-<id8>.jsonl`. `:131` does `json.load`, which fails on JSONL.
- `gemini.py:109` skips any directory not 64 characters long. Current directories are slugs (`my-project`, `work`, ...). The 64-char hash is still stored as `projectHash` in the header.
- Verified locally: 15 files, 17 slug dirs, zero 64-char dirs, zero `.json` sessions.
- Current format, from `chatRecordingService.ts` and `chatRecordingTypes.ts`:
  - Line 1 is the header `{sessionId, projectHash, startTime, lastUpdated, kind:'main'|'subagent'}`. It may be re-emitted partially with `summary`, `directories`, `memoryScratchpad`.
  - Following lines are plain message records `{id, timestamp, type:'user'|'gemini'|'info'|'error'|'warning', content}`, or `{"$set":{messages:[...],lastUpdated,...}}` (full replace, as in all local files), `{"$patch":{id|updates[],content,toolCalls[],removeIds[],orderIds[]}}`, or `{"$rewindTo":"<msgId>"}`. Last write wins per message id.
  - `content` is `string | Part[]` (`[{text}]` locally). The extractor assumes a string.
  - `gemini` messages carry `toolCalls[{id,name,args,result,status,timestamp,displayName,agentId}]`, `thoughts[{subject,description,timestamp}]`, `tokens{input,output,cached,thoughts,tool,total}`, `model`.
  - Subagent sessions are at `chats/<parentSessionId>/<subSessionId>.jsonl`.
- Resolve the project path from `<tmp>/<slug>/.project_root` (verified, plain absolute path) or `~/.gemini/projects.json` `{"projects":{path:slug}}`, instead of brute-force hashing in `_build_hash_map`.
- Local content is user messages only (15 sessions, one prompt each), so the assistant side of the parser is NV on real data.

**Antigravity: `.pb` is not decodable, but plaintext sources exist**
- `antigravity.py:7` says `.pb` is "not decoded". Local state: 18 legacy `.pb` files (about 7.99 bits/byte entropy, so encrypted) and 246 `conversations/<id>.db` SQLite files, one per conversation.
- The `.pb` files are AES-encrypted. agentsview (`antigravity_crypto.go`) reads the key from an `ANTIGRAVITY_KEY` env var, treats this as a last resort and calls it a removal candidate.
- Do not decode `.pb`. Use the plaintext transcript at `brain/<id>/.system_generated/logs/transcript.jsonl`, also under `antigravity-cli/` and `antigravity-ide/`:
  - Present for 470 conversations locally.
  - Keys: `step_index, source (USER_EXPLICIT|MODEL|SYSTEM), type (USER_INPUT|PLANNER_RESPONSE|GENERIC|SYSTEM_MESSAGE|ERROR_MESSAGE|CHECKPOINT), status, created_at (RFC3339), content, thinking, tool_calls[{name,args}], truncated_fields`.
  - `transcript_full.jsonl` and `chunks/` also exist. `transcript.jsonl` may truncate large fields, so prefer `transcript_full.jsonl` when present (NV which is more complete).
- `conversations/<id>.db` tables are `trajectory_meta, steps(idx, step_type, status, metadata, step_payload BLOB, ...), gen_metadata, executor_metadata, parent_references`. Payloads are schemaless protobuf with no public `.proto`; agentsview ships a wire walker, which is fragile.
- `~/.gemini/antigravity/conversation_summaries.db` is plaintext and verified. It has `conversation_summaries(conversation_id, title, preview, step_count, last_modified_time, workspace_uris, agent_name, parent_conversation_id, nesting_depth, project_id)` plus an FTS table with `user_body` and `agent_body`.
- `antigravity.py` reads only `task.md` and `walkthrough` (lines 77-78). It gets no real conversation, no thinking, no tools, and no workspace.

**Codex: parses, but loses most of the content**
- Verified 1,247 rollouts, cli_version 0.154-0.162. Top-level keys are `{timestamp, type, payload, ordinal[, metadata]}`.
- Top-level types seen: `session_meta, response_item, event_msg, turn_context, compacted, world_state, token_usage_record, retained_context, inter_agent_communication_metadata`.
- Handled by `codex.py:85-188`: `session_meta`, `response_item.{message,reasoning,function_call}`. Dead code: `:87` legacy top-level `message`.
- Not handled:
  - `response_item.custom_tool_call` (26k records: `name, input, call_id`) and `custom_tool_call_output`.
  - `function_call_output` (1,757), so no tool results at all.
  - `agent_message` (`author, recipient, content`).
  - `event_msg.token_count` (`info.total_token_usage`, `last_token_usage`) and `token_usage_record`.
  - `turn_context.model`, `effort`, `cwd` (model is never recorded).
  - `compacted` (`message`, `replacement_history`).
  - `item_completed` items (`CommandExecution`, `FileChange`, `Reasoning`, `AgentMessage`, `SubAgentActivity`).
  - Subagent linkage: `session_meta.source` is a dict `{subagent:{thread_spawn:{parent_thread_id}}}`, plus `parent_thread_id`, `agent_role`, `forked_from_id`.
- `codex exec` is `source:"exec"`, `originator:"codex_exec"`: 1,096 of 1,284 sessions. Separately, the `source` values were cli 70, vscode 15.
- `base.py:18` `MIN_USER_PROMPTS=3` drops about 1,117 sessions with 2 user-role items (usually 1 real prompt plus injected context). `user_prompt_count` also counts injected AGENTS.md and environment context as user prompts.
- Layout: only `Y/M/D` is globbed; `archived_sessions/` and `.jsonl.zst` are missed. Cheap indexes exist: `state_5.sqlite.threads(id, rollout_path, title, first_user_message, tokens_used, model, git_branch, cwd, archived)` and `thread_history_1.sqlite`.

**Cursor: text-only, flat timestamps**
- `cursor.py:139-158` reads `composerData.fullConversationHeadersOnly` and then `bubbleId:<c>:<b>`, taking only `text`, and stamps every message with `created_at` (`:158`). Locally `_v` is 18 (agentsview documents `_v:16` composers and `_v:3` bubbles), and 40 of 44 composers have headers. `conversation` and `conversationMap` are empty, so the `:124` path is dead.
- Bubbles carry far more than is used: `type` (1 user, 2 ai), `createdAt`, `startedAtMs`, `completedAtMs`, `tokenCount`, `modelInfo`, `thinking` and `allThinkingBlocks`, `toolFormerData`, `toolResults`, `requestId`.
- `composerData` has `modelConfig.modelName`, `usageData`, `subagentComposerIds`, `unifiedMode`, `status`, `name`. There is a `composerHeaders` table (`composerId, createdAt, lastUpdatedAt, isArchived, isSubagent`).
- `lastUpdatedAt` is present in only 19 of 44 composers, so use the headers table.
- Secrets: `composerData` contains `blobEncryptionKey` and `speculativeSummarizationEncryptionKey`. Never copy raw composer JSON into the index.
- New stores not read: `agentKv:blob:*` (13k content-addressed blobs, mixed protobuf and JSON) and `ItemTable`.
- Not read at all: `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` (151 files, the agent CLI and background agents). Records are `{role, message:{content:[{type:'text'|'tool_use', text|name,input}]}}` plus `{type:'status',status}`. There are no timestamps and no tool results. Older transcripts are plain text with `user:`/`assistant:` markers (agentsview).

**Claude Code: works, lossy**
- Verified locally: 277 main plus 2,010 subagent files, CC 2.1.27x-2.1.29x. Lines are DAG nodes with `uuid, parentUuid, isSidechain`.
- Missing:
  - Titles: `claude.py:276` reads only the legacy `summary` record. Current titles come from `{"type":"ai-title","aiTitle":...,"sessionId"}` (cass tests use `title`, so accept both). `custom-title`, `agent-name`, `last-prompt` are NV.
  - Thinking blocks (26k) are dropped. `message.model` is not stored (`:291` handles only `tool_use`/`text`).
  - `isMeta`, `isCompactSummary`, `isVisibleInTranscriptOnly` are not filtered, so compaction summaries become fake user prompts.
  - `system` subtypes `compact_boundary` and `turn_duration` are ignored. `cost-state` records (totalCostUSD, modelUsage) are ignored.
  - `attachment` (144k) and `queue-operation` are noise, correctly ignored.
  - `<sid>/tool-results/*` sidecars hold truncated tool output (66 locally).
  - `agentId` links a sidechain to its Task tool call.
  - Forks/resumes (`continued-in`) are not modelled.

**OpenCode: extractor reads `session`/`message`/`part`**
- No data locally (empty `storage/`), so I cannot verify the extractor against real files. agentsview shows OpenCode v2 adds `session_v2` and `session_message(session_id, id, type, data[, seq])`, with a `kv` migration marker, and says Kilo shares the format. `opencode.py` lines 43/654/722 do not query these tables. Treat as drift risk.

## 3. Field-level schemas (no content)

### Pi (version 3, tree)
Verified on 46 files, 18,782 records; no branches locally.

- File: `<ISO ts with dashes>_<uuid7>.jsonl`. The directory is `--<cwd with / replaced by ->--`.
- Line 1 is the header: `{type:'session', version:3, id, timestamp(ISO), cwd[, parentSession]}`. It has no `id`/`parentId` and is not part of the tree.
- Every other entry has `{type, id(8 hex), parentId(string|null), timestamp(ISO)}`.
- Entry `type` values:
  - `message` `{message:{role, ..., timestamp(epoch ms)}}`. Roles: `user` (`content` string or blocks), `assistant`, `toolResult`, `system` (`content:'', sections{preamble,tools,rules,docs,project_context,skills,cwd,mcp_servers}, toolsAdded[], toolsRemoved[]`), `bashExecution` `{command,output,exitCode,cancelled,truncated,excludeFromContext}` (documented, not seen locally).
  - `model_change` `{provider, modelId}`.
  - `thinking_level_change` `{thinkingLevel}`.
  - `compaction` `{summary, firstKeptEntryId, tokensBefore, details{readFiles,modifiedFiles}, usage, fromHook, systemMessage}`.
  - `branch_summary` `{fromId, summary, usage?, details?}` (documented; NV locally).
  - `context_edit` `{targetId, replacement:null|content}`. Verified 54 locally, all `replacement:null`, meaning drop the target from model context. The target stays in raw history.
  - `custom_message` `{customType, content, display, details?}`. Locally: `subagent-notify`, `stack-harness` and similar. It enters LLM context.
  - `custom` `{customType, data}`. No LLM context.
  - `label` `{targetId, label}` (documented).
  - `session_info` `{name}`.
  - `usage` `{kind, provider, model, usage}` (documented).
- Assistant message: `{role, content:[text|thinking|toolCall], api, provider, model, responseModel?, responseId?, thinkingLevel?, usage, stopReason('stop'|'length'|'toolUse'|'error'|'aborted'|'deferred'), rawStopReason?, errorMessage?, durationMs?, timestamp}`.
  - Blocks: `{type:'text',text}`, `{type:'thinking',thinking,thinkingSignature,redacted?}`, `{type:'toolCall',id,name,arguments,thoughtSignature?,namespace?}`.
  - `usage`: `{input, output, cacheRead, cacheWrite, cacheWrite1h?, reasoning?, totalTokens, cost{input,output,cacheRead,cacheWrite,total}}`. `reasoning` is already included in `output`.
- `toolResult`: `{toolCallId, toolName, content:[text|image{data,mimeType}], isError, details?, durationMs?, usage?, nestedCalls?, timestamp}`.
- Identity: header `id` is the session id. The active branch is the path from the leaf (the last entry unless `$rewind`) to the root; filter by `parentId`, not file order.
- Siblings: the same wire format is used by OMP (`~/.omp/agent/sessions`), Prime (`~/.prime/agent/sessions`) and OMO. `~/.pi/agent/sessions/*/subagent-artifacts/*.md` are not sessions.

### Command Code
- `projects/<cwd with / replaced by ->/`: 68 project dirs, 605 `.jsonl` files, 600 `.meta.json`, 600 `.checkpoints.jsonl`.
- Only 5 transcripts exist (4 non-empty), while all 600 `meta.json` have `{entrypoint:'print'}`. Print-mode runs keep metadata and checkpoints but no transcript.
- Transcript, locally the Pi-style v3 header: `{type:'session',version:3,id,timestamp,cwd}` then `{type:'message',id,parentId,timestamp,message:{role,content:[blocks],meta:{source:'user'|'model'|'tool',createdAt,messageId}},model?,usage?}`.
- Blocks use Anthropic shape: `text`, `thinking{signature}`, `tool_use{id,name,input}`, `tool_result{tool_use_id,content}` (role `user`, `meta.source:'tool'`).
- `usage` (assistant only): `{inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, costUsd}`; `model` string (`deepseek/deepseek-v4-flash`).
- `<uuid>.checkpoints.jsonl`: one record per turn `{id, messageId, turnNumber, createdAt, prompt, messageCount, files:[{hash,path}]}`. `prompt` makes the user prompts recoverable even when no transcript exists. The `file-history/<uuid>/<sha256>` blobs hold file snapshots.
- `history.jsonl`: `{p:str, t:int}`, a global prompt history with no session id (NV semantics).
- A flat variant, from agentsview tests: `{id, timestamp, sessionId, role, content[], gitBranch, cwd?}` with `.meta.json {title, userRenamed, projectPath, cwd}`. It is not seen locally. Accept both: dispatch on `type=='message'` versus top-level `role`.
- Redact `~/.commandcode/auth.json`; never read it.

### Hermes (`state.db`)
- Real data lives in `~/.hermes/profiles/<name>/state.db`. The root `state.db` has schema but 0 rows. Counts: 8 sessions, 176 messages. `schema_version` is 30 (verified; the version is a single-row table).
- `sessions`: `id, source (cli|cron|oneshot|telegram|...), user_id, session_key, chat_id, chat_type, thread_id, display_name, origin_json, model, model_config, system_prompt, parent_session_id, started_at(REAL epoch s), ended_at, end_reason, message_count, tool_call_count, api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, cwd, git_branch, git_repo_root, billing_provider, estimated_cost_usd, actual_cost_usd, cost_status, title, title_source, profile_name, rewind_count, archived, pinned, hidden, last_activity_at, tool_names`.
- `messages`: `id (autoinc, order), session_id, role (user|assistant|tool|session_meta), content, tool_call_id, tool_calls (JSON [{id,call_id,type,function{name,arguments},response_item_id}]), tool_name, timestamp(REAL), token_count, finish_reason, reasoning, reasoning_content, reasoning_details, codex_reasoning_items, codex_message_items, active, compacted, _compressed_summary, display_kind (steer|internal_notification|NULL), display_metadata, api_content, platform_message_id`.
- Other tables: `session_model_usage(session_id, model, ... tokens, cost)` for per-model usage, `system_prompts(hash, prompt)`, `async_delegations`, FTS tables (ignore).
- Subagents/compression are linked by `parent_session_id`; skip rows where `messages.active=0`. `kanban.db(tasks, task_runs, task_events, task_comments)` is task state, not chat; `shared-state.db` is room sync. Legacy JSONL in `~/.hermes/sessions/` is empty here.
- Open the database with `mode=ro` and copy WAL files together; the WAL was 0 bytes in this inspection (`immutable=1` skips the WAL, so a live copy is stale).

## 4. Build order (first 8)

1. **Gemini fix.** It is broken today (0 sessions) and the fix is small. Include `$set`/`$patch`/`$rewindTo` replay, `.project_root` resolution and subagent dirs.
2. **Pi.** This is the most-used harness on this machine (239 files, 46 sessions with transcripts). The schema is public and stable (v3). It gives a reusable tree-JSONL base.
3. **Codex enrichment.** Add `custom_tool_call`/outputs, `token_count`, model, subagent parent, `origin=exec`, `.zst`, `archived_sessions`. It is the largest volume and currently loses tool output.
4. **Antigravity via `transcript.jsonl` plus `conversation_summaries.db`.** Drop `.pb`. Keep the `.db` protobuf walk out of v1.
5. **Command Code.** Close to free once Pi exists. Cover the checkpoints-only print runs so prompts are still captured.
6. **Hermes `state.db`.** First SQLite-container source; it forces the per-row fingerprint design (see 5).
7. **Claude enrichment.** Add thinking, model, `ai-title`, compaction and meta filtering, `cost-state`. The risk is low and the schema is stable.
8. **Cursor.** Add agent-transcripts JSONL first (cheap), then per-bubble timestamps, model, thinking, tools, and `composerHeaders`. Leave `agentKv` for later (opaque).

Next wave, in order: Cline/Roo/Kilo VS Code tasks (shared Cline schema), OpenCode v2 tables, Zed, Goose/Crush (SQLite), Kimi/Qwen/Factory (Claude-like JSONL), Kiro, Continue. Do JetBrains, Windsurf Cascade and Augment only on demand, since they are encrypted or undocumented.

## 5. Recommended base-contract changes

- **Fingerprint-based source discovery.** `_source_unchanged` is per-file mtime (`base.py:47`), which does not fit a shared SQLite store. Add a `discover_sources() -> Iterable[SourceRef(path, virtual_id, fingerprint)]` split from `parse(ref)`. Fingerprints: Hermes `last_activity_at`, Zed `updated_at`, Cursor `lastUpdatedAt`, Codex `thread_history` byte offset.
- **Session relations and origin.** Add `parent_session_id`, `relation` (subagent|fork|continuation|compaction) and `origin` (interactive|exec|cron|subagent). Claude currently smuggles this through `thread_id` and a title prefix. Pi/Codex/Gemini/Antigravity/Hermes all need it.
- **Tree-aware messages.** Add `parent_id` and `active` to `UnifiedMessage`. Pi/Gemini/Claude/Hermes have branches, rewinds, `context_edit` and `active=0`. Without this a session flattens to a wrong linear chat.
- **Typed content kinds.** Replace string-only `content` plus `Role` with a `kind` (text, thinking, tool_call, tool_result, injected_context, compaction_summary). `user_prompt_count` currently counts injected context as prompts (Codex), and compaction summaries (Claude).
- **Noise policy per origin.** The global `MIN_USER_PROMPTS=3` silently drops about 90% of Codex `exec` runs and all single-shot runs. Tag `origin=exec` and keep or hide in the UI, rather than dropping at extraction.
- **Unknown-type telemetry.** Count unknown record types and files matched versus parsed in `skip_counts`. A stale glob produced silent zeros for Gemini. Have `is_available()` warn when sources exist but yield 0.
- **Redaction hook** at the extractor-to-store boundary. Needed for `encrypted_content`, `thinkingSignature`, Cursor encryption keys, and system prompts or `tools` sections in Pi and Hermes. Exclude secrets and auth files by default.
- **Session-level model, usage and cost fields.** Pi, Command Code and Hermes provide `cost`/`costUsd`. Normalize token keys (`input`, `inputTokens`, `input_tokens`).
- **Incremental append reads** (byte offset or ordinal) for append-only JSONL. Codex rollouts are large and reparsed whole.
- **Format-version field** per session (Pi `version`, Cursor `_v`, Hermes `schema_version`, Codex `cli_version`) for drift detection and fixture-based contract tests.

**Not checked:** the macOS/Windows paths (taken from the open-source parsers' docs, not from the machines); JetBrains/Windsurf-Cascade/Augment/Trae-encrypted internals; Gemini assistant-side records on real data; OpenCode v2 on this machine.
