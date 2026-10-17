# Changelog

All notable changes to this project are documented here.
The version is the single source of truth in `lore/__init__.py`.

## [Unreleased]
### Added
- `lore memory context` builds a token-budgeted prompt block from shared memory for a project path.
- `lore memory show` prints one memory entry by id (metadata and body).
- `lore memory import-claude` seeds shared memory from Claude Code auto-memory files
  (`~/.claude/projects/*/memory/*.md`). Dry run by default; `--apply` writes. Idempotent by
  file path and content hash, edited files supersede their previous import, and text is
  redacted before writing.
### Removed
- Legacy `ai-history` compatibility removed: the `AI_HISTORY_*` env prefix, the `~/.ai-history`
  auto-migration and the `X-AI-History-Revision` response header are gone. Use `LORE_*` env names,
  `~/.lore` as the data dir and `X-Lore-Revision`.
- **Warp extractor.** Lore no longer imports chat history from the Warp terminal (`lore/extractors/warp.py`,
  its noise rules, formatter and docker mount are gone). Sessions already ingested keep loading: the `warp`
  source value stays readable as a legacy tool.

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
