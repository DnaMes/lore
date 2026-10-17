# Lore

Local-first **archive** and **shared memory** for AI coding sessions — extract, search, and export your work across Claude Code, Cursor, GitHub Copilot, Aider, OpenCode, and more.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/DnaMes/lore/actions/workflows/tests.yml/badge.svg)](https://github.com/DnaMes/lore/actions/workflows/tests.yml)
[![Security](https://github.com/DnaMes/lore/actions/workflows/security.yml/badge.svg)](https://github.com/DnaMes/lore/actions/workflows/security.yml)
[![GitHub Issues](https://img.shields.io/github/issues/DnaMes/lore)](https://github.com/DnaMes/lore/issues)

**The Local-First Knowledge Layer for AI Coding.**

Lore is two things in one tool:

- **An archive** — it collects, unifies, and exports chat histories from all your AI coding assistants (~10 tools) into one searchable local store, with no cloud connectivity.
- **A shared agent memory** — a cross-tool knowledge store that agents write to and recall from (keyword + semantic search, with provenance), so a decision made in one tool is available to the next.

The name carries both halves: *lore* is accumulated knowledge **and** history/backstory.

---

## Quickstart

```bash
git clone https://github.com/DnaMes/lore.git
cd lore
pip install -e .
lore export --all   # index all sessions
lore-web            # open browser at http://localhost:5000
```

Or with Docker:

```bash
docker compose up -d
```

---

## Web UI Preview

```
┌─────────────────────────────────────────────────────────┐
│  Lore  Sessions  Stats  Threads  Projects  Memory  Rules │
├─────────────────────────────────────────────────────────┤
│ 🔍 Search sessions...                     [Sync ▾]     │
├──────────────┬──────────────────────────────────────────┤
│ claude-code  │  fix: sqlite locking in cursor extractor │
│ gemini-cli   │  refactor: incremental index sync        │
│ opencode     │  feat: session resume button             │
│ cursor       │  docs: update README with quickstart     │
└──────────────┴──────────────────────────────────────────┘
```

> Full screenshots coming soon. Run `lore-web` to see the live dashboard.

On a session detail page, use **Find** (or press `/`) to search the full transcript across lazy-loaded pages; **Prev** / **Next** jump between hits with in-page highlighting.

---

## Supported Tools

| Tool | Data location | Notes |
|------|---------------|-------|
| **Claude Code** | `~/.claude/projects/` | JSONL conversation files |
| **Cursor** | `~/.cursor/` (SQLite) | Requires safe DB copy to avoid locks |
| **GitHub Copilot (VS Code)** | VS Code extension storage | Session logs from the Copilot Chat panel |
| **Copilot CLI** | Shell history / log files | Terminal Copilot sessions |
| **Gemini CLI** | `~/.gemini/` | Set `GEMINI_PROJECT_ROOTS` for path resolution |
| **Codex** | `~/.codex/` | OpenAI Codex CLI sessions |
| **OpenCode** | `~/.opencode/`, `opencode.db` | JSON storage plus SQLite `session` / `session_v2` (scratch copy) |
| **Antigravity** | Varies | Experimental tool support |
| **Aider** | `.aider.chat.history.md` per project | Markdown chat logs, discovered under your home dir |
| **Pi** | `~/.pi/agent/sessions/` | JSONL v3 tree transcripts (also reads `~/.omp`, `~/.prime`) |
| **Command Code** | `~/.commandcode/projects/` | v3 and flat JSONL transcripts, `.meta.json`, print-mode `.checkpoints.jsonl` |
| **Hermes** | `~/.hermes/profiles/*/state.db`, `~/.hermes/state.db` | SQLite sessions and messages (scratch copy; never opens originals) |
| **Cline** | VS Code `globalStorage/saoudrizwan.claude-dev/tasks/`, `~/.cline/data/sessions/` | Fixtures from the documented schema, not verified on real data |
| **Roo Code** | VS Code `globalStorage/rooveterinaryinc.roo-cline/tasks/` | Fixtures from the documented schema, not verified on real data |
| **Kilo Code** | VS Code `globalStorage/kilocode.kilo-code/tasks/` | Fixtures from the documented schema, not verified on real data |
| **Goose** | `~/.local/share/goose/sessions/sessions.db` (and alternate roots) | Fixtures from the upstream schema, not verified on real data |
| **Crush** | `~/.local/share/crush/projects.json` → `crush.db` | Fixtures from the upstream schema, not verified on real data |
| **Kimi** | `~/.kimi/sessions/<md5>/<session_id>/context.jsonl` | Fixtures from the upstream schema, not verified on real data |
| **Qwen Code** | `~/.qwen/projects/<slug>/chats/*.jsonl` (and legacy `tmp/`) | Fixtures from the upstream schema, not verified on real data |
| **Zed** | `~/.local/share/zed/threads/threads.db` (and `$XDG_DATA_HOME`) | Fixtures from upstream format descriptions, not verified on real data |
| **Factory Droid** | `~/.factory/sessions/<slug>/<uuid>.jsonl` | Fixtures from third-party format descriptions, not verified on real data |

---


## 🎯 Features

- **Unified History**: See all chats from Claude Code, Cursor, Gemini CLI, Codex, VSCode Copilot, and more in one place.
- **Shared Agent Memory**: A cross-tool knowledge store agents write to and recall from — facts, decisions, and lessons with provenance, searchable by keyword or by meaning.
- **Smart Context Switching**: Seamlessly move a session from one tool to another (e.g., Claude -> Gemini) when hitting rate limits.
- **Modern Web Dashboard**: A polished, SpecStory-inspired UI with Markdown rendering, code highlighting, and full-text search.
- **MCP Integration**: Query history and shared memory directly from within Claude Code and other MCP clients.
- **Privacy First**: Everything stays local on your machine. No cloud, no tracking.

---

## 🚀 Quick Start

### Installation

Install from a clone:

```bash
git clone https://github.com/DnaMes/lore.git
cd lore
pip install -e .
```

### Basic Commands

```bash
# Check available AI tools on your system
lore check

# Run every available extractor without writing anything and report, per tool,
# matched sources, parsed sessions, skips and unknown records
lore check --deep

# List recent sessions
lore list --since 7d

# Weekly activity digest (sessions by day, tool, top projects)
lore digest --since 7d
lore digest --since 2w --format markdown

# Shared cross-tool memory — record a fact/decision any AI tool can recall
lore memory add --kind decision --title "Use Postgres 16" \
    --body "Standardise the stack on PG16" --tag db --tag infra
lore memory search postgres
lore memory search "database performance"              # keywords + meaning when available
lore memory search "database performance" --semantic   # meaning only, no relevance floor
lore memory search '"exact phrase"'                     # quoted phrase
lore memory list
lore memory rate 7 5                                    # rate a confirmed memory 1-5
lore memory add --title "Use PG 17" --body "..." --supersedes 7   # replaces #7; near-duplicates are listed
lore memory import-claude                                # dry run: count Claude auto-memory files to import
lore memory import-claude --apply                        # write them (re-runs skip unchanged files; edits supersede)

# Semantic memory search needs the optional embedding model:
#   pip install -e ".[semantic]"
# Without it, search transparently stays keyword-only. In the default mode only
# semantic matches with cosine similarity >= LORE_MEMORY_RECALL_MIN_SIMILARITY
# (default 0.6, calibrated 2026-10-14) are added.

# Search across all sessions
lore search "database migration"

# Start the Web UI
lore-web

# Run a tool and sync its session after exit
lore run codex

# Sync existing sessions for a tool
lore sync gemini

# List threads for cross-tool continuation
lore threads

# Generate derived rules (SpecStory-style)
lore rules --limit 30
```

`lore check --deep` reads every source of each available tool and prints how many
sources matched, how many sessions were parsed, which were skipped and why, and how
many records the parser did not recognise. It warns when a tool's data directory exists
but no session comes out (a stale glob or a changed file format). It writes no archive
or index. Plain `lore check` stays fast and also shows the counters of the last sync.

---

## 🔄 Session Management (The Switcher)

The `lore-session` tool allows you to jump between tools with full context.

### Scenario: Claude Code Rate Limit Reached

1. You are in Claude and see "Usage Limit Reached".
2. Run: `lore-session switch gemini`
3. Gemini CLI starts with the last 15 messages from your Claude session pre-loaded as context.

```bash
# Manual switch
lore-session switch <tool> [--messages N]

# Continue a specific thread
lore-session switch <tool> --thread-id <thread-id>

# Auto-select best tool
lore-session continue
```

## 🧰 SpecStory-Style Workflow

Lore supports a SpecStory-like flow:

```bash
# Wrap a tool session, then sync after it ends
lore run claude

# Backfill/sync existing sessions for a tool
lore sync codex
```

`lore run` writes a PTY log to `~/.lore/runs/` for audit/debugging.

---

## MCP Integration

Lore ships a [Model Context Protocol](https://modelcontextprotocol.io/) server so Claude Code (and other MCP-capable clients) can search your own session history and shared memory mid-conversation — without leaving the editor.

**Use case:** Ask Claude Code `"Search my history for sqlite locking fixes"` and it will query your local indexed sessions in real time. Agents can also write decisions and lessons to shared memory and recall them later from any tool.

### Claude Code setup

Add this block to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "lore": {
      "command": "lore-mcp",
      "args": []
    }
  }
}
```

### OpenCode setup

Add this block to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "lore": {
      "type": "local",
      "command": ["lore-mcp"]
    }
  }
}
```

Available MCP tools: `search_history`, `list_sessions`, `get_session`, `get_session_messages`, `get_thread`, `list_projects`, `switch_to_tool`, `memory_write`, `memory_recall`, `memory_feedback`.

See [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) for full inputs, outputs, and HTTP endpoint reference.

---

### Available MCP tools

- `search_history`: Find matching past sessions.
- `list_sessions`: List indexed sessions with filters (`include_automated` adds exec/cron/subagent sessions).
- `get_session`: Load one session.
- `get_session_messages`: Load the transcript for one session.
- `get_thread`: Load cross-session thread context.
- `list_projects`: Enumerate projects in the history index.
- `switch_to_tool`: Hand off to `lore-session switch`.
- `memory_write`: Record a fact, decision, or lesson into shared agent memory.
- `memory_recall`: Recall from shared memory by keyword or meaning, with provenance.
- `memory_feedback`: Rate a confirmed memory 1-5 (not candidates); the bounded weight is shown with it (`lore memory rate`).

### API reference

See `docs/API_REFERENCE.md` for:

- MCP tool inputs and outputs
- HTTP JSON endpoints under `/api/v1/...`
- agent retrieval guidance for Claude Code, OpenCode, and other tools

---

## 🎨 Web UI

Browse your history at `http://localhost:5000`.

- **Dashboard**: High-level stats and recent activity.
- **Search**: `Cmd+K` global search with SQLite FTS.
- **Chat View**: SpecStory-style session layout with left TOC.
- **Threads**: Cross-tool continuity view of a project thread.
- **Memory**: Browse, search (keyword or semantic), and manage shared agent memory.
- **Rules**: Derived rules extracted from assistant responses.
- **Export**: Save any session as a clean Markdown file.
- **Select mode** (sessions list): toggle **Select**, then check rows or press `x` on the focused row to bulk **Delete…** or **Tag…** (in-page confirm, no native dialog).
- **Mobile sheets**: **Filters** and **Prompts** open as bottom sheets below 1024px with focus trap; **Escape** closes an open sheet.
- Keyboard handlers ignore modified keys and events from inputs, textareas, selects, or `contenteditable` fields. Press `?` for the full list (disable shortcuts from that dialog).

| Page | Key | Action |
|------|-----|--------|
| Sessions list | `j` / `k` | Move focus between rows |
| Sessions list | `Enter` | Open focused session |
| Sessions list | `/` | Focus tool filter (desktop) |
| Sessions list | `x` | Toggle row selection (select mode) |
| Session detail | `j` / `k` | Previous / next prompt (`#msg-N`) |
| Session detail | `e` / `c` | Expand / collapse all tool groups |
| Session detail | `/` | Find in session |
| Either | `?` | Keyboard shortcuts help |

Plain `lore export` files are **unredacted** local archives by design — they stay on your machine
with full fidelity for search and replay. When you need to share or publish, use
`lore export --redact --output-dir /path/to/share` (or `lore export-html SESSION --redact`, or
`/export/<id>?redact=1` in the web UI). Redaction applies best-effort pattern matching for
secret-shaped strings (OpenAI/Anthropic keys, JWTs, PEM private keys, assignment-style secrets,
and more — see the `Kinds` list in [`lore/utils/redact.py`](lore/utils/redact.py)). Review
redacted output before sharing; it is not a guarantee that nothing sensitive remains.

### Security Probe Matrix

Run deterministic route/API hardening probes against a local or deployed web URL:

```bash
# Local
lore-web-probe --base-url http://127.0.0.1:5000

# Deployed target protected by Basic auth
export AI_HISTORY_WEB_PROBE_PASSWORD='your-password'
lore-web-probe \
  --base-url https://your-deployed-host.example.com \
  --user admin \
  --password-env AI_HISTORY_WEB_PROBE_PASSWORD


# Increase timeout/retries when remote index fallback is slow
lore-web-probe --base-url https://your-deployed-host.example.com --user admin \
  --password-env AI_HISTORY_WEB_PROBE_PASSWORD --timeout-seconds 30 --timeout-retries 2
```

Probe output now includes a `build_info` object fetched from `/api/build-info` (status,
revision, module) so deployed/runtime drift can be diagnosed quickly during parity checks.
Query-parameter hardening probes use URL-encoded metacharacters (for example `%3B`) so
gateway/proxy normalization does not mask application-level validation behavior.

You can query it directly:

```bash
curl -u admin:"$AI_HISTORY_WEB_PROBE_PASSWORD" \
  https://your-deployed-host.example.com/api/build-info
```

Note: `/export/<session_id>` first serves linked markdown from `index.json`. If the session
is indexed but the markdown file is missing, it now regenerates markdown from the live
session for that tool. For unknown session ids, it returns `404` directly to avoid
expensive scans unless you explicitly set `AI_HISTORY_EXPORT_FALLBACK_SCAN=true`.

Exit codes:

- `0`: all probes passed
- `1`: probe failures
- `3`: blocked by upstream gateway auth (for example Traefik `401`)

---

## 🏗️ Architecture

The product is **Lore**, but the Python import package is `lore` (the
import name was deliberately not renamed).

- `lore/core`: Data models and unified session logic.
- `lore/extractors`: Tool-specific logic for parsing histories.
- `lore/exporters`: Markdown and Context formatting.
- `lore/interfaces`: CLI, Web, and MCP entry points.

---

## 🛠️ Requirements

- Python 3.11+
- Flask (for Web UI)
- Markdown (for rendering)
- highlight.js (vendored — no CDN, works offline)
- Tailwind CSS (vendored — no CDN, works offline)

## ⚙️ Gemini Project Resolution

If Gemini CLI projects don't map back to a path on your machine, set:

```bash
export GEMINI_PROJECT_ROOTS="$HOME/projects:$HOME/work:$HOME/code"
```

This helps Lore resolve Gemini's project hash to real folders.

---

## `lore improve`

`lore improve` runs a fixed pipeline: backfill memory embeddings, optional knowledge
extraction, promotion of stored extractions into reviewable memory **candidates**, and
proposals from session summaries. It has **no cloud LLM default** — configure
`--provider` or `LORE_LLM_PROVIDER` for the extraction stage. Use `--dry-run` to count
candidates without writing; `--json` prints a per-stage report. Exit code 1 only when a
stage errors.

## AI session summaries

`lore summarize` writes validated JSON summaries to the v2 archive (`session_summaries` in
`~/.lore/lore.sqlite`, surfaced in the web UI). The default provider is local **Ollama**;
cloud providers (for example Gemini) send redacted transcript text only when you pass
`--allow-cloud`. Transcript-bearing runs never use the LLM disk cache. Run
`lore summarize --dry-run` first to see per-session token and call estimates. Large backlogs
can be slow and costly — use `--max-sessions`, `--max-input-tokens`, and `--since` (default
30 days when no `--session` is given).

**MCP.** `get_session_summary` is read-only and returns stored summary JSON (no
transcript). `summarize_session` is off by default; set `LORE_MCP_ALLOW_SUMMARIZE=1` to
enable it. When enabled, only a local Ollama endpoint on localhost is used — never a cloud
provider — because an agent reading untrusted session content must not trigger sending
private text off-machine (prompt-injection / exfiltration risk).

## Automated sessions

Sessions that are not a person talking to an assistant carry an `origin` of `exec`,
`cron` or `subagent`. Lore stores them (the archive stays complete, and they skip the
minimum-user-prompts filter that drops tiny interactive sessions) but hides them from
default views and from the LLM stages. `interactive` and `unknown` sessions behave
exactly as before.

| Surface | Show them |
| --- | --- |
| `lore list`, `search`, `stats`, `digest`, `threads`, `rules`, `generate-titles`, `analyze`, `format`, `knowledge`, `improve`, `summarize`, `prune` | `--include-automated` |
| Web UI (`/`, `/sessions`, `/projects`, `/threads`, `/stats`, `/api/v1/...`) | `?automated=1`, or the "Automated" checkbox on the sessions list |
| MCP `list_sessions`, `search_history`, `list_recent_sessions`, `list_projects`, `get_thread` | argument `include_automated: true` |

Where totals are shown the number of hidden sessions is printed ("12 automated sessions
hidden"; a thread shows its own count for hidden subagent children, and counts follow
the active filters). Filtering the sessions list by a tag does not hide automated
sessions. Token and cost totals (`/api/stats/costs`) always cover every session, since the
spend happened either way; the payload carries `automated_included_line` ("includes 3
automated sessions"). A session or thread opened by its id always works, hidden or not, and
`lore export` writes every session. `lore prune` never removes automated sessions unless
`--include-automated` is given. Today the Claude Code and Gemini CLI extractors tag their
subagent sessions `subagent` (only those already imported before, so the index does not
grow); other tools gain `exec`/`cron` detection with their extractor work. Claude Code and
Gemini CLI subagent sessions also record their parent (`parent_session_id`, `relation` of
`subagent`) when the path names it.

Boolean switches (`include_automated`, `?automated=`) are parsed strictly: `true`/`1`/`yes`/`on`
and `false`/`0`/`no`/`off` (or a real boolean, or the integers `0`/`1`) work, anything else
is an error (MCP: an error string, web and `/api/v1`: HTTP 400). A repeated `?automated=`
takes its first value.

### Re-extraction after a parser upgrade

When an extractor learns to produce more fields, its `PARSER_VERSION` rises and sessions
indexed by the older parser are parsed again once, even if the source file did not change.
The first sync after such an upgrade therefore re-extracts sessions in batches: each run
re-parses at most `LORE_PARSER_REEXTRACT_BUDGET` stale sources per tool (default `150`) and
the next runs drain the rest. `0` re-parses no stale source in that run (changed sources
are still parsed), a negative value removes the cap, an invalid value falls back to `150`.
The variable is read on every run, so `LORE_PARSER_REEXTRACT_BUDGET=0` persists across runs:
existing Claude Code and Gemini CLI subagent sessions stay untagged (and visible) until the
variable is unset.

---

## 📜 License

MIT License. Built for developers who value their context.

## Production Ops

The web interface now includes built-in hardening and operational endpoints for deployment checks and monitoring.

### Health, readiness, and metrics

```bash
# Liveness (process up + revision)
curl http://127.0.0.1:5000/api/health

# Readiness (filesystem/index checks)
curl -i http://127.0.0.1:5000/api/ready

# JSON metrics counters
curl http://127.0.0.1:5000/api/metrics

# Prometheus text format
curl http://127.0.0.1:5000/api/metrics?format=prom
```

### Request tracing and headers

- Every response includes `X-Request-ID` for correlation.
- API responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`.
- Security headers include stricter CSP, `X-Frame-Options`, and `Referrer-Policy`.

### Rate limiting configuration

```bash
# Master switch
export AI_HISTORY_RATE_LIMIT_ENABLED=true

# Shared window in seconds
export AI_HISTORY_RATE_LIMIT_WINDOW_SECONDS=60

# Per-endpoint budgets per window
export AI_HISTORY_RATE_LIMIT_MAX_API_REQUESTS=240
export AI_HISTORY_RATE_LIMIT_SEARCH_PER_WINDOW=180
export AI_HISTORY_RATE_LIMIT_AUDIT_PER_WINDOW=20
export AI_HISTORY_RATE_LIMIT_RELOAD_PER_WINDOW=12
```

### Logging mode

```bash
# Optional JSON request logs for easier ingestion
export AI_HISTORY_JSON_REQUEST_LOGS=true
```

When enabled, API/error requests are logged as JSON objects with method, path, status, duration, and request id.

### Docker Compose production defaults

Container runtime uses Gunicorn (`lore.interfaces.web:app`) instead of Flask development server.

`docker-compose.yml` defines a single `app` service with a health check. There is no `db`/`redis` — the store is local SQLite (+WAL); Gunicorn runs `--workers 1` because the reload-job state is in-memory.

```bash
# Validate compose config
docker compose config

# Start stack in background
docker compose up -d

# Watch health state
docker compose ps
```

Example `.env` values for deployment:

```bash
AI_HISTORY_RATE_LIMIT_ENABLED=true
AI_HISTORY_RATE_LIMIT_WINDOW_SECONDS=60
AI_HISTORY_RATE_LIMIT_MAX_API_REQUESTS=240
AI_HISTORY_RATE_LIMIT_SEARCH_PER_WINDOW=180
AI_HISTORY_RATE_LIMIT_AUDIT_PER_WINDOW=20
AI_HISTORY_RATE_LIMIT_RELOAD_PER_WINDOW=12
AI_HISTORY_JSON_REQUEST_LOGS=false
```
