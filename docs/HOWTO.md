# How-to

Short recipes. Reference: ./CLI.md. Setup: ./INSTALL.md.

## What Lore is

Lore is an archive and a shared agent memory in one local tool.

- The archive collects chat histories from the supported AI coding assistants into one searchable store under `~/.lore`, with no cloud connectivity.
- The shared memory is a cross-tool knowledge store that agents write to and recall from, by keyword or by meaning, with provenance, so a decision made in one tool is available to the next.

The name covers both halves: lore is accumulated knowledge as well as history and backstory. Which file is read for which tool is in `./PRIVACY.md`, the memory model in `./MEMORY.md`.

## Repository layout

The product is called Lore; the Python import package is `lore`.

- `lore/core`: data models and unified session logic.
- `lore/extractors`: one module per tool, parsing that tool's native format.
- `lore/exporters`: Markdown and context formatting.
- `lore/interfaces`: CLI, web UI and MCP entry points.

## Extractor support levels

The read location of every tool is listed in `./PRIVACY.md`. The extractors differ in how well they are backed:

- Antigravity support is experimental; its on-disk layout differs between releases.
- Built from the documented upstream schema and exercised against fixtures only, not verified on real machine data: Cline, Roo Code, Kilo Code, Goose, Crush, Kimi, Qwen Code, Zed, Factory Droid.
- Every other tool in that table is read without such a caveat.

For a fixture-only extractor, upstream format drift shows up as unrecognised records rather than as a hard error. `lore check --deep` reports exactly that.

## Inspecting extractors with lore check --deep

`lore check` is read-only and fast. It lists the tools found on the machine, prints each tool's data path and, for a tool that synced before, the counters of its last run: sources matched, files parsed, sessions, unknown records and skips.

```bash
lore check
lore check --deep
```

`lore check --deep` runs every available extractor to completion and prints, per tool, how many sources matched, how many sessions were parsed, which records were skipped and why, how many records the parser did not recognise, and how many files were parsed. It writes no archive and no index, and one failing extractor does not hide the other tools. A tool whose data directory exists but yields no session gets a warning, which points at a stale glob or a changed file format. The command exits `1` when an extractor raised.

## Weekly digest

```bash
lore digest --since 7d
lore digest --since 2w --format markdown
```

`--since` takes a window such as `7d`, `2w` or `1m` (default `7d`). The digest lists sessions by day, by tool and by top project. The default output is plain text; `--format markdown` is the form to paste into a note or a ticket. Add `--include-automated` to count exec, cron and subagent sessions as well.

## Session switching with lore-session

Output of `.venv/bin/python lore_session_cli.py --help`:

```text
usage: lore_session_cli.py [-h] {list,switch,continue} ...

AI Session Manager - Seamless tool switching

positional arguments:
  {list,switch,continue}
                        Command to execute
    list                List all sessions for current project
    switch              Switch to another AI tool
    continue            Auto-select best available tool

options:
  -h, --help            show this help message and exit
```

`list` shows the sessions recorded for the current project, grouped by tool.
`switch TOOL` saves a context block and starts that tool with it, and `continue` picks one of `gemini`, `codex`, `claude` by availability and does the same.

Typical use, when one tool has hit its usage limit:

```bash
lore-session switch gemini
```

The context block holds the last 15 messages of the project's recorded sessions, oldest first, each message truncated to 2000 characters. `--messages N` changes the count, and `--thread-id ID` continues one specific thread instead of the project's sessions. Secrets are redacted from the block first, and it is written to a unique owner-only file `~/.lore/handoff/context-<id>.md`. `gemini` and `codex` receive it as `@<that file>`, so the text never appears in the process list; both send it to the cloud, so they need `--allow-cloud` or `LORE_ALLOW_CLOUD=1` and are otherwise refused with exit code 2. The file is removed after the tool exits. `claude` is started with `--continue` and receives nothing from Lore. For a manual tool such as `cursor` or `vscode` the command keeps the file and prints how to paste it.

## Wiring memory recall into an agent session

`lore memory context --project <path> --budget 2000` renders the block an agent should read at session start; `./MEMORY.md` documents the ranking and the output formats. The wiring is per client: call that command from your own session-start hook and hand its stdout to the agent.

Earlier notes referred to the Claude Code and Kimi memory-recall hooks and to `LORE_RECALL` and `LORE_RECALL_BUDGET`. The Claude Code and Kimi memory-recall hooks are maintained in the ai-stack repository, not in this package. Those two variables do not exist in this repository, so do not rely on them. The budget lives in `--budget` (default 2000, a token estimate computed as characters divided by 4). Set `LORE_DEBUG=1` to print memory-context build failures to stderr.

## Restoring a rejected memory through the Python API

`lore memory dedupe --restore FILE` reverses a rejection recorded in an undo log, see `./MEMORY.md`. To restore one rejected memory without such a log:

```python
from pathlib import Path

from lore.storage import set_memory_status

output_dir = Path("~/.lore").expanduser()
set_memory_status(output_dir, 7, "confirmed", from_status="rejected")
```

Rejected rows are never deleted, so the call only flips the status. It returns `False` for an unknown id and raises `ValueError` when the memory is not currently rejected.

## Automated sessions

Sessions that were not a person talking to an assistant carry an origin of `exec`, `cron` or `subagent`. Lore archives them, so the archive stays complete, but leaves them out of the default views and out of the LLM stages.

| Surface | Include them with |
| --- | --- |
| `list`, `search`, `stats`, `digest`, `threads`, `rules`, `prune`, `generate-titles`, `analyze`, `knowledge`, `improve`, `summarize`, `format` | `--include-automated` |
| Web UI pages and `/api/v1/...` | `?automated=1`, or the "Automated" checkbox on the sessions list |
| MCP `list_sessions`, `search_history`, `list_recent_sessions`, `list_projects`, `get_thread` | argument `include_automated: true` |

- `lore export` writes every session, automated or not, and `lore prune` never removes an automated session unless `--include-automated` is given.
- Where a total is shown, the hidden count is printed as "12 automated sessions hidden". A thread counts its own hidden subagent children.
- Filtering the sessions list by a tag does not hide automated sessions.
- Token and cost totals (`/api/stats/costs`) always cover every session, because the spend happened either way; the payload carries `automated_included_line`.
- A session or thread opened by its id works whether or not it is hidden.
- Claude Code and Gemini CLI subagent sessions also record their parent in `parent_session_id`, with a `relation` of `subagent`, when the source path names it.
- The switches are parsed strictly: `true`, `1`, `yes`, `on`, and `false`, `0`, `no`, `off`, or a real boolean. Anything else is an error: an error string over MCP, HTTP 400 for the web UI and `/api/v1`. A repeated `?automated=` takes its first value.

## Re-extraction after a parser upgrade

Each extractor carries a `PARSER_VERSION`. When it rises, sessions indexed by the older version are parsed again once, even if the source file did not change. The first sync after such an upgrade therefore works in batches: one run re-parses at most `LORE_PARSER_REEXTRACT_BUDGET` stale sources per tool (default `150`) and later runs drain the rest.

- `0` re-parses no stale source in that run; changed sources are still parsed.
- A negative value removes the cap; an invalid value falls back to `150`.

Because the variable is read on every run, `LORE_PARSER_REEXTRACT_BUDGET=0` persists: subagent sessions stay untagged and visible until it is unset.

## Gemini project resolution

Gemini CLI records projects by hash. When that hash does not map back to a directory, list the roots it should search in `GEMINI_PROJECT_ROOTS`, separated by `:` on POSIX:

```bash
export GEMINI_PROJECT_ROOTS="$HOME/projects:$HOME/work:$HOME/code"
```

Those three roots and `$HOME` itself are already the built-in candidates, so the variable is only needed for projects outside them. Unsafe entries are ignored with a warning.

## Keyboard shortcuts in the web UI

The handlers ignore modified keys and events from inputs, textareas, selects and contenteditable fields, and they stay quiet while a modal dialog is open. Press `?` for this list inside the UI; the same dialog can disable the shortcuts.

| Page | Key | Action |
| --- | --- | --- |
| Sessions list | `j` / `k` | Move focus between rows |
| Sessions list | `Enter` | Open the focused session |
| Sessions list | `/` | Focus the tool filter (desktop, 1024px and wider) |
| Sessions list | `x` | Toggle the focused row (select mode) |
| Session detail | `j` / `k` | Previous / next prompt (`#msg-N`) |
| Session detail | `e` / `c` | Expand / collapse all tool groups |
| Session detail | `/` | Find in the session; `Prev` / `Next` step through the hits |
| Either | `?` | Keyboard shortcuts help |

On a session detail page, Find searches the full transcript across the lazy-loaded pages and highlights every hit in place.

## Select mode, bulk actions and mobile sheets

- Toggle **Select** on the sessions list, then tick rows or press `x` on the focused row, and use **Delete...** or **Tag...** in the bulk bar. Both confirm inside the page, with no native browser dialog. **Select all on page** and **Clear** sit next to them.
- Below 1024px the **Filters** panel of the sessions list and the **Prompts** panel of a session detail page open as bottom sheets with a focus trap; `Escape` closes an open sheet.

## Response headers and tracing

- Every response carries `X-Request-ID` for correlation; a client-sent value is reused, otherwise one is generated.
- API responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`; the budgets themselves are in `./CONFIGURATION.md`.
- Security headers set a strict `Content-Security-Policy`, `X-Frame-Options: DENY` and `Referrer-Policy: strict-origin-when-cross-origin`.
- With `LORE_JSON_REQUEST_LOGS=true`, each request is logged as one JSON object with `event`, `method`, `path`, `status`, `duration_ms` and `request_id`.

## Probe result codes

`lore-web-probe` exits `0` when every probe passed, `1` on probe failures, and `3` when an upstream gateway blocked the run with its own authentication, for example a `401` from a reverse proxy. `2` means the arguments were refused, such as `--user` without a usable password. Besides the per-probe lines it prints a `build_info` line read from `/api/build-info`, so a deployed revision can be compared with the checkout. The query-parameter hardening probes send URL-encoded metacharacters such as `%3B`, so proxy normalisation cannot mask application-level validation.

## Exporting a single session from the web UI

`/export/<session_id>` serves the linked Markdown from the index. When the session is indexed but its Markdown file is missing, the file is regenerated from the live session for that tool. An unknown id returns `404` without scanning every source, unless `LORE_EXPORT_FALLBACK_SCAN=true` is set.

## Validating the compose file

`docker compose config` parses `docker-compose.yml`, resolves the environment and prints the canonical service definition without starting anything. `./DOCKER.md` covers the mounts, the healthcheck and the container user.

```bash
docker compose config
```

## Vendored front-end assets

The web UI ships highlight.js 11.9.0 and Tailwind 3.4.16 in `lore/interfaces/static/`. There is no CDN reference, so the dashboard renders fully offline.

## Removed features

Warp support and ai-history compatibility were removed in 2.4.0; legacy Warp rows stay readable.

## License

MIT, see `../LICENSE`.
