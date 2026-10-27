# Changelog

All notable changes to this project are documented here.
The version is the single source of truth in `lore/__init__.py`.

## [Unreleased]

### Added
- Tombstones and pruned ids live in the v2 store (#48): deleting a session records it in `deleted_sessions` and `lore prune` records its ids in the new `ignored_sessions` table (migration 27). Both are append-only and idempotent, so a reindex keeps the state and a store built on another machine knows what was deleted or hidden. `deleted_sessions.json` and `ignored.json` stay as a compatibility mirror; readers union them with the store, and recording an id never creates a store that did not exist.
- The v2 store stamps `store_meta.content_hash` — a sha256 over the session fields the served payload is built from — beside `generated_at`, and the index writer asserts it right after committing, so a store that did not persist what was written fails the build instead of being served. A stale, unreadable or hash-mismatched store is never skipped silently: the gate and the service layer log the reason, and `assert_store_fresh` raises `StaleStoreError` with every reason it found.
- `tests/test_v2_json_equivalence.py` builds a realistic archive (several tools, multi-message sessions, a subagent thread, tags, a memory, a tombstone, a pruned id) and asserts the v2 and legacy JSON payloads agree on session ids, per-session content fields and counts, before and after a reindex.

D
- MCP over streamable HTTP (#29): `lore-mcp --http [--host] [--port] [--path] [--token-file] [--timeout]` serves the same `create_server()` tool set as stdio (which stays the default and is unchanged) from `lore/interfaces/mcp_http.py`. The endpoint takes one JSON-RPC message per `POST` and answers `application/json`; the server is stateless, acknowledges notifications with `202` and refuses `GET` with `405`. Loopback is the default bind; a non-loopback host refuses to start without a bearer token from `LORE_MCP_HTTP_TOKEN` or `--token-file`, the `Host`/`Origin` checks are the `lore-web` allowlist (`400`/`403`), bodies are capped at 1 MiB (`413`), concurrent requests at 4 (`503`), and every request has a deadline (`--timeout`) after which the tool call is cancelled with JSON-RPC error `-32000`. The token is compared in constant time and never logged.
- Portable archive bundles with source provenance (#163): `lore bundle export <path>` writes a zip holding a SQLite-consistent v2 store snapshot (raw `source_revisions`/`source_artifacts` evidence included) plus a manifest with the format version, the originating namespace and machine scope, one sha256 per member, the row count of every archived table and an evidence fingerprint over every captured `(source_identity, content_sha256)` pair. `lore bundle import <path> [--merge|--replace] [--dry-run]` verifies all of it before the live store is opened, then merges: revisions dedupe by `(source_identity, content_sha256)` and divergent bytes become an extra revision, an identical session changes nothing while a divergent copy of the same provider id lands under a qualified id with `copy_of_session_id`, imported rows keep their original `source_namespace` and the report prints how many rows came from which origin, and tags, knowledge, summaries, memory (with tags, ratings and evidence links), tombstones and per-source health are merged without overwriting local annotation. `--dry-run` merges into a temporary copy and reports the real counts; every completed import is logged append-only in the new `bundle_imports` table (migration 26).
- Native-CLI analysis adapters behind one explicit provider boundary (#180-#184): `lore/llm/adapters/` defines a persisted request descriptor (provider identity, model, auth mode, timeout, cancellation token, execution policy), detects the installed `claude`/`codex`/`gemini` executables (`--version` plus a `--help` capability probe), invokes the unmodified CLI with the prompt on standard input, parses its documented structured output and enforces the timeout, the prompt bound and cancellation. A missing CLI reports a reason and raises `AdapterUnavailable`; it never falls back to an API provider. `antigravity-cli` ships as an availability-gated stub with its open contract questions documented.

### Changed
- Provider selection goes through that boundary: `get_provider()` builds every provider from a `ProviderRequest` and one selection table (`lore/llm/adapters/selection.py`), refusing an unknown name, a mismatched auth mode, an API key (or `base_url`) for a native-CLI provider with typed errors instead of conventions.
- The `gemini` API provider no longer reads `~/.gemini/oauth_creds.json`: the CLI's login belongs to the `gemini-cli` adapter, so API credentials and CLI credentials stay separate.

### Added
- `accessibility`: accessible names for the project filter controls (tool, tag, start and end date), the noise-rules preview textarea and the resume copy button; the labels are checked by `tests/test_a11y_form_labels.py` (cluster of PRs #200, #201, #205, #208, #210).

### Fixed
- A bundle import or a restore leaves a store the reader still trusts: `lore bundle import --merge` re-stamps `store_meta.content_hash` and `generated_at` inside the merge transaction (without it the freshness gate rejected the whole store once a bundle was merged, hiding the imported sessions), `--replace` and `lore restore` stamp the installed snapshot instead of keeping the stamp of the moment it was taken (otherwise a newer local `index.json` made the reader serve the archive the import or restore had just replaced).
- Date filters agree across the web UI, the CLI and MCP: a date-only end value covers the whole selected day (sessions on the chosen final day were dropped), timestamps carrying an offset are converted to the local wall clock instead of raising a naive/aware comparison error, and date-only values are anchored in the host's timezone (or an explicit one) through one shared helper.

### Changed
- Antigravity availability means readable data now: a `brain/` directory alone is not "available", and `lore check --deep` still inspects such a tree and reports its coverage — sources matched, sessions parsed, skip reasons and a `Coverage:` line with Antigravity's `unsupported_artifacts`, `task_artifacts_parsed`/`transcript(s)_parsed` split instead of a bare "not found".

### Changed
- A chunked session summary reports its coverage instead of a bare `truncated` flag: a `coverage` object carries `chunks_total`/`chunks_used`, `complete`, the omitted chunk ranges, the session's `seq_range`, how many user turns fell into the gaps, and the revision of the transcript that was read.

### Changed
- Model-provider cache keys cover the whole request identity — provider, effective model, task, prompt version, source revision/coverage scope, sampling parameters and prompt fingerprints — instead of `system|prompt` plus a model name. Entries record that identity under `request`, writes are atomic and owner-only, and the four providers no longer collide on the same text.

### Added
- Generated memories can carry a claim (observation, hypothesis, rule) with a confidence, the source span (`session_id`, `message_id`, `revision`) and the model request that produced it, stored as a candidate by default. `claim_state` distinguishes observation, hypothesis, confirmed, rejected and superseded; review changes only the status, so the evidence survives, and `lore memory list --json` is the audit export (claim, evidence, sources).

### Added
- Memory candidate review: `lore memory review` lists the queue and `lore memory review accept|reject <id> [...]` decides it (rejection keeps the row and is reversible), and the web UI gained a Candidates tab at `/memory?status=candidate` with per-card Accept/Reject posting to `/memory/<id>/review`. Candidates stay out of recall and out of the default list until they are accepted.

### Added
- `docs/READING-CONTRACT.md`: the rules a reader can rely on (roles and execution states, theme-driven typography, keyboard and focus, live status, document structure), each tied to its implementation, plus `tests/test_a11y_reading_contract.py`, which checks the machine-decidable half over every page — language, one `h1` and no heading jumps, skip link with target, unique ids, an accessible name on every link, button and form control, live regions for async feedback, theme variables instead of hardcoded text colours, no fixed heights on transcript content, and a focus ring for every interactive component.

### Added
- `lore memory repair-index` (dry run by default, `--apply` writes, `--json` for machines) compares the memory table with the shared FTS index, reports memories whose search row is missing and index rows whose memory is gone, and repairs both in one transaction.

### Fixed
- The transcript keeps roles and execution states apart: every turn carries its original role in `data-role` and gets a role-specific badge (assistant, Tool result, System, Info) instead of rendering every non-user message as the assistant, and tool state uses one vocabulary (`pending`, `running`, `ok`, `failed`, `cancelled`, `unmatched`, `unknown`) with a distinct chip per state that also survives folding a burst.

### Fixed
- Display normalisation is view-only: the canonical message text is preserved on `original_content` and every render re-derives from it, so repeated normalisation is idempotent and a page load can no longer fold an already folded message. The file-operation and unset-environment-variable summaries keep the lines they fold in a collapsed `details` block, so no line is lost from the view or the export.

### Added
- The standalone HTML export renders full Markdown (headings, lists, tables, blockquotes, inline markup, links) with the web UI's pipeline and sanitises it with one shared allowlist, instead of understanding only paragraphs and code fences; the page stays self-contained and offline. The Markdown renderer and the HTML allowlist now live in `lore/utils/markdown_render.py` and `lore/utils/html_sanitize.py` so the UI and the export cannot drift apart.

### Added
- Markdown and HTML exports render every tool call from one canonical envelope: name, call id, pretty-printed JSON arguments (raw text plus a note when the payload is not JSON), result or error, status, duration, and truncation or dropped-event labels. Compact `tool_events` are merged in, so a failed call without recorded arguments and an unmatched tool result stay visible; exports no longer print "Unknown Tool" blocks and no longer drop results.
- Exports label derived metadata: `title_source` in the front matter and a "not transcript content" note for the title and summary, and an explicit line when a session has no messages.

### Added
- Search hits carry `evidence`: the matching message (`message_seq`, `role`), a marked excerpt with `excerpt_truncated` and its permalink; `/api/v1/search` reports a real `total` with `page`/`per_page`/`next_page` (`limit` still accepted) and `total_is_partial` for scoped searches, and `lore search` gained `--page` plus the total and an evidence line per hit.
- Message permalinks: `/session/<id>/m/<seq>` and `/api/v1/sessions/<id>/messages/<seq>` resolve one message with a bounded neighbour window (21 messages) read straight from the v2 rows, with 410 for a message the store does not know.

### Changed
- `/api/v1/sessions/<id>/messages` is a bounded window now (`page`, `per_page`, `at=<seq>`) and reports the session's real `total`, instead of serialising the whole live session and truncating it.

### Changed
- Search filters constrain the candidates before the limit: `/api/search`, `/api/v1/search`, the MCP tools `search_history` and `list_sessions` and the v2 FTS and semantic paths apply tool, project, saved-tag and inclusive date filters inside the query, so a query whose best unfiltered hits live in another project, tool or date range still fills its limit. An unparsable date filter is rejected with an explicit error instead of widening the result set.

### Added
- `lore list --tag` and `lore search --tag`, plus a `tag` parameter on the MCP tools `search_history` and `list_sessions`: the tag filter now matches the user's saved tags/bookmarks (`session_tags`) as well as the LLM-generated index keywords, and the sessions dropdown lists both.

### Changed
- Markdown rendering reuses one parser per thread (`thread_local_markdown_parser`) instead of building a parser for every conversion: measured 1.37x faster on 1 KB and 1.27x on 10 KB messages, with identical output (cluster of PRs #202-#215).

### Added
- `lore backup <path>`, `lore backup verify <path>` and `lore restore <path> [--force]` (#161): a bundle is a zip with a format-version marker, a SQLite-consistent snapshot taken with the online-backup API (never a copy of a live WAL database) and a `manifest.json` carrying the schema and applied migration versions, the Lore version, the creation time, one sha256 per member and the row count of every archived table — sessions, messages, the shared FTS index, memory, tags and the other annotations. Verification checks the structure, every checksum, `PRAGMA integrity_check`, the row counts and schema compatibility without restoring; a restore verifies first, refuses an existing store without `--force`, and swaps the snapshot in from a sibling temp file so a failed restore leaves the live store untouched. Bundles carry no credentials: nothing outside the store is read, and provider credentials live in the environment.

## [2.4.0] - 2026-10-26

### Added
- Semantic search over sessions via sqlite-vec (`semantic` extra), fused with keyword hits by reciprocal rank fusion behind a calibrated distance cutoff.
- Exact-phrase queries, result snippets, and source tags on session and memory hits.
- `lore summarize` writes AI session summaries for archived sessions, with `--dry-run` estimates and citation-checked output.
- `lore improve` backfills embeddings, extracts knowledge, and proposes memory candidates.
- `lore digest`, `lore threads`, and `lore rules` print an activity digest, open threads, and derived rules.
- `lore run --no-pty` disables PTY capture; `lore check --deep` runs every extractor without writing and reports discovery counters.
- OpenRouter provider, opt-in cloud; non-free models are refused unless `LORE_OPENROUTER_ALLOW_PAID=1`.
- jev provider over text-only delegate lanes, opt-in cloud.
- MCP `get_session_summary` (read-only) and `summarize_session` (local only, enabled by `LORE_MCP_ALLOW_SUMMARIZE=1`).
- Session summaries are stored (migration 18) and removed with their session.
- Origin, relations, message kinds, and tool events are stored (migration 17); user-editable session tags and bookmarks.
- Session readers for Cline, Roo Code, Kilo Code, Goose, Crush, Kimi CLI, Qwen Code, Zed, and Factory Droid.
- Session readers for Cursor agent transcripts, Hermes, Pi (version 3 tree), Command Code (including checkpoint-only runs), OpenCode v2, Antigravity plaintext transcripts, and Codex archived rollouts.
- `lore memory dedupe [--apply]` lists exact confirmed duplicates (same title, body, kind, and scope); dry run by default; `--apply` runs in one transaction and writes a JSON undo log.
- `lore memory dedupe --restore FILE` reverses the rejections recorded in a dedupe undo log.
- `lore memory context` renders a token-budgeted memory block for a project path.
- `lore memory show` prints one memory in full: status, kind, scope, supersession links, and body.
- `lore memory import-claude` seeds shared memory from Claude Code auto-memory files; dry run by default, `--apply` writes, idempotent by file path and content hash.
- Cross-platform install smoke test (`scripts/smoke_install.py`): installs a wheel into a clean venv with an isolated `HOME` and checks the CLI commands, `lore-web`, `lore-mcp` and archive behaviour.
- Docker install matrix (`scripts/install-matrix.sh`): the smoke test across Python 3.11-3.14 slim images, 3.12 with `semantic`, Alpine (musl), Debian 12, Ubuntu 24.04, Fedora via distro `pipx` and emulated linux/arm64 (10 of 10 targets passed).
- A GitHub workflow (`.github/workflows/install-matrix.yml`) that builds the wheel and runs the smoke test on Linux, macOS and Windows on every push to `main` and pull request.
- Memory candidate status and atomic supersession, with similar-memory hints; recall is hybrid by default with a 0.6 relevance floor and source tags.
- Web UI: peek preview pane, keyboard shortcuts with a help dialog, and in-session find across all pages with highlight and next/prev.

### Changed
- **BREAKING:** `TRUSTED_PROXY` is boolean-valued: only `1`, `true`, `yes` or `on` (case-insensitive) enable proxy trust. The value `0` used to enable trust (any non-empty value did); it now disables it, as does `false`, `no`, `off` or any other value.
- **BREAKING:** `lore analyze` and `lore format` need `--allow-cloud` or `LORE_ALLOW_CLOUD=1` by default, because their default provider, gemini, is a cloud provider. Without it they exit with code 2 and send nothing.
- **BREAKING:** `lore knowledge` has no default provider; `--provider` is required, and a bare `lore knowledge` exits with code 2.
- **BREAKING:** the `watch` extra is removed; `lore watch` polls for new sessions and needs no extra package.
- **BREAKING:** the Python package `ai_history` is renamed to `lore`; CLI modules are `lore_cli` and `lore_session_cli`, and environment variables use the `LORE_*` prefix.
- `lore memory dedupe` respects scope: memories are never merged across kinds or scopes, and the kept copy is the one with the highest feedback weight, then the newest update.
- Sessions need at least 3 user prompts to be imported by default (`LORE_MIN_USER_PROMPTS`; `LORE_IMPORT_PROFILE` selects the profile); Claude Code subagent transcripts are included.
- `lore export` and `lore sync` print skipped-session counts and their reasons to stderr after the run.
- `lore search` says "Found 1 result" for a single hit and prints scores to three decimals.
- Extractors keep more detail: Claude thinking, model, native titles and compaction; Codex tool results, tokens and context; Cursor bubble times, model, thinking and tools.
- Sync skips unchanged sources before opening them and re-extracts sessions when the parser version rises.
- Automated sessions (exec, cron, subagent) are hidden by default; `include_automated` shows them.
- Web UI uses light and dark themes only, and a precompiled stylesheet replaces the Tailwind JIT runtime.
- Bounded reload memory: `IndexBuilder` streams sessions, and OpenCode extraction is two-pass.
- Session list and conversation UI: day-grouped list, folded tool-call groups, AI summary card, bulk select with delete and tag, bottom sheets for outline and filters.
- `lore list` reads the live tool directories, not the archive index. After a source file is deleted the session disappears from `lore list` while it stays in `lore search`, `lore stats` and the web UI. `--format json` prints only the JSON array to stdout; totals and paging hints go to stderr.
- Packaging: the license is declared as SPDX `MIT` (needs setuptools>=77, PEP 639); the sdist excludes maintainer files (handover notes, session management logs, maintenance-night documents, triage and research docs, and scratch directories); the `[dev]` extra includes pre-commit and bandit.

### Fixed
- `lore` starts on Windows: the `pty` module is only loaded for `lore run`.
- `lore` no longer crashes on consoles that cannot encode the status symbols: stdout and stderr replace unencodable characters (for example on a cp1252 console).
- `lore-web` starts in under a second on macOS instead of about 35 s (no reverse DNS lookup while binding; the first answer now takes 0.4-0.8 s).
- `lore export` keeps index entries whose source file was deleted, and now also keeps entries whose export is gone: nothing is dropped automatically, only `lore prune` removes entries.
- `lore prune` honours `ignored.json` in the v2 store views, so pruned sessions stay hidden.
- MCP `switch_to_tool` and the web "continue" command call `lore-session` instead of the removed `ai-session` command.
- `lore search` and `lore prune` tell you to run `lore export --all` when no index exists.
- `lore memory context` ignores overly broad ancestor scopes (`/`, the home directory, and similar); descendant scopes are unchanged.
- Search accepts apostrophes in queries and handles whitespace at the edges correctly.
- Knowledge extraction reports why it failed and commits its writes; a stale knowledge base is cleared when every session is deleted.
- Sync no longer scans the FTS table once per written session.
- Session titles are generated through the selected provider.
- Gemini CLI JSONL sessions use the current format; Antigravity and Copilot drops are reported instead of silently counted as zero.
- OpenCode no longer truncates tool output at 50k; Codex and OpenCode keep string tool inputs.
- The cost dashboard persists per-session token totals and counts sessions without token data.
- Accessibility: visible focus outlines, labelled controls, keyboard and screen-reader affordances, and muted-text contrast in both themes.
- Archive preservation never drops an entry on a filesystem error (permission denied, unmounted archive), and it also runs for filtered exports.
- Memory dedupe compares `scope_project` with the same normalisation recall uses, so `/work/repo/` and `/work/repo` form one scope.
- `lore search` excludes pruned sessions before it applies the result limit.
- Changes to `ignored.json` invalidate the v2 index cache, so prune and un-prune take effect at once.
- Install hints name `pipx install git+https://github.com/DnaMes/lore` and `pipx inject`, not the unrelated PyPI package `lore`.
- `docker-compose.yml` reads `LORE_PORT` for the published host port (default 5000), so it can move when 5000 is taken.

### Security

- `lore run` creates `runs/` as 0700 and its raw transcript logs and metadata as 0600 from the first byte.
- The Gemini session handoff and `gemini-cli` titles no longer put session text into the process arguments: the handoff passes a path to the private context file, titles use standard input.

### Removed
- Internal planning notes, research dumps, homelab leftovers and one-off maintainer scripts left the tree (`docs/archive`, `docs/research`, `docs/superpowers`, `SESSION-MANAGEMENT.md`, `tools/sync.py` and others); they stay reachable in Git history under the tag `archive/pre-cleanup-2026-10-26`. The recall and cutoff calibration scripts and their data moved to `tools/calibration/`.
- **Warp extractor:** Lore no longer imports Warp chat history; sessions already ingested keep loading as a legacy tool.
- Legacy `ai-history` compatibility: the `AI_HISTORY_*` env prefix, the `~/.ai-history` auto-migration, and the `X-AI-History-Revision` header are gone. Use `LORE_*` env names, `~/.lore`, and `X-Lore-Revision`.
- Z.AI provider.

### Security
- Requests whose `Host` header is not loopback, the bind host, a public origin, or listed in `LORE_ALLOWED_HOSTS` get a 400.
- `Host` and `Origin` parsing accepts only ASCII digit ports in 1 to 65535; non-ASCII digits and out-of-range values (for example `0`, `65536` or a fullwidth `１２`) are rejected as not allowed (400 for Host, 403 for Origin) instead of returning a 500.
- The cloud opt-in is enforced in the library, not only the CLI: `get_provider()` (`lore/llm/factory.py`), `run_improve` (`lore/services/improve.py`) and `TitleGenerator` (`lore/titles/generator.py`) raise `CloudOptInRequired` without `--allow-cloud` or `LORE_ALLOW_CLOUD=1`; `ollama` counts as local only on a loopback host. The title prompt's transcript excerpt is redacted with the same redactor as the other send paths.
- Mutating requests (POST, PUT, PATCH, DELETE) with an `Origin` header must match their own scheme and host or a configured public origin; anything else gets a 403.
- `lore prune` deletes only regular `.md` export files under the archive root; symlinks and control or store files (index, ignored, deleted-session and title-cache names, memories, rules, opencode state, `-wal`/`-shm`) are refused with a stderr warning. A damaged `ignored.json` (bad UTF-8, invalid JSON or the wrong shape) warns once per process, and pruned sessions may be visible again until the file is fixed.
- Dedupe undo logs record the store they came from, and `--restore` refuses a log written for a different store.
- `analyze`, `knowledge`, `format`, `summarize`, `improve` and `generate-titles` require `--allow-cloud` or `LORE_ALLOW_CLOUD=1` for every non-local provider; a refused run exits with code 2 and sends nothing.
- `lore-session switch` / `continue` and the MCP `switch_to_tool` require the cloud opt-in for `gemini` and `codex`, redact the context before it is written or passed, and keep it in a private mode-0600 file under `~/.lore/handoff/` instead of the shared `/tmp/ai-session-context.md`.
- `lore-web` prints a warning at startup when it binds beyond loopback without `LORE_PUBLIC_ORIGINS` or `LORE_ALLOWED_HOSTS`.
- The web UI binds to `127.0.0.1` by default.
- Per-window rate limits for the web UI (`LORE_RATE_LIMIT_*`), and honoured public origins behind a proxy (`LORE_PUBLIC_ORIGINS`).
- Secret redaction for prompts and exports; `lore export --redact` produces shareable exports, and structured tool data is redacted by field name.
- Inline event handlers are replaced with CSP-safe delegation.
- Secret-named tool inputs from Cursor agent transcripts are never rendered.
- An off-host `OLLAMA_HOST` is treated as cloud, and transcripts stay out of the disk LLM cache.

## [2.3.0] - 2026-05-18
### Changed
- **Renamed the product from `ai-history` to `Lore`.** Lore is positioned as
  both a local-first archive of AI coding sessions and a shared cross-tool
  agent memory (accumulated knowledge + history).
- CLI binaries renamed: `ai-history` → `lore`, `ai-session` → `lore-session`,
  `ai-history-web` → `lore-web`, `ai-history-mcp` → `lore-mcp`.
- Repository moved to `github.com/DnaMes/lore`.
- Default data directory is now `~/.lore` (was `~/.ai-history`) — an existing
  `~/.ai-history` is auto-migrated in place on first run.
- The Python import package remains `lore` — the import name was
  deliberately not renamed.

### Added
- `memory_sources` provenance linking: a memory can be tied to the session
  it was derived from (`memory add --from-session`, MCP `source_session`,
  shown on the `/memory` page).

## [2.2.0] - 2026-05-18
### Added
- Scoped MCP search: `search_history` gains a `scope` parameter
  (user_only / assistant_only / tool_results / all).
- `ai-history export-html <session>` — standalone single-file HTML
  session export (inlined CSS, no CDN, no JS, all content escaped).
- Headless render smoke-test guarding against CSP/asset regressions.
- Release checklist documented in CONTRIBUTING.

### Changed
- New `lore/services/` layer holds shared index/extractor logic;
  the `mcp → web_data` layering inversion is removed.
- `mcp.create_server()` decomposed from ~550 lines into focused
  `mcp_tools/` modules.

### Fixed
- `supersede_memory` is now crash-safe (was non-atomic).
- `unpack_vector` no longer crashes semantic recall on a malformed BLOB.
- `export-html --output` no longer creates directories implicitly; the
  export is written owner-only (0600).
- Memory page search controls have accessible names (WCAG AA).

### Security
- HTML export hardening; memory render-safety guardrail (no stored XSS).

## [2.1.0] - 2026-05-18
### Added
- **v2 SQLite store** as the single source of truth (issue #44): sessions,
  messages and a unified FTS5 search index, with a staged migration runner.
  `load_index()` reads v2 with transparent JSON fallback.
- **Shared cross-tool agent memory** (#33): `memory_write` / `memory_recall`
  MCP tools and an `ai-history memory` CLI — facts, decisions and lessons any
  AI tool can record and recall.
- **Semantic memory search**: optional embedding backend (`ai-history[semantic]`)
  ranks recall by meaning; falls back to keyword search when absent.
- **`/memory` web page**: browse, search (keyword or semantic) and delete memory.
- **Aider extractor** (#51) and an `ai-history digest` activity-summary command.
- Vendored Tailwind + highlight.js — the web UI works fully offline (#19).
- Server-side pagination for `/sessions` (#17); mobile sidebar drawer.
- Package versioning: `--version` CLI flag, version shown in the web UI.

### Changed
- Search routes and MCP search now read the v2 store (#34), keeping search
  and the session list consistent.
- CSP: `script-src` is nonce-only; `style-src` uses `'unsafe-inline'` so the
  Tailwind runtime works.

### Fixed
- Concurrency: `busy_timeout` + idempotent migrations for the SQLite store.
- WCAG AA: contrast, keyboard-operable theme picker, modal focus traps.
- Security: owner-only data-file permissions, memory input caps, CSRF guards.

### Security
- 80%+ test coverage gate; `pip-audit` and a security workflow in CI.

## [0.1.0] - 2026-05-13
### Added
- Initial public release
- Support for Claude Code, Cursor, VSCode Copilot, Gemini CLI, Warp, Codex, OpenCode extractors
- Web UI for browsing and searching sessions
- MCP server for Claude Code integration
- Export to Markdown
- Incremental sync (skip unchanged sessions)
