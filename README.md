# Lore

Local-first **archive** and **shared memory** for AI coding sessions — extract, search, and export your work across Claude Code, Cursor, GitHub Copilot, Aider, OpenCode, and more.

[![PyPI](https://img.shields.io/pypi/v/lore)](https://pypi.org/project/lore/)
[![Python](https://img.shields.io/pypi/pyversions/lore)](https://pypi.org/project/lore/)
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
pip install lore
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

---

## Supported Tools

| Tool | Data location | Notes |
|------|---------------|-------|
| **Claude Code** | `~/.claude/projects/` | JSONL conversation files |
| **Cursor** | `~/.cursor/` (SQLite) | Requires safe DB copy to avoid locks |
| **GitHub Copilot (VS Code)** | VS Code extension storage | Session logs from the Copilot Chat panel |
| **Copilot CLI** | Shell history / log files | Terminal Copilot sessions |
| **Gemini CLI** | `~/.gemini/` | Set `GEMINI_PROJECT_ROOTS` for path resolution |
| **Warp** | `~/.warp/` | AI session blocks from the Warp terminal |
| **Codex** | `~/.codex/` | OpenAI Codex CLI sessions |
| **OpenCode** | `~/.opencode/` | OpenCode agent sessions |
| **Antigravity** | Varies | Experimental tool support |
| **Aider** | `.aider.chat.history.md` per project | Markdown chat logs, discovered under your home dir |

---

## 🎯 Features

- **Unified History**: See all chats from Claude Code, Cursor, Gemini CLI, Codex, VSCode Copilot, Warp, and more in one place.
- **Shared Agent Memory**: A cross-tool knowledge store agents write to and recall from — facts, decisions, and lessons with provenance, searchable by keyword or by meaning.
- **Smart Context Switching**: Seamlessly move a session from one tool to another (e.g., Claude -> Gemini) when hitting rate limits.
- **Modern Web Dashboard**: A polished, SpecStory-inspired UI with Markdown rendering, code highlighting, and full-text search.
- **MCP Integration**: Query history and shared memory directly from within Claude Code and other MCP clients.
- **Privacy First**: Everything stays local on your machine. No cloud, no tracking.

---

## 🚀 Quick Start

### Installation

```bash
# From PyPI (recommended)
pip install lore

# Or from source
git clone https://github.com/DnaMes/lore.git
cd lore
pip install -e .
```

### Basic Commands

```bash
# Check available AI tools on your system
lore check

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
lore memory rate 7 5                                    # rate a memory 1-5

# Semantic memory search needs the optional embedding model:
#   pip install -e ".[semantic]"
# Without it, search transparently stays keyword-only. In the default mode only
# semantic matches with cosine similarity >= LORE_MEMORY_RECALL_MIN_SIMILARITY
# (default 0.7, uncalibrated) are added.

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
- `list_sessions`: List indexed sessions with filters.
- `get_session`: Load one session.
- `get_session_messages`: Load the transcript for one session.
- `get_thread`: Load cross-session thread context.
- `list_projects`: Enumerate projects in the history index.
- `switch_to_tool`: Hand off to `lore-session switch`.
- `memory_write`: Record a fact, decision, or lesson into shared agent memory.
- `memory_recall`: Recall from shared memory by keyword or meaning, with provenance.
- `memory_feedback`: Rate a recalled memory 1-5; the bounded weight is shown with it (`lore memory rate`).

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
