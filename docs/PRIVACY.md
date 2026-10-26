# Privacy and data locations

This document describes how Lore handles session data, where local data is read and written, when data leaves the machine, and how sensitive information is protected.

## Principle

Lore operates as a local-first archive and manager for AI coding assistant sessions. It reads session histories from the directories where tools write them on the local filesystem and writes its own unified archive under `~/.lore` (customizable via the `--output-dir` option).

By default, Lore uploads nothing. Core features run entirely on the local machine without internet access:
- Indexing and archiving session transcripts
- Full-text search (SQLite FTS5)
- Local web interface (`lore-web`)
- Cross-tool memory store and vector retrieval
- Session export (Markdown, JSON, HTML)

## Read sources

Lore inspects local session files and SQLite databases created by supported AI assistant tools. All reads are strictly read-only.

For SQLite stores, Lore never directly connects to the active database in place. Instead, it creates a temporary scratch copy using `copy_sqlite_with_sidecars` (in `lore/utils/sqlite_copy.py`) or `safe_copy_db` (in `lore/utils/paths.py`). The copy includes any associated write-ahead log (`-wal`) and shared memory (`-shm`) sidecars. The scratch copy is then opened strictly in read-only mode using SQLite URI parameters (`?mode=ro`).

A code audit of `lore/extractors/` confirms this architecture:
- `lore/extractors/antigravity.py`: Opens scratch copy with `file:{copy_path}?mode=ro`
- `lore/extractors/hermes.py`: Opens scratch copy with `file:{copy_path}?mode=ro`
- `lore/extractors/cursor.py`: Opens scratch copy with `file:{work_db_path}?mode=ro`
- `lore/extractors/opencode.py`: Opens databases with `file:{db_path}?mode=ro`
- `lore/extractors/opencode_v2.py`: Opens scratch copy with `file:{copy_path}?mode=ro`
- `lore/extractors/sqlite_common.py`: Shared by Goose, Crush, and Zed extractors via `open_readonly_copy()`, connecting with `file:{copy_path}?mode=ro`
- `lore/extractors/codex.py`: Opens scratch copy with `file:{copy_path}?mode=ro`

JSON and JSONL sources are opened using standard Python read-only file streams. Lore never writes to, modifies, or deletes files inside any AI tool directory.

### Tool read locations

| Tool | Directory or file read |
| :--- | :--- |
| `claude-code` | `~/.claude/projects/*/*.jsonl`, `~/.claude/projects/*/*/subagents/*.jsonl` |
| `gemini-cli` | `~/.gemini/tmp/*/chats/session-*.jsonl`, `~/.gemini/tmp/*/chats/session-*.json`, `~/.gemini/tmp/*/chats/*/*.jsonl` |
| `codex` | `~/.codex/sessions/*/*/*/rollout-*.jsonl`, `~/.codex/archived_sessions/**/rollout-*.jsonl[.zst]`, scratch copy of `~/.codex/state_5.sqlite` |
| `cursor` | Scratch copy of `~/.config/Cursor/User/globalStorage/state.vscdb` (`cursorDiskKV` rows `composerData:*` and sidebar chat data); `~/.cursor/projects/*/agent-transcripts/**/*.jsonl` |
| `vscode-copilot` | `~/.config/Code/User/workspaceStorage/*/chatSessions/*.json`, `~/.config/Code/User/workspaceStorage/*/chatSessions/*.jsonl` |
| `copilot-cli` | `~/.copilot/session-state/*.jsonl` |
| `opencode` | `~/.local/share/opencode/storage/session/**/ses_*.json`, `~/.opencode/storage/session/**/ses_*.json`, read-only `~/.local/share/opencode/opencode.db` and `~/.opencode/opencode.db` |
| `antigravity` | `~/.gemini/antigravity{,-cli,-ide}/brain/*/.system_generated/logs/transcript_full.jsonl` (or `transcript.jsonl`); scratch copy of `conversation_summaries.db`; fallback `task.md` / `walkthrough.md` |
| `aider` | `~/**/.aider.chat.history.md` (per project root) |
| `pi` | `~/.pi/agent/sessions/--*--/*.jsonl`, `~/.omp/agent/sessions/--*--/*.jsonl`, `~/.prime/agent/sessions/--*--/*.jsonl` |
| `command-code` | `~/.commandcode/projects/<slug>/*.{jsonl,meta.json,checkpoints.jsonl}` |
| `hermes` | Scratch copies of `~/.hermes/profiles/*/state.db` and `~/.hermes/state.db` |
| `cline` | `~/.config/<Editor>/User/globalStorage/saoudrizwan.claude-dev/tasks/*/api_conversation_history.json`, `~/.cline/data/sessions/*.json` |
| `roo-code` | `~/.config/<Editor>/User/globalStorage/rooveterinaryinc.roo-cline/tasks/*/api_conversation_history.json` |
| `kilo-code` | `~/.config/<Editor>/User/globalStorage/kilocode.kilo-code/tasks/*/api_conversation_history.json` |
| `goose` | Scratch copy of `~/.local/share/goose/sessions/sessions.db`, `~/.config/goose/sessions/sessions.db`, `$XDG_DATA_HOME/goose/sessions/sessions.db`, `~/.local/share/Block/goose/data/sessions/sessions.db` |
| `crush` | `~/.local/share/crush/projects.json` -> `<project>/.crush/crush.db` (scratch copy) |
| `kimi` | `~/.kimi/kimi.json`, `~/.kimi/sessions/<md5>/<session_id>/context.jsonl` |
| `qwen` | `~/.qwen/projects/<slug>/chats/<session-id>.jsonl`, `~/.qwen/tmp/<project-id>/chats/<session-id>.jsonl` |
| `zed` | Scratch copy of `~/.local/share/zed/threads/threads.db`, `$XDG_DATA_HOME/zed/threads/threads.db` (zstd decompression requires optional `zstandard`) |
| `factory-droid` | `~/.factory/sessions/<project-slug>/<session-uuid>.jsonl` and `<session-uuid>.settings.json` |

## Data directory ~/.lore

All files created by Lore are stored under `~/.lore` unless overridden with `--output-dir`.

### Files and directories

- `index.json`: Legacy JSON index of all parsed sessions and aggregate statistics.
- `index.sqlite`: Legacy SQLite full-text search index (FTS5).
- `index_v2.sqlite`: Unified primary store (SQLite). Contains full session messages, metadata, FTS5 full-text indices, memory tables (`memories`), deletion tombstones (`deleted_sessions`), per-session knowledge (`session_knowledge`), and vector embeddings (`memory_embeddings`, `session_embeddings`).
- `deleted_sessions.json`: List of soft-deleted session IDs (mirrored in `deleted_sessions` table in `index_v2.sqlite`).
- `ignored.json`: Records pruned session identifiers to avoid re-indexing unwanted records.
- `projects/`: Default target for session markdown exports organized by project slug (`projects/<slug>/<session-id>.md`).
- `knowledge/`: Output directory for extracted knowledge entries (`knowledge/knowledge_base.json`).
- `stats/`: Output directory for generated usage statistics (`stats/stats.json`).
- `formatted/`: Output directory for formatted markdown session transcripts (`formatted/<session-id>.md`).
- `llm_cache/`: On-disk response cache for LLM API calls, keyed by prompt content.
- `title_cache/`: Cache for titles generated by LLM title generation.
- `cache/`: Internal extractor scratch cache used to store modification times and incremental synchronization markers.

### File permissions and multi-user isolation

Lore enforces strict owner-only permissions on POSIX systems via utility functions in `lore/utils/paths.py`:
- `secure_dir(path: Path)`: Creates directories with mode `0700` (`rwx------`), preventing other local users from listing or accessing session files.
- `restrict_file(path: Path)`: Sets file permissions to mode `0600` (`rw-------`), ensuring only the file owner can read or modify databases, json indices, and tombstones.

## Outbound network and provider data sharing

By default, Lore does not make outbound network connections. External network requests occur only when a user explicitly invokes an LLM command and selects a provider.

Every non-local provider (gemini, openrouter, jev, and ollama when `OLLAMA_HOST` resolves off-host) requires an explicit cloud opt-in: `--allow-cloud` or `LORE_ALLOW_CLOUD=1` (also `true` or `yes`). The gate is `require_cloud_opt_in` in `lore/services/summarize_cli.py`; it runs in `analyze`, `knowledge`, `format` and `summarize`. Without the opt-in those commands refuse, print an error naming both options, and exit with code 2 before anything is sent. `improve` and `generate-titles` do not run this gate (see the next section).

### Cloud behaviour by command

- `lore knowledge` has no default provider. Without `--provider` it exits with code 2 and sends nothing. A cloud provider also needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`.
- `lore analyze` and `lore format` default to `gemini`, a cloud provider. Without the opt-in they exit with code 2 and send nothing. `analyze --no-llm` sends nothing at all. `format` has no `--no-llm` flag; use `--provider ollama` to stay local.
- `lore summarize` defaults to `ollama` (local). A cloud provider, or an off-host Ollama endpoint, needs the opt-in. `--dry-run` exits before any provider is contacted.
- `lore improve` sends conversation text only to the provider named by `--provider` or `LORE_LLM_PROVIDER`, and only in the `extract_knowledge` stage. It has no `--allow-cloud` gate: a notice naming the provider is printed to stderr, and the run proceeds. Leave both unset to keep it local, and use `--dry-run` to send nothing.
- `lore generate-titles` uses the local provider by default. `--provider gemini-cli` is the cloud choice; selecting it is the opt-in and no `--allow-cloud` gate applies. Titles send a transcript excerpt per title.

| Provider | Used by | Data sent | Opt-in |
| :--- | :--- | :--- | :--- |
| `none` (default) | `archive`, `search`, `web UI`, `memory`, `export` | Nothing | Not applicable |
| `ollama` | `summarize`, `improve`, `generate-titles` | Transcript text sent to the configured Ollama HTTP endpoint | Local by default (loopback). If `OLLAMA_HOST` resolves off-host, it is treated as a remote provider and requires `--allow-cloud` or `LORE_ALLOW_CLOUD=1` (enforced via `provider_locality` in `lore/services/summarize_cli.py`). |
| `gemini` (Google, `google-generativeai`) | `analyze`, `format` (default provider for both); `knowledge` (no default, needs `--provider gemini`) | Conversation text sent to Google Generative AI API | Opt-in required. gemini is a cloud provider and needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1` like the others. Once opted in, `analyze`, `knowledge` and `format` send conversation text to Google. Without the opt-in the command refuses and exits with code 2; users who require zero cloud egress can pass `--no-llm` to `analyze` or select `--provider ollama`. |
| `gemini-cli` | `generate-titles` | Transcript excerpt sent via local `gemini` CLI executable | Explicit choice via `--provider gemini-cli`. |
| `openrouter` (`openrouter.ai`) | `summarize`, `analyze`, `knowledge`, `format`, `improve` | Session text sent to OpenRouter API | Opt-in via `--allow-cloud` or `LORE_ALLOW_CLOUD=1` in `summarize`, `analyze`, `knowledge` and `format`; not checked by `improve`. Non-free models are refused unless `LORE_OPENROUTER_ALLOW_PAID=1` is set in the environment. |
| `jev` | `summarize`, `analyze`, `knowledge`, `format`, `improve` | Session text sent to maintainer's internal delegate broker | Opt-in via `--allow-cloud` or `LORE_ALLOW_CLOUD=1` in `summarize`, `analyze`, `knowledge` and `format`; not checked by `improve`. |

## Network check and telemetry audit

A code check across all Python sources (`lore/**/*.py`) for network primitives and telemetry terms reveals:

```bash
$ grep -rn "urlopen\|requests\.\|httpx\|telemetry" lore --include=*.py | head -20
lore/extractors/base.py:150:        """Per-run source telemetry, reset at the start of every ``extract_sessions()``.
lore/extractors/gemini.py:134:        # Replay telemetry: malformed_lines, unknown_records, unmatched_ids.
lore/extractors/claude.py:85:    # 4: titles, thinking, model, compaction/meta kinds, known-record telemetry.
lore/interfaces/live_probe.py:10:from urllib.request import Request, urlopen
lore/interfaces/live_probe.py:91:            with urlopen(request, timeout=timeout_seconds) as response:
lore/interfaces/live_probe.py:119:            with urlopen(head_request, timeout=timeout_seconds) as response:
lore/interfaces/live_probe.py:161:        with urlopen(request, timeout=timeout_seconds) as response:
lore/interfaces/web.py:263:    Browsers always send Origin on cross-origin requests. curl/MCP clients
lore/llm/openrouter.py:18:from urllib.request import Request, urlopen
lore/llm/openrouter.py:219:                with urlopen(request, timeout=self.config.timeout) as response:
lore/services/discovery.py:1:"""Per-tool discovery telemetry: record it at sync time, read it back for ``lore check``.
lore/services/discovery.py:56:        logger.warning("Could not record discovery telemetry: %s", type(exc).__name__)
```

Analysis of matches:
1. `urlopen`: Used only in `lore/interfaces/live_probe.py` for healthcheck probing of configured local or target endpoints in diagnostic tooling, and in `lore/llm/openrouter.py` when sending API requests to OpenRouter.
2. `requests.` and `httpx`: No imports exist anywhere in the `lore` package.
3. `telemetry`: The word is used strictly to refer to in-memory counters and local diagnostic records (tracking malformed transcript lines, unknown message types, and per-tool scan counts saved to `last_discovery.json` for display in `lore check`).

Verification: Lore contains no telemetry collectors, usage metrics reporting, tracking beacons, crash uploaders, or phone-home routines.

## Semantic search model (fastembed)

Lore provides optional vector embeddings for semantic search over agent memory entries (`lore[semantic]` extra). This implementation is located in `lore/storage/embeddings.py`.

- Default model: `BAAI/bge-small-en-v1.5` (producing 384-dimensional float vectors).
- Network download: The model is initialized via `fastembed.TextEmbedding(model_name=DEFAULT_MODEL)`. On first use, `fastembed` downloads the model weights from the Hugging Face Hub (`https://huggingface.co/` repository `qdrant/bge-small-en-v1.5` / `BAAI/bge-small-en-v1.5`).
- Local cache: Downloaded model weights are stored by `fastembed` in a local cache directory, defaulting to `/tmp/fastembed_cache` (or configured via the `FASTEMBED_CACHE_PATH` environment variable).
- Offline fallback: If `fastembed` is not installed or unable to download the model, `embeddings_available()` returns `False` and Lore automatically falls back to SQLite FTS5 lexical search.

## Redaction and input validation

Lore includes automated redaction mechanisms to prevent secrets from being leaked in exports or sent to external LLMs.

### Export redaction

The export commands support secret redaction:
- `lore export --redact`: Redacts secret-shaped tokens in exported markdown (requires specifying a non-default `--output-dir` to prevent overwriting the canonical archive).
- `lore export-html --redact <session-id>`: Redacts secret-shaped patterns before generating HTML output.

### Redaction functions

Implemented in `lore/utils/redact.py`:
- `redact(text: str) -> RedactionResult`: Replaces detected API keys, bearer tokens, private keys (PEM blocks), and high-entropy secrets with redaction placeholders. Returns the redacted text and the replacement count.
- `redact_value(key: object, value: str) -> RedactionResult`: Context-aware redaction for key-value structures.
- `redact_many(texts: Iterable[str]) -> tuple[list[str], int]`: Batch redaction across multiple text blocks.

Prompt preparation in `lore/llm/base.py`:
- `prepare_prompt(text: str) -> tuple[str, int]`: Automatically runs `redact(text)` on user prompt inputs before passing text to any external LLM provider, stripping secret-shaped tokens prior to transmission.

### Input sanitization and validation

Security validation functions in `lore/utils/security.py` guard file operations and queries:
- `sanitize_path(user_path: str, base_dir: Path, allow_absolute: bool = False) -> Path`: Enforces directory boundaries and prevents directory traversal attacks.
- `sanitize_filename(filename: str, max_length: int = 255) -> str`: Normalizes filenames and strips unsafe characters.
- `validate_tool_executable(tool: str, executable_path: str) -> bool` and `get_safe_executable(tool: str) -> Optional[str]`: Restricts external CLI invocations to known paths.
- `validate_tool_name(tool: str) -> bool`: Validates tool identifier format.
- `validate_session_id(session_id: str, max_length: int = 256) -> bool`: Rejects malformed session keys.
- `validate_search_param(param: str, max_length: int = 256) -> bool` and `validate_search_query(query: str, max_length: int = 256) -> bool`: Guards search query inputs against injection.

## Claude Code transcript retention

Claude Code applies an automated deletion policy to transcripts in `~/.claude/projects/` after its retention period expires (default is 30 days).

Lore checks this configuration in `lore/utils/claude_retention.py` and reports it during `lore check`:
- Reads `cleanupPeriodDays` from `~/.claude/settings.json` or `~/.claude/settings.local.json`.
- If `cleanupPeriodDays` is unset or set to 30 days or less, `lore check` prints a notice warning that Claude Code deletes transcripts and shows how to increase retention:
  ```json
  {"cleanupPeriodDays": 365}
  ```
- Lore archives transcripts when it syncs. If transcripts are deleted by Claude before Lore has run a sync, Lore cannot recover unindexed files.

## Deleting ~/.lore and data isolation

Deleting the `~/.lore` directory (e.g. `rm -rf ~/.lore`) permanently purges:
- All indexed session records and message archives
- The SQLite primary store (`index_v2.sqlite`) and legacy indices
- All cross-tool memories and vector embeddings
- Extracted knowledge bases and cached LLM responses

Deleting `~/.lore` has no effect on original tool session files. Lore never writes to or removes data from source directories (`~/.claude`, `~/.codex`, `~/.gemini`, `~/.config/Cursor`, etc.).

## Related documentation

- [Configuration Guide](./CONFIGURATION.md)
- [Agent Memory System](./MEMORY.md)
- [Security Policy](../SECURITY.md)
