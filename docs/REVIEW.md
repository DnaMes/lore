# ai-history — Comprehensive Multi-Agent Review

Date: 2026-05-06
Reviewers (parallel subagents): code-reviewer, security-auditor, architect-reviewer, performance-engineer, qa-expert, researcher.
Source: 1672 sessions in `~/.ai-history/index.json` (19 MB), 78 MB SQLite, ~12 K LOC across `lore/`.

This document summarizes the findings. The actionable backlog is in `docs/ROADMAP.md`. Bulk issue creation was a one-shot script used in 2026-05, removed 2026-10-17.

---

## Top 3 to fix before public release

1. **Drop the unused Postgres + Redis stack** — `docker-compose.yml` ships `psycopg2-binary` and `redis` and exports `DATABASE_URL`/`REDIS_URL`. The codebase never imports either. Default password is `changeme`. Container runs as root. Public-release blocker; contradicts the "local-first" claim.
2. **Tool-call args bypass HTML sanitization** — `web_formatting.py:177-178` re-injects unsanitized HTML *after* `bleach.clean`. With the planned reverse-proxy deployment, untrusted JSONL content from extracted tools becomes XSS. Critical.
3. **CSRF disabled on every state-changing route** — every POST has `@csrf.exempt`. `/session/<id>/delete`, `/api/reload-sessions`, `/api/noise-rules` all CSRF-vulnerable behind a reverse proxy with cookies. High.

---

## Findings by category

### Bugs (FIXED or in progress)

| ID | Title | Severity | Status |
|----|-------|----------|--------|
| 1 | Claude sessions appeared stale (sort by `created` instead of `updated`) | high | **fixed** in d56d69c |
| 1c | `index.json` writes are non-atomic (SIGINT mid-write corrupts) | high | open |
| 1d | `safe_copy_db` leaks temp DB copies forever | medium | open |
| 1e | Extractor exceptions silently swallowed at `logger.debug` | medium | open |
| 1f | MCP server returns exception details in wire response | high | open |

### Security (20 findings; top items)

| ID | Title | Severity |
|----|-------|----------|
| 2 | Tool-call args bypass HTML sanitization (XSS via `web_formatting.py:177-178`) | **critical** |
| 3 | CSRF disabled on every state-changing API route (`@csrf.exempt`) | high |
| 4 | Path traversal via poisoned `export_path` (`web_data.py:307`) | high |
| 5 | Container runs as root with read-only mounts of host AI tool secrets | high |
| 6 | `POSTGRES_PASSWORD` defaults to `changeme` (until #1a removes the stack) | high |
| 7 | CSP allows `'unsafe-inline'` for scripts and styles | medium |
| 8 | Rate limiter trusts `X-Forwarded-For` without proxy validation | medium |
| 9 | Default Flask `SECRET_KEY` rotates per restart in production | medium |
| 12 | CDN script tags without Subresource Integrity (Tailwind, highlight.js, Google Fonts) | medium |
| 16 | `subprocess.run([gemini, prompt])` passes content as argv (ARG_MAX, leaks) | low |
| — | Strengths: parameterized SQL throughout, `subprocess shell=False`, request ID + rate limiting + CSP/HSTS headers in place | — |

### Architecture (strengths + concerns)

**Strengths:**
- `UnifiedSession` is a true canonical model. Best part of the system.
- `BaseExtractor` abstraction is clean — adding a new tool is a 1-file affair.
- Dual-track storage (`index.json` + SQLite FTS) works. FTS path falls back to JSON keyword scan; resilient.
- Job lifecycle is disciplined: TTL pruning + cancel flag + timeout assertion.

**Strategic concerns:**
- Full rebuild on every Sync — only `OpenCodeExtractor` has incremental sync; the other 8 don't. At 100K sessions this hits a 30-60s cliff.
- `RELOAD_JOBS` in-process dict forbids multi-worker. Confirmed at `web_jobs.py:25`. Fix already prepared (Redis is in compose, ironically, and SQLite is available).
- `web.py` at ~1300 LOC remains the dumping ground despite the modular split.
- `web_templates.py` (2,036 LOC of stringly-typed HTML/CSS/JS) was a smart shortcut at 200 LOC, negative at 2,000.

**Recommended target architecture for v3** is in `ROADMAP.md` items #44-#48.

### Performance (top 5 wins)

| Rank | Win | Impact | Effort |
|------|-----|--------|--------|
| 1 | Incremental index sync (mtime-based) | massive — Sync is the dominant pain point | medium (~1 day) |
| 2 | Strip `search_text`/`keywords` from dashboard payload | 75% payload + parse cost reduction | trivial (~1h) |
| 3 | FTS5 with `content=sessions` external content table | halves SQLite size (~40 MB reclaim) | low |
| 4 | Streamed indexing for huge sessions | 10-50× memory reduction for power users | medium |
| 5 | Pre-render markdown at index time + lazy-render rest client-side | large for long sessions | medium |

### QA (15-item punch list)

**Coverage gaps:** `antigravity`, `copilot`, `cursor`, `vscode` extractors have zero direct tests. Public `/api/v1/*` routes have zero contract tests.

**Top P0 additions:** parametrized contract test across all 11 extractors; tests for the 4 dark extractors; `/api/v1/*` route tests; `pytest-cov` + 80% gate; `pip-audit` in CI.

### Competitive landscape (researcher)

**Real competitors:**
- **claude-code-history-viewer** (jhlee0409) — 1.2k stars, Tauri desktop app, covers 7 tools (Claude, Gemini, Codex, Cline, Cursor, Aider, OpenCode). Has analytics + cost dashboard + i18n.
- **SpecStory** — VS Code extension + cloud SaaS. Auto-generates `.cursorrules` from history. Enterprise customers (Uber, NVIDIA).
- **claude-history** (raine) — 244 stars, Rust TUI, fuzzy search, fork/resume conversations. Claude only.
- **claude-run** — 592 stars, web UI, Claude only.

**Moats ai-history has:**
- Broadest tool coverage (9+, more than anyone except claude-code-history-viewer at 7).
- Local-first + multi-tool aggregation. Browser-extension competitors (Echoes, etc.) target cloud chats only.
- MCP server mode — unique.

**Gaps to close:**
- Token cost dashboard (claude-code-history-viewer has it; we don't).
- Polished rule generation (SpecStory's superpower; we have the command but no UI).
- Semantic search (nobody has shipped it).
- Static HTML export (SpecStory's cloud-share; we can do offline-first).

See ROADMAP.md items #34-#43 for the full feature backlog.

---

## How to use this review

1. **Triage**: open `docs/ROADMAP.md`, agree on what's P0 / P1 / P2 / P3 for the public release.
2. **Issues**: bulk creation used a one-shot script in 2026-05 (removed 2026-10-17); use `gh issue create` per backlog item with proper labels.
3. **Sequence**: ship the P0 fixes, tag v2.1, *then* go public. The Postgres ghost stack (#1a) and the XSS bypass (#2) are the two items that absolutely must land first.
4. **Track**: re-run the multi-agent review every quarter. Prompts are documented in this file's git history.

---

## Quick-fix progress

- [x] **#1** Claude sync (sort by `updated`) — d56d69c
- [ ] **#1a** Drop Postgres + Redis stack
- [ ] **#1b** Rename `_new` entry-point modules
- [ ] **#1c** Atomic index.json writes
- [ ] **#1d** safe_copy_db cleanup
- [ ] **#2** XSS in tool-call rendering
- [ ] **#3** CSRF on POST routes
- [ ] **#4** export_path containment check
- [ ] **#5** Container non-root user
