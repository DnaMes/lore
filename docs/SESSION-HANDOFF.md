# Session Handoff — 2026-05-06 (session 2)

Updated after the second session of 2026-05-06.

## TL;DR

- All P0 release blockers fixed (XSS #2, path traversal #4, Docker infra #1a, atomic writes #1c, non-root container #5, MCP error leak #1f).
- MCP server expanded with 5 new tools + `api_payloads.py` shared serializers.
- 129/129 tests passing.
- Remaining high-value next items: CSRF (#3), validate_session_id tightening (#15), Forgejo issue creation.
- Decision still pending: where to publish issues (Forgejo or GitHub).

---

## Commits this session (on top of `bf43570`)

| Hash | Description |
|------|-------------|
| `f012270` | feat: expand MCP server with get_session, get_thread, list_projects tools |
| `67f08bd` | fix: P0 security and infra hardening (#1a #1c #1f #4 #5) |
| `cf3c85f` | fix: XSS in format_message_content placeholder re-injection (#2) |

---

## What was done

### MCP expansion (`f012270`)

- `lore/interfaces/api_payloads.py` — new shared serializer module used by both MCP and HTTP API
- `lore/interfaces/mcp.py` — refactored to use api_payloads + web_data/web_services; 5 new tools: `get_session`, `get_session_messages`, `list_projects`, `get_thread`, `list_recent_sessions`
- `ai_history_mcp_new.py` — added `main_sync()` entry point
- `pyproject.toml` — added `ai-history-mcp` console script
- `README.md` — expanded with OpenCode MCP config + new tools list
- `docs/API_REFERENCE.md` — new file covering both MCP and HTTP surfaces
- `tests/test_api_and_mcp.py` — 4 contract tests

### P0 hardening (`67f08bd`)

- **#1a** `docker-compose.yml`: dropped Postgres + Redis services entirely; volumes now mount under `/home/ai` not `/root`
- **#5** `Dockerfile`: added `useradd -m -u 10001 ai` + `USER ai`; dropped `psycopg2-binary` and `redis` deps
- **#4** `resolve_export_path`: added `is_relative_to(OUTPUT_DIR)` containment check — prevents path traversal via poisoned `export_path` in index.json
- **#1c** `IndexBuilder.build_index`: atomic write via `NamedTemporaryFile` + `os.replace()` — no partial index on SIGINT
- **#1f** `MCPServer`: log exceptions server-side, return generic `"Internal error"` to peer (no stack trace / file path leak)
- Test updated: `test_session_delete_removes_index_entry_and_export` now places export_path inside fake OUTPUT_DIR

### XSS fix (`cf3c85f`)

- **#2** `web_formatting.py`: 
  - `repl_cmd_msg/name/args` now use `html.escape(m.group(2))` before building HTML
  - Added `sanitize_rendered_html()` pass at the end of `_format_message_content_cached` after all placeholder re-injection
  - `SANITIZE_TAGS` expanded with `details`, `summary` so tool-call/thinking collapsibles survive the final bleach pass

---

## Still open (prioritized)

### Must ship before public release

- [ ] **#3** Remove `@csrf.exempt` from POST routes (`web.py:677,699,726,1110,1250,1265`). May need API client updates. (4h+)
- [ ] **#1b** Rename `_new` suffix: `ai_history_mcp_new.py` → `lore.cli.mcp`, `ai_history_web_new.py` → `lore.cli.web`. Update `pyproject.toml:42-47`. (1h)
- [ ] **#1d** `safe_copy_db` cleanup of `/tmp/*.vscdb*` via context manager. (`utils/paths.py:64`) (30m)
- [ ] **#1e** Surface extractor exceptions to job result metadata instead of silent `logger.debug`. (`web_data.py:213`, `web.py:870`) (2h)

### Security hardening P1

- [ ] **#7** Replace CSP `'unsafe-inline'` with nonces or hashes. (`web.py:496-497`)
- [ ] **#8** Only honor `X-Forwarded-For` when `TRUSTED_PROXY` env is set. (`web_utils.py:75`)
- [ ] **#9** Refuse to start with ephemeral `SECRET_KEY` when `FLASK_ENV=production`. (`web.py:143`)
- [ ] **#10** Add `pip-audit` to CI.
- [ ] **#11** `requirements.lock` via `pip-compile`.
- [ ] **#12** SRI hashes on CDN assets.
- [ ] **#15** Tighten `validate_session_id` to `[A-Za-z0-9_.\-]{1,128}`.
- [ ] **#15a** Plan `bleach` → `nh3` migration.

### Performance P1

- [ ] **#16** Incremental index sync (per-session mtime check). Headline UX win. (1 day)
- [ ] **#17** Strip `search_text`/`keywords` from dashboard payload. (1-2h)

### Architecture / tech debt P2

- [ ] **#21** Stop re-exporting from `web.py`; delete `_TEST_EXPORTS`.
- [ ] **#25b** Split `mcp.create_server()` (445 LOC) into sub-functions.
- [ ] **#25c** Move HTML out of `web_templates.py` (2,036 LOC) to `lore/templates/*.html`.

### Features

- [ ] **#34** Token cost dashboard (1 week)
- [ ] **#35** Semantic search (sqlite-vec + Ollama)
- [ ] **#37** Polished `ai-history rules` UI

---

## Decision pending: where do issues go?

`tools/create_issues.sh` is ready for both targets:

1. **Forgejo** at `100.119.46.15:3000` (Tailscale-only, token at `~/.config/forgejo/token`):
   ```
   tools/create_issues.sh forgejo
   ```
   Run from a Tailscale-connected machine. The repo `erdna/ai-history` already exists.

2. **GitHub** (no public repo exists yet):
   ```
   gh repo create DnaMes/ai-history --public --source=. --remote=github --push
   tools/create_issues.sh github DnaMes ai-history
   ```

---

## Files of interest

| Path | Purpose |
|------|---------|
| `docs/ROADMAP.md` | Full prioritized backlog (60+ items) |
| `docs/REVIEW.md` | Multi-agent review synthesis |
| `docs/API_REFERENCE.md` | MCP + HTTP API docs |
| `tools/create_issues.sh` | Bulk issue creator |
| `lore/interfaces/web_formatting.py` | XSS fix location |
| `lore/interfaces/mcp.py` | MCP server (445 LOC, needs decomposition #25b) |
| `lore/interfaces/api_payloads.py` | Shared serializers (new) |
| `lore/interfaces/web.py:677` | CSRF hotspot (#3) |
| `lore/utils/paths.py:64` | safe_copy_db leak (#1d) |

---

## Session memory hints

- German for casual chat; English for code.
- User role: tech lead / hobbyist, multi-tool AI session aggregator.
- Forgejo (Tailscale-only) for private repos; GitHub for public OSS.
- Test runner: `.venv/bin/python -m pytest tests/`. Currently 129/129.
- `~/.lore/` is the canonical output dir.

## Next-session opening prompt suggestion

> Read docs/SESSION-HANDOFF.md and pick up from there. P0 blockers are
> fixed. Next priority: CSRF (#3) on web.py POST routes, then tighten
> validate_session_id (#15), then decide Forgejo vs GitHub and run
> tools/create_issues.sh.
