# Changelog

All notable changes to this project are documented here.
The version is the single source of truth in `lore/__init__.py`.

## [Unreleased]

### Added
- `accessibility`: accessible names for the project filter controls (tool, tag, start and end date), the noise-rules preview textarea and the resume copy button; the labels are checked by `tests/test_a11y_form_labels.py` (cluster of PRs #200, #201, #205, #208, #210).

### Fixed
- Date filters agree across the web UI, the CLI and MCP: a date-only end value covers the whole selected day (sessions on the chosen final day were dropped), timestamps carrying an offset are converted to the local wall clock instead of raising a naive/aware comparison error, and date-only values are anchored in the host's timezone (or an explicit one) through one shared helper.

### Changed
- Search filters constrain the candidates before the limit: `/api/search`, `/api/v1/search`, the MCP tools `search_history` and `list_sessions` and the v2 FTS and semantic paths apply tool, project, saved-tag and inclusive date filters inside the query, so a query whose best unfiltered hits live in another project, tool or date range still fills its limit. An unparsable date filter is rejected with an explicit error instead of widening the result set.

### Added
- `lore list --tag` and `lore search --tag`, plus a `tag` parameter on the MCP tools `search_history` and `list_sessions`: the tag filter now matches the user's saved tags/bookmarks (`session_tags`) as well as the LLM-generated index keywords, and the sessions dropdown lists both.

### Changed
- Markdown rendering reuses one parser per thread (`thread_local_markdown_parser`) instead of building a parser for every conversion: measured 1.37x faster on 1 KB and 1.27x on 10 KB messages, with identical output (cluster of PRs #202-#215).

## [2.4.0] - 2026-10-26 (unreleased until tagged)

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
- `lore memory rate` records feedback ratings for confirmed memories, with a bounded derived weight.
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

### Removed
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
