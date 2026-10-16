# Lore — Product Roadmap

Date: 2026-05-06 (updated after session 2)
Source: multi-agent review + competitive research (7 tools surveyed) + user feedback.

---

## P0 — Done (all fixed before public release)

| ID | What | Commit |
|----|------|--------|
| #1 | Claude sessions appeared stale — sort by `updated` not `created` | `d56d69c` |
| #1a | docker-compose shipped unused Postgres + Redis stack | `67f08bd` |
| #1c | Index writes were non-atomic (SIGINT could corrupt) | `67f08bd` |
| #1f | MCP server leaked exception details in wire response | `67f08bd` |
| #2 | XSS: tool-call HTML re-injected after bleach | `cf3c85f` |
| #3 | CSRF disabled on all POST routes | `aa3ad08` |
| #4 | Path traversal via poisoned `export_path` | `67f08bd` |
| #5 | Container ran as root | `67f08bd` |

---

## P0 — Still open (must ship before public announcement)

- [ ] **#1b** Rename `_new` suffix modules: `ai_history_mcp_new.py` → `lore/cli/mcp.py`, `ai_history_web_new.py` → `lore/cli/web.py`. Update `pyproject.toml` console scripts. (`pyproject.toml:42-47`) (1h)
- [ ] **#1d** `safe_copy_db` leaks `/tmp/*.vscdb` forever — add `try/finally` or context manager. (`utils/paths.py:64`) (30m)
- [ ] **#1e** Extractor exceptions silently swallowed at `logger.debug` — surface to job result metadata. (`web_data.py:213`, `web.py:870`) (2h)
- [ ] **#PyPI** Publish to PyPI: update `pyproject.toml` author email, bump `requires-python` to `>=3.11`, add `CHANGELOG.md`, test with `twine upload --repository testpypi`.
- [ ] **#README** Add screenshot/GIF of web UI to README — dramatically increases first impressions. Add CI + PyPI badges. Add quickstart (3 commands to running UI).
- [ ] **#COC** Add `CODE_OF_CONDUCT.md` (Contributor Covenant). Required for GitHub community health score.

---

## P1 — First month after release

### Security hardening

- [ ] **#7** Replace CSP `'unsafe-inline'` with nonces or hashes. (`web.py:496-497`)
- [ ] **#8** Only trust `X-Forwarded-For` when `TRUSTED_PROXY` env is set. (`web_utils.py:75`)
- [ ] **#9** Warn/refuse to start with ephemeral `SECRET_KEY` in `FLASK_ENV=production`. (`web.py:143`)
- [ ] **#10** Add `pip-audit` job to CI (`ci.yml`). Zero-CVE gate before releases.
- [ ] **#11** Generate `requirements.lock` via `pip-compile`, commit it.
- [ ] **#12** SRI hashes on Tailwind/highlight.js CDN assets, or vendor them (see #19).
- [ ] **#13** Drop `data:` from `img-src` CSP.
- [ ] **#15** Tighten `validate_session_id` to `[A-Za-z0-9_.\-]{1,128}`.
- [ ] **#15a** Plan `bleach` → `nh3` migration (`bleach` archived 2024).
- [ ] **#15b-c** Add type hints + `from __future__ import annotations` repo-wide.
- [ ] **#15d** Bump `requires-python` to `>=3.11` in `pyproject.toml`.
- [ ] **#15e** Switch pre-commit from black+isort+flake8 to `ruff` (format + check).
- [ ] **#bandit** Add `bandit -r lore/ -ll` to CI. Fix HIGH findings.

### Performance

- [ ] **#16** Incremental index sync — per-session mtime check, only re-parse changed files. From O(all) to O(changed). **Headline UX win.** (1 day)
- [ ] **#17** Strip `search_text`/`keywords` from dashboard payload. Split `index.json` into `index_meta.json` + `index_search.json`. (~75% payload reduction) (1-2h)
- [ ] **#18** FTS5 with `content=sessions` external content table. Halves SQLite size. (4h)
- [ ] **#19** Vendor Tailwind + highlight.js. Drop CDN, restore offline/air-gapped use. (4h)
- [ ] **#20** Streamed indexing — don't materialize all 15K messages for large sessions. (1 day)

### QA / CI

- [ ] **#26** Parametrized contract test across all 11 extractors.
- [ ] **#27** Tests for antigravity/copilot/cursor/vscode (currently zero coverage).
- [ ] **#28** Public `/api/v1/*` route contract tests.
- [ ] **#29** `pytest-cov` + 80% coverage gate; publish HTML artifact.
- [ ] **#30** Flask boot smoke test: `/api/health`, `/api/ready`, `/api/build-info`.
- [ ] **#31** Schema snapshot test for `index.json`.
- [ ] **#32** Property-based parser tests (`hypothesis`) — feed malformed JSONL.
- [ ] **#33** `pytest-randomly` — expose order-dependence.

---

## P1-P2 — Features (competitive gaps)

Sourced from surveying 7 competing tools: claude-code-history-viewer (1.2k★), raine/claude-history (200★), SpecStory (500★), cursor-chat-history-mcp, CASS, claude-historian-mcp, agent-sessions.

- [ ] **#34** **Token cost dashboard** — per-session, per-tool, per-project, over time. `jhlee0409/CCHV` ships this; it's the #1 requested feature in the space. (1 week) ⭐
- [ ] **#35** **Semantic search via local embeddings** — `sqlite-vec` + `nomic-embed-text` via Ollama. Zero competitors have shipped this. "Find sessions where I debugged auth" works; FTS misses it. (1 week) ⭐
- [ ] **#36** Shareable static HTML export — self-contained single file per session. SpecStory does cloud links; we can do offline-first.
- [ ] **#37** `lore rules` UI — polish the existing rule generation, expose as web UI. SpecStory's main hook.
- [ ] **#38** Session timeline / git-diff view — show file changes alongside the conversation. Nobody has shipped this. (Major)
- [ ] **#39** Project-level cost attribution — pairs with #34; lets devs justify AI spend.
- [ ] **#40** Noise filter web UI — drag-and-drop rules with live preview.
- [ ] **#41** MCP-over-HTTP transport — `--transport streamable-http` so Cursor/remote MCP hosts can query history without running a local process.
- [-] **#42** ~~Warp Block deeper import~~ — dropped: the Warp extractor was removed (2026-10-16).
- [ ] **#43** `lore digest` — weekly summary CLI command (sessions, cost, top projects). Habit-forming, trivial to ship.
- [ ] **#51** **Aider extractor** — `~/.aider/` chat logs. `jhlee0409/CCHV` already ships this; gap for users who run both. (4h)
- [ ] **#52** **File watcher / auto-sync daemon** — `inotify`/`watchdog` that indexes on JSONL append so the dashboard is always live without manual Sync. SpecStory's `specstory watch` analog. (1 day)
- [ ] **#53** **Scoped MCP search** — add `scope` param (`user_only`, `assistant_only`, `tool_results_only`) to `search_history` MCP tool. `claude-historian-mcp` has this. (2h)
- [ ] **#54** **Session resume button** — web UI "Resume in Claude" button that opens `claude --resume <session_id>`. `raine/claude-history` ships `--resume`. (4h)
- [ ] **#55** **Git commit linking** — tag sessions with the git SHA/branch active when the session started. `cursor-chat-history-mcp` does this. Enables "what was I building during this conversation?" (1 day)
- [ ] **#56** **Remote machine aggregation** — pull sessions from SSH hosts into the local index. CASS ships this for teams sharing a dev server. (1 week)

---

## P2 — Architecture / tech debt

- [ ] **#21** Stop re-exporting from `web.py`; delete `_TEST_EXPORTS`; update tests.
- [ ] **#22** Merge `web_helpers.py` into `web_services.py`.
- [ ] **#23** Hoist `_build_search_text`/`_infer_title` out of dual loops in `exporters/index.py`.
- [ ] **#24** Mtime-keyed cache for `load_sessions_for_tool`.
- [ ] **#25** `web_jobs.py` should own `_audit_*` helpers.
- [ ] **#25a** Decompose `session_detail` (199 LOC, cyclomatic ~25).
- [ ] **#25b** Decompose `mcp.create_server()` (445 LOC).
- [ ] **#25c** Move HTML out of `web_templates.py` (2,036 LOC) to `lore/templates/*.html`.
- [ ] **#25d** Build Jinja2 `Environment` once at module import (not per-request).
- [ ] **#25e** `BaseExtractor.iter_sqlite_dbs()` helper — DRY the copy-connect-try pattern.
- [ ] **#25f** Centralize tool data root paths in `utils/paths`.

---

## P3 — v3 redesign (strategic)

- [ ] **#44** Single SQLite source of truth (drop `index.json`).
- [ ] **#45** Persistent JobStore (`SQLiteJobStore` default). Unblocks `gunicorn --workers >1`.
- [ ] **#46** Templates → Jinja files on disk + Tailwind CLI prebuild.
- [ ] **#47** Optional FastAPI alongside Flask.
- [ ] **#48** Plugin extractor SDK (`pip install lore-extractor-foo`).

---

## Marketing / positioning

- [ ] **#49** Lead README with "your AI sessions ARE documentation" — SpecStory's framing + our multi-tool moat.
- [ ] **#50** Comparison table in README vs claude-code-history-viewer / claude-history / SpecStory / CASS.

---

## Triage notes

- Items #1b, #1d, #1e, #PyPI, #README, #COC → ship before public announcement.
- Items #7-#33 → v2.1 milestone, within 30 days of release.
- Items #34, #35, #51, #52 are the highest-impact post-release features (competitive gaps).
- v3 redesign (#44-#48) only once user count justifies the rework.
