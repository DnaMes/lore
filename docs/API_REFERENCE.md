# API Reference

`lore` exposes two integration surfaces for agents and tools:

1. MCP over stdio via `lore-mcp`
2. HTTP JSON endpoints via `lore-web`

Use MCP when the client already supports tool calling. Use HTTP when you are wiring a custom tool, service, plugin, or skill.

## Quick Start

Install or refresh entry points:

```bash
pip install -e .
```

Start the web API locally:

```bash
lore-web
```

Start the MCP server manually:

```bash
lore-mcp
```

## Claude Code

Add this to `~/.claude/settings.json`:

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

If `lore-mcp` is not on `PATH`, use the full executable path.

Example prompts in Claude Code:

- `Search my history for sqlite locking fixes`
- `Find recent claude-code sessions in /repo/demo`
- `Show me the messages from session abc123`
- `Load thread thread-1 and summarize the prior work`

## OpenCode

Add this to `opencode.json`:

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

If needed, replace `lore-mcp` with an absolute path.

## MCP Tools

All MCP tools return JSON text payloads so agents can parse the result deterministically.

### `search_history`

Search indexed sessions.

Input:

```json
{
  "query": "sqlite lock",
  "tool": "claude-code",
  "project": "/repo/demo",
  "limit": 10
}
```

Output shape:

```json
{
  "query": "sqlite lock",
  "tool": "claude-code",
  "project": "/repo/demo",
  "count": 1,
  "results": [
    {
      "id": "session-1",
      "tool": "claude-code",
      "title": "Claude debugging session",
      "created": "2026-04-01T10:00:00",
      "updated": "2026-04-01T10:05:00",
      "project": "/repo/demo",
      "thread_id": "thread-1",
      "message_count": 2,
      "prompt_count": 1,
      "export_path": null,
      "score": 0.42,
      "snippet": "database is «locked» during export",
      "source": "session"
    }
  ]
}
```

`snippet` is the matching passage with `«`/`»` around matched terms (null when
unavailable); escape it before rendering. A query that is one double-quoted
phrase (`"exact phrase"`) matches that phrase. A quoted single word is an exact
token, so `"oauth"` no longer matches `OAuth2` (unquoted terms still prefix-match).
The legacy fallback ignores phrase adjacency.

### `list_sessions`

List indexed sessions.

Input fields:

- `tool`: optional
- `project`: optional substring match
- `thread_id`: optional
- `since`: optional duration like `7d`
- `limit`: optional, default `20`

### `get_session`

Fetch one session.

Input:

```json
{
  "session_id": "session-1",
  "include_messages": true
}
```

Returns live session data when available. Otherwise returns indexed summary data with `"live": false`.

### `get_session_messages`

Fetch only messages for one session.

Input:

```json
{
  "session_id": "session-1",
  "limit": 200
}
```

### `list_recent_sessions`

List recently updated sessions.

Input:

```json
{
  "limit": 10
}
```

### `list_projects`

List projects present in the index.

Input:

```json
{
  "limit": 50
}
```

### `get_thread`

Fetch a thread overview and optional merged messages.

Input:

```json
{
  "thread_id": "thread-1",
  "include_messages": true
}
```

### `switch_to_tool`

Prepare a handoff to another CLI tool through `ai-session switch`.

Input:

```json
{
  "tool": "gemini-cli",
  "max_messages": 15,
  "thread_id": "thread-1"
}
```

### `memory_write`

Record a durable memory. Input: `title` and `body` (required), `kind`
(`fact|decision|todo|snippet|link|lesson|note`), `project`, `tool`, `tags`,
`author`, `source_session` (session id the memory came from) and `supersedes`
(integer id of the memory this one replaces).

Output:

```json
{
  "status": "stored",
  "memory_id": 12,
  "kind": "note",
  "similar": [{"id": 7, "title": "Use Postgres 16", "similarity": 0.91}],
  "similar_available": true,
  "superseded": 7
}
```

- `similar` lists up to 3 near-duplicates (active, `confirmed`, same `project`
  scope, cosine similarity at or above `LORE_SIMILARITY_THRESHOLD`, default
  `0.85`, a placeholder pending calibration in #92; invalid values fall back to
  the default, range 0 to 1), computed before the new memory is stored. Nothing
  is superseded automatically. Without the optional `semantic` extra `similar` is
  `[]` and `similar_available` is `false`.
- `superseded` is present only when `supersedes` was given. The new memory is
  stored and the old one's `superseded_by` is set in one transaction. The target
  must exist, be `confirmed`, not yet superseded and have the same `project`
  scope (a different `kind` is allowed); otherwise nothing is written and the
  tool returns an error string.

#### Memory status

A memory is `candidate`, `confirmed` or `rejected` (`status` key in every memory
payload). Writes through `memory_write` and `lore memory add` are `confirmed`.
Only `confirmed` memories are returned by `memory_recall`, `lore memory list` /
`search` and the web `/memory` page; candidates are produced by later review
workflows and change state only `candidate -> confirmed|rejected`. Rejected rows
are kept.

### `memory_recall`

Search the shared memory store.

Input:

```json
{
  "query": "database performance",
  "kind": "decision",
  "project": "/repo/demo",
  "include_superseded": false,
  "semantic": null,
  "limit": 10
}
```

`semantic` is a tri-state:

- absent or `null` (auto): keyword search; when the optional `semantic` extra is
  installed and a memory has a stored vector, memories similar in meaning are
  fused in (reciprocal rank fusion). Memories without a vector are still found by
  keyword. Only semantic candidates with cosine similarity at or above
  `LORE_MEMORY_RECALL_MIN_SIMILARITY` (default `0.7`, a placeholder pending
  calibration in #92; invalid values fall back to the default, range 0 to 1)
  take part.
- `false`: keyword search only; the embedding model is never loaded.
- `true`: rank by embedding similarity with no floor; falls back to keywords when
  the backend or stored vectors are missing.

A query that is one double-quoted phrase matches that phrase (same rules as
`search_history`).

Output shape:

```json
{
  "count": 1,
  "memories": [
    {
      "id": 7,
      "kind": "decision",
      "title": "Use Postgres 16",
      "body": "Standardise the stack on PG16",
      "author": "agent",
      "created": "2026-04-01T10:00:00+00:00",
      "updated": "2026-04-01T10:00:00+00:00",
      "scope_project": null,
      "scope_tool": null,
      "superseded_by": null,
      "tags": ["db"],
      "snippet": "Standardise the stack on «PG16»",
      "source": "memory",
      "sources": [{"session_id": "session-1", "note": "recorded from this session"}],
      "feedback_weight": 0.5,
      "feedback_count": 0,
      "status": "confirmed"
    }
  ]
}
```

`snippet` is set for keyword hits only. `sources` lists the sessions the memory was
recorded from (session ids only, empty when none); `source` is always `"memory"`.
`feedback_weight` (0 to 1, `0.5` when unrated) and `feedback_count` come from
`memory_feedback` ratings and are always present.

### `memory_feedback`

Rate a recalled memory. Input: `memory_id` (integer), `score` (integer 1 to 5) and
optional `rater` (default `agent`, at most 64 chars, no control characters).

```json
{"memory_id": 7, "score": 5, "rater": "cursor"}
```

Output: `{"status": "recorded", "weight": 0.625, "count": 1}`. Invalid input or an
unknown memory returns a plain error string. One rating per rater and memory: a new
rating replaces the old one. The same operation is `lore memory rate <id> <1-5>
[--rater NAME]`.

The weight is `(sum((score - 1) / 4) + 1.5) / (n + 3)`, a mean pulled toward the
neutral 0.5 by three virtual votes. It is derived at read time, never stored, and
only shown by default. Set `LORE_FEEDBACK_INFLUENCE` (0 to 1, default `0`, invalid
values fall back to `0`) to let it nudge `memory_recall` ranking: results are
reordered by `1/(60 + rank)` times `1 + influence * (0.75 + 0.5 * weight - 1)`.
Caveat: rater names are unverified labels, so one party can pose as many raters;
the influence is therefore bounded to +-25% and off by default.

## HTTP API

Base URL:

```text
http://127.0.0.1:5000
```

All endpoints below are read-only.

### `GET /api/v1/search`

Query params:

- `q`: required for useful results, minimum 2 chars
- `tool`: optional
- `project`: optional
- `limit`: optional, default `20`, max `200`

Example:

```bash
curl 'http://127.0.0.1:5000/api/v1/search?q=sqlite&tool=claude-code'
```

### `GET /api/v1/sessions`

Query params:

- `tool`: optional
- `project`: optional substring match
- `thread_id`: optional
- `q`: optional search term
- `limit`: optional, default `50`, max `500`

Example:

```bash
curl 'http://127.0.0.1:5000/api/v1/sessions?project=%2Frepo%2Fdemo&limit=25'
```

### `GET /api/v1/sessions/<session_id>`

Query params:

- `live=1`: allow broader live fallback lookup across tools

Example:

```bash
curl 'http://127.0.0.1:5000/api/v1/sessions/session-1'
```

### `GET /api/v1/sessions/<session_id>/messages`

Query params:

- `limit`: optional, default `500`, max `5000`

Example:

```bash
curl 'http://127.0.0.1:5000/api/v1/sessions/session-1/messages?limit=100'
```

### `GET /api/v1/projects`

Query params:

- `tool`: optional
- `limit`: optional, default `100`, max `500`

### `GET /api/v1/threads`

Query params:

- `limit`: optional, default `100`, max `500`

### `GET /api/v1/threads/<thread_id>`

Returns:

- `thread`: aggregate metadata
- `timeline`: indexed sessions in the thread
- `messages`: merged live messages across sessions
- `toc_items`: prompt previews
- `groups`: per-tool groupings
- `continue_command`: suggested `ai-session` continuation command

## Common Response Fields

Session summary fields:

- `id`
- `tool`
- `title`
- `display_title`
- `created`
- `updated`
- `project`
- `thread_id`
- `message_count`
- `prompt_count`
- `export_path`

Live session detail adds:

- `assistant_message_count`
- `total_tokens`
- `summary`
- `git_branch`
- `source_path`
- `cli_version`
- `live`

Message fields:

- `index`
- `message_id`
- `role`
- `timestamp`
- `content`
- `model`
- `tokens`
- `tool_calls`
- `reasoning`

## Integration Guidance For Agents

Recommended retrieval flow:

1. Use `search_history` or `GET /api/v1/search` to find candidate sessions.
2. Use `get_session` or `GET /api/v1/sessions/<id>` to inspect metadata.
3. Use `get_session_messages` or `GET /api/v1/sessions/<id>/messages` only when the session is relevant.
4. Use `get_thread` for cross-session continuity.

This keeps retrieval cheap and avoids loading full transcripts unless needed.

## Notes

- The API is local-first and intended for localhost workflows.
- Search is backed by the generated history index.
- Some detail endpoints return richer data when live session loading succeeds.
- Query and identifier parameters are validated strictly; invalid values return `400`.
