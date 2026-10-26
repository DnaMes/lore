# Command reference

`lore` (version 2.4.0) is a local-first archive and shared agent memory for your
AI coding sessions. This page lists every command, what it is for, whether it
reads or writes your data, and the exact output of its `--help` flag.

Install with the steps in [INSTALL.md](./INSTALL.md). From a clone without
installing, run the commands through the `python3 lore_cli.py <command>` entry
point instead of the `lore` console script; the arguments are identical.

Related pages: [CONFIGURATION.md](./CONFIGURATION.md) for settings and
environment variables, [MEMORY.md](./MEMORY.md) for the shared memory store,
[PRIVACY.md](./PRIVACY.md) for what leaves the machine, and the
[README](../README.md) for the overview.

## Global options

Output of `.venv/bin/python lore_cli.py --help`:

```text
usage: lore [-h] [-v] [--version] [--output-dir OUTPUT_DIR]
            {list,export,export-html,search,stats,digest,memory,watch,check,threads,rules,prune,sync,run,generate-titles,reindex,analyze,knowledge,improve,summarize,format}
            ...

Lore — local-first archive and shared agent memory for your AI coding sessions

positional arguments:
  {list,export,export-html,search,stats,digest,memory,watch,check,threads,rules,prune,sync,run,generate-titles,reindex,analyze,knowledge,improve,summarize,format}
                        Commands
    list                List all sessions
    export              Export sessions to Markdown
    export-html         Export one session as a standalone HTML file
    search              Search sessions
    stats               Show statistics
    digest              Print an activity digest for a recent time window
    memory              Manage the shared cross-tool memory store
    watch               Watch for new sessions
    check               Check tool availability
    threads             List threads
    rules               Generate derived rules
    prune               Prune sessions by prompt count
    sync                Sync sessions for a tool
    run                 Run a tool and sync after it exits
    generate-titles     Generate readable titles for sessions
    reindex             Rebuild index from all sources on disk (fixes missing
                        sessions after sync)
    analyze             Generate AI-powered statistics and insights
    knowledge           Extract knowledge from sessions using LLM
    improve             Backfill embeddings, extract knowledge, and propose
                        memory candidates
    summarize           Generate AI session summaries for archived sessions
    format              Format sessions with AI-generated summaries and tags

options:
  -h, --help            show this help message and exit
  -v, --verbose         Verbose output
  --version             Show the lore version and exit
  --output-dir OUTPUT_DIR
                        Output directory (default: ~/.lore)

Examples:
  lore list                      List all sessions
  lore list --tool claude-code   List Claude Code sessions only
  lore export --all              Export all sessions to ~/.lore
  lore search "docker"           Search for "docker" in all sessions
  lore watch --git               Watch for new sessions and auto-commit
  lore check                     Check availability of AI tools
        
```
Most commands list and process sessions that were created by automated runs
(exec, cron, subagent) and hide them by default; pass `--include-automated` to
include them. The `--help` output of every command that supports the flag
repeats it, so it is not explained again below.

The cloud opt-in can also be set with `LORE_ALLOW_CLOUD=1` (see [CONFIGURATION.md](./CONFIGURATION.md)).

## list

List archived sessions with optional filters.

Mode: read-only

```text
usage: lore list [-h] [--tool TOOL] [--project PROJECT] [--since SINCE]
                 [--format {table,json}] [--limit LIMIT] [--offset OFFSET]
                 [--include-automated]

options:
  -h, --help            show this help message and exit
  --tool TOOL           Filter by tool
  --project PROJECT     Filter by project path
  --since SINCE         Filter by date (e.g., 7d, 2w)
  --format {table,json}
  --limit LIMIT         Limit number of results
  --offset OFFSET       Offset for pagination
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

## export

Export sessions to Markdown files and rebuild the index.

Writes: Markdown exports and the index under the output directory (default `~/.lore`).

After the run, stderr reports how many sessions were skipped and why (for example, sessions below the prompt floor). An index entry whose source file was deleted is kept, unless its exported file is also gone.

```text
usage: lore export [-h] [--all] [--tool TOOL] [--project PROJECT] [--full]
                   [--redact]

options:
  -h, --help         show this help message and exit
  --all              Export all sessions
  --tool TOOL        Export only specific tool
  --project PROJECT  Export only specific project
  --full             Re-parse every source, even those unchanged since the
                     last export
  --redact           Redact secret-shaped strings (requires a non-default
                     --output-dir)
```

## export-html

Export one session as a standalone HTML file.

Writes: the file given by `--output`, or `<session-id>.html` in the current directory (created with mode 0600).

```text
usage: lore export-html [-h] [--output OUTPUT] [--redact] session_id

positional arguments:
  session_id       Session ID (full or unique prefix)

options:
  -h, --help       show this help message and exit
  --output OUTPUT  Output file or directory (default: <session-id>.html in
                   cwd)
  --redact         Redact secret-shaped strings before rendering HTML
```

## search

Full-text search across archived sessions.

Mode: read-only

Prints `Found N result` (singular for one hit) with each score to three decimals. With no index yet it says to run `lore export --all` first.

```text
usage: lore search [-h] [--tool TOOL] [--project PROJECT]
                   [--include-automated]
                   query

positional arguments:
  query                Search query

options:
  -h, --help           show this help message and exit
  --tool TOOL          Search only specific tool
  --project PROJECT    Search only specific project
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

## stats

Print counts and breakdowns from the existing index.

Mode: read-only

```text
usage: lore stats [-h] [--include-automated]

options:
  -h, --help           show this help message and exit
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

## digest

Print an activity digest for a recent time window.

Mode: read-only

```text
usage: lore digest [-h] [--since SINCE] [--format {text,markdown}]
                   [--include-automated]

options:
  -h, --help            show this help message and exit
  --since SINCE         Time window to summarize (e.g., 7d, 2w, 1m; default:
                        7d)
  --format {text,markdown}
                        Output format (default: text)
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

## memory

Manage the shared cross-tool memory store.

Writes: the memory store in the output directory (SQLite `index_v2.sqlite`); each subcommand below states its own write behaviour.

```text
usage: lore memory [-h]
                   {add,list,search,dedupe,import-claude,show,context,rate}
                   ...

positional arguments:
  {add,list,search,dedupe,import-claude,show,context,rate}
    add                 Record a new memory
    list                List recorded memories
    search              Search memories
    dedupe              Reject exact confirmed memory duplicates (dry run
                        unless --apply)
    import-claude       Import Claude Code auto-memory files (dry run unless
                        --apply)
    show                Show one memory in full
    context             Render a budgeted memory block for agent context
    rate                Rate a memory from 1 to 5

options:
  -h, --help            show this help message and exit
```

The subcommands are documented below. Run them as `lore memory <sub>` (or `python3 lore_cli.py memory <sub>` from a clone).

### add

Record a new memory.

Writes: one memory record in the store.

```text
usage: lore memory add [-h] [--kind KIND] --title TITLE --body BODY
                       [--project PROJECT] [--author AUTHOR] [--tag TAG]
                       [--from-session FROM_SESSION] [--supersedes ID]

options:
  -h, --help            show this help message and exit
  --kind KIND           fact|decision|todo|snippet|link|lesson|note (default:
                        note)
  --title TITLE         short memory title
  --body BODY           the memory content
  --project PROJECT     optional project scope
  --author AUTHOR       who recorded it (default: human)
  --tag TAG             tag (repeatable: --tag db --tag infra)
  --from-session FROM_SESSION
                        id of the session this memory came from (provenance)
  --supersedes ID       id of an active, confirmed memory (same project) that
                        this one replaces
```

### list

List recorded memories.

Mode: read-only

```text
usage: lore memory list [-h] [--kind KIND] [--project PROJECT] [--all]
                        [--limit LIMIT]

options:
  -h, --help         show this help message and exit
  --kind KIND        filter by kind
  --project PROJECT  filter by project scope
  --all              include superseded memories
  --limit LIMIT      max results
```

### search

Search memories by keyword and, when the semantic extra is installed, by meaning.

Mode: read-only

```text
usage: lore memory search [-h] [--kind KIND] [--project PROJECT]
                          [--limit LIMIT] [--semantic]
                          query

positional arguments:
  query              search query

options:
  -h, --help         show this help message and exit
  --kind KIND        filter by kind
  --project PROJECT  filter by project scope
  --limit LIMIT      max results
  --semantic         rank by meaning only (default: keywords plus meaning when
                     the 'semantic' extra is installed)
```

### dedupe

Reject exact confirmed duplicates.

Writes: memory status changes and an undo log, only with `--apply` (dry run by default). Duplicates must match on title, body, kind and scope, so memories are never merged across kinds or scopes. The undo log records its store, and `--restore` refuses a log written for a different store.

```text
usage: lore memory dedupe [-h] [--apply] [--restore FILE]

options:
  -h, --help      show this help message and exit
  --apply         mark duplicates rejected (default: dry run)
  --restore FILE  restore rejections from a dedupe undo log written by --apply
```

### import-claude

Import Claude Code auto-memory files.

Writes: memory records, only with `--apply` (dry run by default).

```text
usage: lore memory import-claude [-h] [--apply] [--root ROOT]

Seed the memory store from ~/.claude/projects/<slug>/memory/*.md.

Type mapping: user -> fact, feedback -> lesson, project -> fact, reference -> link.
Files with a missing or unknown type, or no usable name/description, are
imported as candidates (kind note). MEMORY.md indexes, files without
frontmatter and empty bodies are skipped.

Re-running writes nothing for unchanged files; an edited file is written as
a new memory that supersedes the previous import. Titles and bodies are
redacted before writing.

options:
  -h, --help   show this help message and exit
  --apply      write to the store (default: dry run)
  --root ROOT  Claude projects directory (default: ~/.claude/projects)
```

### show

Show one memory in full.

Mode: read-only

```text
usage: lore memory show [-h] memory_id

positional arguments:
  memory_id   memory id

options:
  -h, --help  show this help message and exit
```

### context

Render a budgeted memory block for agent context.

Mode: read-only

```text
usage: lore memory context [-h] [--project PROJECT] [--budget BUDGET]
                           [--tool TOOL] [--format {text,json}]
                           [--limit LIMIT]

options:
  -h, --help            show this help message and exit
  --project PROJECT     project scope (default: current working directory,
                        resolved)
  --budget BUDGET       token budget estimate (ceil(chars/4); default: 2000)
  --tool TOOL           optional tool scope filter
  --format {text,json}  output format (default: text)
  --limit LIMIT         max memories to consider
```

### rate

Rate a memory from 1 to 5.

Writes: the rating for that memory.

```text
usage: lore memory rate [-h] [--rater RATER] memory_id score

positional arguments:
  memory_id      memory id
  score          1 (wrong) to 5 (right and useful)

options:
  -h, --help     show this help message and exit
  --rater RATER  who rates (default: human)
```

## watch

Poll for new sessions and export them as they appear.

Writes: new Markdown exports and the index on every poll.

```text
usage: lore watch [-h] [--interval INTERVAL] [--git]

options:
  -h, --help           show this help message and exit
  --interval INTERVAL  Poll interval in seconds (default: 60)
  --git                Auto-commit new exports to git
```

## check

Report which AI tool extractors are available on this machine.

Mode: read-only

```text
usage: lore check [-h] [--deep]

options:
  -h, --help  show this help message and exit
  --deep      Run every available extractor (writes nothing) and report
              matched sources, sessions, skips and unknown records
```

## threads

List open threads collected from session summaries.

Mode: read-only

```text
usage: lore threads [-h] [--include-automated]

options:
  -h, --help           show this help message and exit
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

## rules

Extract reusable rules from session prompts.

Writes: `rules.md` in the output directory.

```text
usage: lore rules [-h] [--limit LIMIT] [--include-automated]

options:
  -h, --help           show this help message and exit
  --limit LIMIT        Max rules to extract
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

## prune

Delete sessions whose prompt count is at or below a threshold.

Writes: marks sessions as ignored in `ignored.json` and deletes their exports (`--dry-run` only prints the selection). Deletion is confined to the output directory: an export path that resolves outside it (for example through a symlink) is skipped with a warning. Ignored ids are also hidden from the v2 store views; the rows are kept.

```text
usage: lore prune [-h] [--max-prompts MAX_PROMPTS] [--dry-run]
                  [--include-automated]

options:
  -h, --help            show this help message and exit
  --max-prompts MAX_PROMPTS
                        Delete sessions with this many prompts or fewer
  --dry-run             Show what would be pruned
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

## sync

Sync sessions for a single tool.

Writes: Markdown exports and index entries for that tool. Like `export`, it prints skipped-session counts to stderr after the run.

```text
usage: lore sync [-h] [--session-id SESSION_ID] [--project PROJECT] tool

positional arguments:
  tool                  Tool to sync (e.g., codex, claude-code)

options:
  -h, --help            show this help message and exit
  --session-id SESSION_ID
                        Sync only a specific session ID
  --project PROJECT     Sync only a specific project
```

## run

Run a tool and sync its sessions after it exits.

Writes: session exports and index entries once the child tool exits.

```text
usage: lore run [-h] [--no-pty] tool ...

positional arguments:
  tool        Tool to run (e.g., codex, claude-code)
  tool_args   Args passed to the tool

options:
  -h, --help  show this help message and exit
  --no-pty    Disable PTY capture
```

## generate-titles

Generate readable titles for sessions.

Writes: the title cache `~/.lore/title_cache/titles.json`. Calls a provider: local by default; `gemini-cli` (cloud) only when selected with `--provider`. Choosing it is the opt-in: no `--allow-cloud` gate applies.

```text
usage: lore generate-titles [-h] [--strategy {fast,keyword,smart,auto}]
                            [--tool TOOL] [--since SINCE] [--force]
                            [--provider {local,gemini-cli}] [--model MODEL]
                            [--include-automated]

options:
  -h, --help            show this help message and exit
  --strategy {fast,keyword,smart,auto}
                        Title generation strategy (default: auto)
  --tool TOOL           Only generate for specific tool
  --since SINCE         Only sessions since (e.g., 7d, 2w)
  --force               Regenerate even if cached
  --provider {local,gemini-cli}
                        Provider for LLM titles (default: local). 'gemini-cli'
                        is a cloud provider and is only used when selected
                        here — titles send a transcript excerpt.
  --model MODEL         Model for the selected provider (provider default
                        otherwise)
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

Note: Titles use `--provider local` by default and stay on this machine. `gemini-cli` is a cloud provider; it is only used when you choose it, and it receives a transcript excerpt per title.

## reindex

Rebuild the index from every source on disk, which fixes sessions missing after a sync.

Writes: `index.json` and the SQLite index under the output directory.

```text
usage: lore reindex [-h]

options:
  -h, --help  show this help message and exit
```

## analyze

Generate AI-powered statistics and insights.

Writes: `stats/stats.json` in the output directory. Calls a provider unless `--no-llm` is given. The default provider is gemini, a cloud provider, so it needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1` (otherwise exit 2, nothing sent).

```text
usage: lore analyze [-h] [--provider {gemini,jev,ollama,openrouter}]
                    [--model MODEL] [--allow-cloud] [--no-llm]
                    [--output OUTPUT] [--include-automated]

options:
  -h, --help            show this help message and exit
  --provider {gemini,jev,ollama,openrouter}
                        LLM provider to use (default: gemini)
  --model MODEL         Model name (default depends on --provider)
  --allow-cloud         Allow cloud providers to receive session text (or set
                        LORE_ALLOW_CLOUD=1)
  --no-llm              Generate basic stats without LLM
  --output OUTPUT       Output file for stats (default:
                        ~/.lore/stats/stats.json)
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

Note: Use `--no-llm` to skip the provider entirely, or `--provider ollama` (local) to keep text on this machine. Any other provider is cloud and needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`.

## knowledge

Extract knowledge entries from sessions with an LLM.

Writes: `knowledge/knowledge_base.json` in the output directory. Calls a provider only when `--provider` is given. There is no default provider: a bare `lore knowledge` exits 2 and sends nothing.

```text
usage: lore knowledge [-h] [--provider {gemini,jev,ollama,openrouter}]
                      [--model MODEL] [--allow-cloud] [--limit LIMIT]
                      [--tool TOOL] [--output OUTPUT] [--include-automated]

options:
  -h, --help            show this help message and exit
  --provider {gemini,jev,ollama,openrouter}
                        LLM provider (required; no default — bare lore
                        knowledge sends nothing). Use ollama for local, or a
                        cloud provider with --allow-cloud
  --model MODEL         Model name (default depends on --provider)
  --allow-cloud         Allow cloud providers to receive session text (or set
                        LORE_ALLOW_CLOUD=1)
  --limit LIMIT         Max sessions to process (default: 50)
  --tool TOOL           Only process sessions from specific tool
  --output OUTPUT       Output file (default:
                        ~/.lore/knowledge/knowledge_base.json)
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

Note: `--provider` is required. Use `--provider ollama` (local), or a cloud provider with `--allow-cloud` or `LORE_ALLOW_CLOUD=1`; a cloud choice without opt-in exits 2.

## improve

Backfill embeddings, extract knowledge and propose memory candidates.

Writes: memory embeddings, knowledge entries and lesson candidates in the output directory. Calls a provider: only the extract_knowledge stage, and only when `--provider` or `LORE_LLM_PROVIDER` is set (`--dry-run` calls nothing). Unlike `analyze`, `knowledge`, `format` and `summarize`, `improve` has no `--allow-cloud` gate: a cloud provider chosen here is used as given, and a notice on stderr names it before the run.

```text
usage: lore improve [-h] [--provider PROVIDER] [--model MODEL] [--limit LIMIT]
                    [--stage {embed_memories,extract_knowledge,distill_lessons,from_summaries}]
                    [--dry-run] [--json] [--include-automated]

options:
  -h, --help            show this help message and exit
  --provider PROVIDER   LLM provider for extract_knowledge
  --model MODEL         Model name for extract_knowledge
  --limit LIMIT         Max sessions for extract_knowledge (default: 50)
  --stage {embed_memories,extract_knowledge,distill_lessons,from_summaries}
                        Run only these stages (repeatable)
  --dry-run             Compute candidate counts without writing or calling an
                        LLM
  --json                Print stage report as JSON
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

Note: Without `--provider` or `LORE_LLM_PROVIDER` the extract stage is skipped, so no conversation text leaves the machine.

## summarize

Generate AI session summaries for archived sessions.

Writes: summaries for the selected sessions. Calls a provider: ollama by default, which is local.

```text
usage: lore summarize [-h] [--session SESSION] [--project PROJECT]
                      [--tool TOOL] [--since SINCE]
                      [--provider {gemini,jev,ollama,openrouter}]
                      [--model MODEL] [--allow-cloud] [--dry-run] [--force]
                      [--max-sessions MAX_SESSIONS]
                      [--max-input-tokens MAX_INPUT_TOKENS]
                      [--max-session-tokens MAX_SESSION_TOKENS]
                      [--level {session,day,project}] [--json]
                      [--include-automated]

options:
  -h, --help            show this help message and exit
  --session SESSION     Session ID (full or unique prefix)
  --project PROJECT     Only sessions for this project path
  --tool TOOL           Only sessions from this tool
  --since SINCE         Only sessions active since this duration (default 30d
                        when --session is omitted)
  --provider {gemini,jev,ollama,openrouter}
                        LLM provider (default: ollama, local)
  --model MODEL         Model name (default depends on --provider)
  --allow-cloud         Allow cloud providers to receive transcript text (or
                        set LORE_ALLOW_CLOUD=1)
  --dry-run             Estimate tokens and calls without contacting a
                        provider
  --force               Re-summarize even when the fingerprint is unchanged
  --max-sessions MAX_SESSIONS
                        Maximum number of sessions to consider
  --max-input-tokens MAX_INPUT_TOKENS
                        Stop after this many estimated input tokens (real run)
  --max-session-tokens MAX_SESSION_TOKENS
                        Skip sessions whose compressed transcript exceeds this
                        token budget
  --level {session,day,project}
                        Summary granularity (only session is implemented)
  --json                Print machine-readable JSON on stdout
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

Note: Default provider is `ollama` (local). `gemini`, `openrouter` and `jev` need `--allow-cloud` or `LORE_ALLOW_CLOUD=1`. `--dry-run` estimates tokens and calls without contacting a provider.

## format

Format sessions with AI-generated summaries and tags.

Writes: formatted files under `formatted/` in the output directory. Calls a provider: gemini by default, a cloud provider, so it needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1` (otherwise exit 2, nothing sent).

```text
usage: lore format [-h] [--provider {gemini,jev,ollama,openrouter}]
                   [--model MODEL] [--allow-cloud] [--session-id SESSION_ID]
                   [--include-automated] [--tool TOOL] [--limit LIMIT]
                   [--output-dir OUTPUT_DIR]

options:
  -h, --help            show this help message and exit
  --provider {gemini,jev,ollama,openrouter}
                        LLM provider to use (default: gemini)
  --model MODEL         Model name (default depends on --provider)
  --allow-cloud         Allow cloud providers to receive session text (or set
                        LORE_ALLOW_CLOUD=1)
  --session-id SESSION_ID
                        Format specific session by ID
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
  --tool TOOL           Format sessions from specific tool
  --limit LIMIT         Max sessions to format (default: 10)
  --output-dir OUTPUT_DIR
                        Output directory (default: ~/.lore/formatted)
```

Note: `format` has no `--no-llm` flag. Use `--provider ollama` (local) to keep text on this machine; any cloud provider needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`.

## lore-session

Switch the active AI tool for the current project and continue where the last
session stopped.

Writes: the shared context file `/tmp/ai-session-context.md`.

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

## lore-web

Serve the web UI. See [WEB-UI.md](./WEB-UI.md) for routes, deployment and
access control.

Writes: nothing on start; its POST endpoints can rebuild caches, cancel jobs and
generate summaries.

```text
usage: lore-web [-h] [--host HOST] [--port PORT] [--debug]

Serve the Lore web UI (Flask dev server). For production use gunicorn.

options:
  -h, --help   show this help message and exit
  --host HOST  Bind host (default 127.0.0.1, or $LORE_WEB_HOST).
  --port PORT  Bind port (default 5000, or $LORE_WEB_PORT).
  --debug      Run Flask in debug mode (or set $LORE_WEB_DEBUG=true).
```

## lore-mcp

Stdio MCP server, so there is no `--help` output: it speaks JSON-RPC on
stdin/stdout and is started by the MCP client. See [MCP.md](./MCP.md) for the
tool list and client configuration.

Writes: the shared memory store through the `memory_write` and `memory_feedback`
tools; the search, session and summary tools only read it.

## lore-web-probe

Security probe matrix against a base URL, to check the deployed web UI's
authentication and exposure.

Writes: nothing; it only sends requests to `--base-url`.

```text
usage: lore-web-probe [-h] --base-url BASE_URL [--user USER]
                      [--password PASSWORD] [--password-env PASSWORD_ENV]
                      [--timeout-seconds TIMEOUT_SECONDS]
                      [--timeout-retries TIMEOUT_RETRIES] [--json]

Run lore web security probe matrix against a base URL.

options:
  -h, --help            show this help message and exit
  --base-url BASE_URL   Base URL to probe
  --user USER           Basic auth username
  --password PASSWORD   Basic auth password
  --password-env PASSWORD_ENV
                        Env var name for basic auth password
  --timeout-seconds TIMEOUT_SECONDS
                        Per-request timeout in seconds
  --timeout-retries TIMEOUT_RETRIES
                        Retries to perform after a timeout
  --json                Print JSON output instead of line-by-line text
```

## Exit codes

The CLI returns three codes:

- `0`: success. This includes runs that simply matched nothing, such as a search
  with no hits.
- `1`: an error. Examples are a missing index, an unknown or ambiguous session
  id, an unavailable provider, or a failed write.
- `2`: a non-local provider was chosen without opt-in. `_cloud_optin_error` in
  `lore_cli.py` (via `require_cloud_opt_in` in `lore/services/summarize_cli.py`)
  returns `2` for `gemini`, `openrouter`, `jev`, or an off-host `ollama`
  without `--allow-cloud` or `LORE_ALLOW_CLOUD=1`; nothing is sent and nothing
  is written.

Automation can therefore treat any non-zero code as failure and distinguish the
refused cloud opt-in from other errors by the value `2`.

The other entry points have their own codes. `lore-web-probe` returns `2` when
`--user` is given without a usable password and `3` when the probe matrix was
blocked; `lore-web` and `lore-mcp` are long-running servers, so their exit code
reflects shutdown rather than a command result.
