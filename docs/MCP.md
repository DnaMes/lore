# MCP server

`lore-mcp` exposes the local Lore archive and the shared cross-tool memory store to any Model Context Protocol client over stdio.

## What it is

`lore-mcp` is the console entry point declared in `pyproject.toml` (`lore-mcp = "lore.cli.mcp:main_sync"`). It calls `create_server()` in `lore/interfaces/mcp.py`, which builds an `MCPServer` (transport in `lore/interfaces/server.py`) and registers the tool groups from `lore/interfaces/mcp_tools/`: `search.py`, `sessions.py`, `memory.py`, `summaries.py`.

The server speaks JSON-RPC 2.0 over stdin/stdout. Requests are read line by line, or framed with `Content-Length` headers; responses go to stdout and the loop ends when stdin closes. It is a stdio server, so it waits for input and prints nothing on its own: do not run it by hand to "see what it does".

Read versus write:

- `memory_write` and `memory_feedback` are the only tools that write to the shared memory store (the `memory` and `memory_feedback` tables in `<output-dir>/index_v2.sqlite`, default `~/.lore/index_v2.sqlite`).
- Every other tool reads. `switch_to_tool` is read-only for Lore but shells out to `lore-session switch`, which prepares a handoff for another tool. When the target is `gemini` or `codex` (they receive the context as a referenced file), the server needs the cloud opt-in `LORE_ALLOW_CLOUD=1` in its environment; the context is redacted before it is written or passed and kept in a private mode-0600 file under `~/.lore/handoff/`. `claude` and the manual tools (`cursor`, `vscode`) send nothing.
- `summarize_session` is the one exception outside the memory store: when enabled it can persist an AI summary row for a session.

## Tools

| name | purpose | writes |
|------|---------|--------|
| `search_history` | Search across all AI chat sessions. `scope` limits matches to a message-role subset: `user_only`, `assistant_only` or `tool_results`; `tag`, `start` and `end` keep only sessions with that saved tag/bookmark and inside the inclusive date range, applied before `limit`. Each hit carries the matching message as `evidence` (`message_seq`, `role`, `excerpt`, `excerpt_truncated`, `permalink`); the payload reports `total` and `total_is_partial`. | no |
| `list_sessions` | List AI chat sessions with optional filters (`tool`, `project`, `thread_id`, `tag`, `start`, `end`, `since`, `limit`, `include_automated`). An unparsable date is reported as an error, not silently ignored. | no |
| `get_session` | Get a single session by id, optionally with messages and user tags. | no |
| `get_session_messages` | Get live session messages by session id. | no |
| `list_recent_sessions` | List recently updated sessions. | no |
| `list_projects` | List projects present in the history index. | no |
| `get_thread` | Get a thread overview and optional messages. | no |
| `switch_to_tool` | Switch to another AI tool with context (runs `lore-session switch`). Requires the cloud opt-in `LORE_ALLOW_CLOUD=1` in the server environment when the target is `gemini` or `codex`; `claude` and the manual tools send nothing. | no |
| `memory_write` | Record a durable memory (fact, decision, todo, snippet, link, lesson, note) into the shared cross-tool store. The response lists near-duplicates in `similar`; pass `supersedes` with one of their ids to replace it. | yes (memory store) |
| `memory_recall` | Search the shared memory store for facts, decisions and lessons recorded earlier by any tool. Matches keywords and, when the embedding backend is installed, meaning; a query in double quotes is an exact phrase. `semantic=false` forces keywords only. | no |
| `memory_feedback` | Rate a confirmed memory from 1 (wrong or unhelpful) to 5 (right and useful). Candidates and rejected memories cannot be rated; one rating per rater and memory, a new rating replaces the old one. The weight is shown with the memory in `memory_recall`. | yes (memory store) |
| `get_session_summary` | Read the stored AI summary for a session (JSON data only, no transcript text). | no |
| `summarize_session` | Generate or refresh the AI summary for one session. Local Ollama only, and refused unless `LORE_MCP_ALLOW_SUMMARIZE=1`. Does not return summary text. | summary row, when enabled |

Verified via `tools/list` (the list is produced by the registrations in `lore/interfaces/mcp_tools/*.py`, not hand-maintained):

```text
['search_history', 'list_sessions', 'get_session', 'get_session_messages', 'list_recent_sessions', 'list_projects', 'get_thread', 'switch_to_tool', 'memory_write', 'memory_recall', 'memory_feedback', 'get_session_summary', 'summarize_session']
```

## The summarize_session gate

`summarize_session` is always advertised in `tools/list`, but the handler refuses to run unless `LORE_MCP_ALLOW_SUMMARIZE=1` is set in the server's environment. Without it the call returns:

```json
{"error": "summarize_session is disabled", "hint": "set LORE_MCP_ALLOW_SUMMARIZE=1"}
```

The provider is fixed to local Ollama (`MCP_SUMMARIZE_PROVIDER = "ollama"` in `lore/services/summarize_cli.py`), so untrusted session text is never sent to a cloud API:

- `LORE_SUMMARY_MODEL` selects the model; the default is `llama3.2`.
- `OLLAMA_HOST` must resolve to a loopback address, otherwise the call fails with a locality error.
- `LORE_SUMMARY_PROVIDER` is used by the web UI summarize path, not by the MCP tool.

The environment variable has to be set for the server process, not for the shell that registers it: pass it through the client (`-e` in Claude Code, `--env` in Codex) as shown under `## Client registration`.

## Finding the lore-mcp path

Install Lore first (see the installation section in `../README.md`); the entry point is created by that install.

```bash
command -v lore-mcp
# /home/<user>/.local/bin/lore-mcp
```

A pipx install links the binary into `~/.local/bin`, a `uv tool install` links it into `~/.local/share/uv/tools/<env>/bin`, and a virtualenv puts it in `<venv>/bin`. `readlink -f "$(command -v lore-mcp)"` shows the real target, which is the most robust value to put into a client configuration:

```bash
readlink -f "$(command -v lore-mcp)"
# /home/<user>/.local/share/uv/tools/lore/bin/lore-mcp
```

The examples below use `<path-to-lore-mcp>`; substitute the absolute path from `command -v` if the client does not inherit your shell `PATH`.

## Client registration

MCP clients each keep their own server list. The examples only show what to write; this page does not modify any client configuration.

### Claude Code

```bash
claude mcp add lore -- <path-to-lore-mcp>
```

Verified against the `claude mcp add` usage text (`claude --version` = 2.1.310), which documents `claude mcp add <name> -- <command> [args...]` for stdio servers. The command was not executed here, because it would write to the Claude Code configuration. Add `-e LORE_MCP_ALLOW_SUMMARIZE=1` to the same command to enable `summarize_session`.

### Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.lore]
command = "<path-to-lore-mcp>"
args = []
```

NOT VERIFIED against the current Codex version. The table name and the `command` / `args` keys match the entries already present in this machine's `~/.codex/config.toml` and the `codex mcp add` usage text (`codex-cli 0.164.0`), but no Lore entry was added or started. To let Codex write the entry itself:

```bash
codex mcp add lore -- <path-to-lore-mcp>
```

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "lore": {
      "command": "<path-to-lore-mcp>"
    }
  }
}
```

NOT VERIFIED against the current Cursor version. The `mcpServers` object with a per-server `command` matches this machine's existing `~/.cursor/mcp.json`, but no Lore entry was added and Cursor was not restarted against it.

## Troubleshooting

- **Empty results from `search_history`, `list_sessions` or `list_projects`.** There is no index yet for the tool you are asking about. Build it first:

  ```bash
  lore export --all          # every detected tool
  lore sync <tool>           # one tool, e.g. lore sync claude-code
  ```

  See `./CLI.md` for the full command set. `lore export --full` re-parses sources that look unchanged.
- **Memory tools return nothing.** The shared memory store is empty; `memory_write` has to run before `memory_recall` or `memory_feedback` can return anything. See `./MEMORY.md`.
- **The client shows no tools.** Check the command path with `command -v lore-mcp`, then use the absolute path in the client configuration. A stdio server started without a client that speaks JSON-RPC simply blocks on stdin.
- **Do not print to stdout.** Stdout carries protocol messages only; anything else corrupts the stream. This is why `lore/interfaces/server.py` and `lore/cli/mcp.py` contain no `print()` calls, and why logs go to stderr: the modules use `logging.getLogger(__name__)` without installing a stdout handler, so Python's default last-resort handler writes warnings and errors to stderr. Redirect or capture stderr separately when debugging.

## See also

- `./CLI.md` — command line reference (`export`, `sync`, `memory`, `summarize`)
- `./CONFIGURATION.md` — output directory, environment variables, provider settings
- `./MEMORY.md` — the shared cross-tool memory model behind `memory_write` and `memory_recall`
- `./API_REFERENCE.md` — tool inputs and outputs, plus the HTTP API
- `../SECURITY.md` — how to report a vulnerability
