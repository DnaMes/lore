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
            {list,export,export-html,search,stats,digest,memory,watch,check,threads,rules,prune,backup,restore,bundle,sync,run,generate-titles,reindex,analyze,knowledge,improve,summarize,format}
            ...

Lore — local-first archive and shared agent memory for your AI coding sessions

positional arguments:
  {list,export,export-html,search,stats,digest,memory,watch,check,threads,rules,prune,backup,restore,bundle,sync,run,generate-titles,reindex,analyze,knowledge,improve,summarize,format}
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
    backup              Write a versioned, consistent backup bundle
    restore             Restore the v2 store from a backup bundle
    bundle              Export or import a portable archive bundle
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

List the sessions found in the live tool directories, with optional filters.

`lore list` reads the tool source directories, not the archive index: when a
session's source file is deleted (for example by the tool's retention setting),
the session disappears from `lore list`, but it stays in `lore search`,
`lore stats` and the web UI, because those read the archive. With
`--format json`, stdout carries only the JSON array; totals and paging hints
go to stderr.

Mode: read-only

```text
usage: lore list [-h] [--tool TOOL] [--project PROJECT] [--tag TAG]
                 [--since SINCE] [--format {table,json}] [--limit LIMIT]
                 [--offset OFFSET] [--include-automated]

options:
  -h, --help            show this help message and exit
  --tool TOOL           Filter by tool
  --project PROJECT     Filter by project path
  --tag TAG             Filter by a saved tag/bookmark (session_tags) or an
                        index keyword
  --format {table,json}
  --limit LIMIT         Limit number of results
  --offset OFFSET       Offset for pagination
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

## export

Export sessions to Markdown files and rebuild the index.

Writes: Markdown exports and the index under the output directory (default `~/.lore`).

What an export contains: every canonical message in order — empty, system, failed and tool
messages included — with each tool call rendered from the canonical envelope
(`lore/exporters/tool_calls.py`): call name and id, pretty-printed JSON arguments (or the raw
text with a "not valid JSON" note), the result or error, status, duration and a truncation or
"n events dropped" label. An unmatched tool result stays visible as its own block. Message text
is written verbatim, so fenced code keeps its exact whitespace and line endings. Titles and
summaries are derived values: the export labels their source (`title_source`) and marks them
"not transcript content" instead of letting them stand in for the transcript. No export step
calls a model, so it works with every provider unavailable.

After the run, stderr reports how many sessions were skipped and why (for example, sessions below the prompt floor). Archive preservation drops nothing automatically: an index entry is kept even when both its source file and its exported Markdown file are gone. Only `lore prune` removes entries, and it records the pruned ids in the v2 store (`ignored_sessions`, migration 27) and mirrors them to `ignored.json`. This is deliberate: an unreadable, unmounted or temporarily missing source must never cost an index row, so the index can contain rows whose source no longer exists — that is the archive.

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

The page renders full Markdown — headings, lists, tables, blockquotes, inline code, emphasis and links — with the same thread-local pipeline the web UI uses, then sanitises the result with the shared allowlist (`lore/utils/html_sanitize.py`): scripts, event handlers, `javascript:` URLs and tags outside the list are removed, while their text stays. The stylesheet is inline and no remote asset is referenced, so the file opens correctly offline.

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
usage: lore search [-h] [--tool TOOL] [--project PROJECT] [--tag TAG]
                   [--page PAGE] [--messages] [--limit LIMIT]
                   [--include-automated]
                   query

positional arguments:
  query                Search query

options:
  -h, --help           show this help message and exit
  --tool TOOL          Search only specific tool
  --project PROJECT    Search only specific project
  --tag TAG            Search only sessions with this saved tag/bookmark
  --page PAGE          Page of results (20 per page; default 1)
  --messages           Return matching messages (passage, evidence, permalink)
                       instead of sessions
  --limit LIMIT        Max message hits with --messages (default: 10)
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

`--messages` drills into the transcript: each hit is one archived message with
its `seq`, an excerpt (`…` marks a shortened passage), the stable evidence
identity, and the `/session/<id>/m/<seq>` permalink. The header names the path
that produced the hits — `vector` when the optional `lore[semantic]` extra has
populated message vectors, `fts` for the keyword fallback over message bodies.
`lore embed-messages` populates the vector path; without it (or without the
extra) message search still works, by keyword, and says so.

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

## metrics

Report evidence-backed workflow metrics. Every metric is labelled with its class
(`observed`, `computed` or `inferred`), the source fields it reads and its
coverage (`covered/total` sessions); a metric whose source field is absent
prints `n/a` with a `no data:` caveat instead of a zero. See
[WORKFLOW-ANALYSIS.md](WORKFLOW-ANALYSIS.md) for the full definitions.

Mode: read-only and local (extracts sessions, writes nothing, needs no index).

```text
usage: lore metrics [-h] [--json] [--include-inferred] [--include-automated]

options:
  -h, --help           show this help message and exit
  --json               Machine-readable output
  --include-inferred   Also report inferred outcomes (heuristic, labelled,
                       never mixed in)
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

## memory

Manage the shared cross-tool memory store.

Writes: the memory store in the output directory (SQLite `index_v2.sqlite`); each subcommand below states its own write behaviour.

```text
usage: lore memory [-h]
                   {add,list,search,dedupe,import-claude,show,context,review,repair-index,rate}
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
    review              Review candidates: list them, or accept/reject by id
    repair-index        Detect and rebuild missing memory search rows (dry run
                        by default)
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
                        [--status {confirmed,candidate,rejected}] [--json]

options:
  -h, --help            show this help message and exit
  --kind KIND           filter by kind
  --project PROJECT     filter by project scope
  --all                 include superseded memories
  --limit LIMIT         max results
  --status {confirmed,candidate,rejected}
                        filter by review status (default: confirmed)
  --json                full audit export: claim, evidence and sources
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

### review

Review candidates: list them, or accept/reject by id.

Without a target, lists the memory candidates waiting for review. With
`accept` or `reject` and one or more ids, sets their status to `confirmed`
or `rejected`; ids that are not candidates are reported as unchanged.

Writes: memory status changes, only with `accept` or `reject` (listing is
read-only).

```text
usage: lore memory review [-h] [--kind KIND] [--json]
                          [{accept,reject}] [ids ...]

positional arguments:
  {accept,reject}  omit to list the candidates waiting for review
  ids              candidate ids to accept or reject

options:
  -h, --help       show this help message and exit
  --kind KIND      filter the listing by kind
  --json           print the result as JSON
```

### repair-index

Detect and rebuild missing memory search rows.

Writes: search-index rows, only with `--apply` (dry run by default).

```text
usage: lore memory repair-index [-h] [--apply] [--json]

options:
  -h, --help  show this help message and exit
  --apply     write the repair instead of only reporting it
  --json      print the report as JSON
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

## sources

Report per-source archive health (captured, filtered, unsupported, failed).

Every tracked source is shown with its state, tool and path, plus a summary
per state; `--state` narrows the report to one state.

Mode: read-only

```text
usage: lore sources [-h]
                    [--state {captured,parsed,filtered,unsupported,failed,unavailable,deleted}]
                    [--limit LIMIT] [--json]

options:
  -h, --help            show this help message and exit
  --state {captured,parsed,filtered,unsupported,failed,unavailable,deleted}
                        Only show sources in this state
  --limit LIMIT         Maximum per-source rows to print (default: 200)
  --json                Machine-readable output
```

## jobs

List or resume durable analysis jobs (the DB is the source of truth).

Mode: read-only; the `resume` subcommand requeues unfinished chunks of an
interrupted job (complete ones are skipped) and reports what is left — it
does not run the work itself.

```text
usage: lore jobs [-h] [--json] [--kind KIND]
                 [--state {pending,running,completed,failed,timed_out,cancelled}]
                 [--limit LIMIT]
                 {resume} ...

positional arguments:
  {resume}
    resume              Requeue unfinished chunks of an interrupted job

options:
  -h, --help            show this help message and exit
  --json                Machine-readable output
  --kind KIND           Only show jobs of this kind
  --state {pending,running,completed,failed,timed_out,cancelled}
                        Only show jobs in this state
  --limit LIMIT         Maximum jobs to print (default: 50)
```

### resume

Requeue unfinished chunks of an interrupted job.

Writes: job and chunk states in the store (requeued chunks return to `pending`).

```text
usage: lore jobs resume [-h] [--json] job_id

positional arguments:
  job_id      Job id (e.g. reload-abc123 or summary-abc123)

options:
  -h, --help  show this help message and exit
  --json      Machine-readable output
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

Extract reusable rules from session prompts, or manage the evidence-backed
harness recommendations built from them.

Mode: bare `lore rules` writes `rules.md` (read-only otherwise) and reads no
other file. `rules export` / `rules activate` / `rules evaluate` write
`recommendations.json`; `rules review` is read-only. Nothing in this command
modifies a harness configuration — recommendations stay inactive until a human
activates them, and even activation only flips a stored flag. See
[RECOMMENDATIONS.md](RECOMMENDATIONS.md) for the field-by-field contract.

```text
usage: lore rules [-h] [--limit LIMIT] [--include-automated]
                  {export,activate,evaluate,review} ...

options:
  -h, --help           show this help message and exit
  --limit LIMIT        Max rules to extract
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

### export

Build recommendations from the archive and merge them into the store. New ones
are `inactive`; an existing id keeps its activation and evaluation history, and
evidence links are unioned rather than replaced.

```text
usage: lore rules export [-h] [--recommendations] [--json] [--limit LIMIT]
                         [--include-automated]

options:
  -h, --help           show this help message and exit
  --recommendations    Select the recommendation export (the only export
                       format)
  --json               Print the machine-readable JSON instead of the summary
  --limit LIMIT        Max recommendations to build
  --include-automated  Also include automated (exec/cron/subagent) sessions,
                       hidden by default
```

### activate

```text
usage: lore rules activate [-h] [--by BY] recommendation_id

positional arguments:
  recommendation_id  Recommendation id (rec-...)

options:
  -h, --help         show this help message and exit
  --by BY            Who activates it (default: $USER)
```

Records actor and UTC timestamp, sets `state: active`, exits `1` for an unknown
id.

### evaluate

```text
usage: lore rules evaluate [-h] --verdict {accepted,corrected,contradicted}
                           [--note NOTE] [--by BY] recommendation_id

options:
  -h, --help            show this help message and exit
  --verdict ...         Review verdict; appended to history, never replacing
                        earlier entries
  --note NOTE           Why (free text, stored with the entry)
  --by BY               Who reviewed it (default: $USER)
```

### review

```text
usage: lore rules review [-h]
```

Read-only listing: id, state, scope, generation request, evidence permalinks and
the evaluation history.

## prune

Delete sessions whose prompt count is at or below a threshold.

Writes: marks sessions as ignored in `ignored.json` and deletes their exports (`--dry-run` only prints the selection).

Deletion is confined to the output directory. `lore prune` deletes only regular `.md` export files under the archive root, and refuses: symlinks (including link chains whose target leaves the root), files directly in the top level of the archive, and control or store files (whose names start with `index`, `ignored`, `deleted_sessions`, `title_cache`, `memory`, `memories`, `opencode_state` or `rules`, or end with `-wal` or `-shm`). Each refusal is reported to stderr. The index is then rebuilt from the sources, preserving every entry that was not re-observed, so entries whose source still exists elsewhere are not lost.

Pruned ids are recorded in the store's `ignored_sessions` table, which is the source of truth, and mirrored to `ignored.json` for builds that predate the table; both are read, so an id in either place stays hidden. They are hidden from the v2 store views (`lore search`, stats and the web UI) without deleting the SQLite rows, and the store file's (and its `-wal` sidecar's) stats are part of the v2 index cache key, so a prune takes effect at once. If `ignored.json` is damaged (bad UTF-8, invalid JSON, or the wrong shape), a process warns once on stderr; ids already in the store stay hidden, so a damaged mirror can no longer make pruned sessions visible again.

Archive preservation (see [export](#export)) no longer drops entries on its own: only `lore prune` removes index entries, so the index can contain rows whose source file no longer exists. That is the archive — use `lore prune` to retire such sessions.

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

## backup

Write a bundle of the v2 store, or check an existing one.

Writes: one zip file at the given path (or `lore-backup-<UTC>.zip` inside it when the path is a directory).

A bundle is a zip with a format-version marker: `manifest.json` records
`format: "lore-backup"`, `format_version`, the creation time, the Lore version,
the store's schema version and every applied migration, one sha256 and size per
payload member, and the row count of every archived table (sessions, messages,
the shared FTS index, memory with its tags/sources/ratings, session tags,
knowledge, summaries, embedding bookkeeping and deletion tombstones).
`db/index_v2.sqlite` is a snapshot taken with SQLite's online-backup API, so a
writer running at the same time cannot leave a torn or stale copy — a plain
file copy of a live WAL database can miss the newest commits.

Only the store is archived. The rest of the data directory — including the
provider cache — stays out, and no credential, token or API key is read or
written: provider credentials come from the environment
([CONFIGURATION.md](./CONFIGURATION.md#provider-credentials-not-lore_-variables)).

`lore backup verify <path>` runs the whole verification without restoring
anything: zip structure, format marker, member checksums and sizes, the
snapshot's `PRAGMA integrity_check`, the row counts against the manifest, and
whether this Lore can restore the bundle's schema at all. It prints every
problem it finds and exits `1` if there is one.

```text
usage: lore backup [-h] [verify] PATH [[verify] PATH ...]

Write a bundle of the v2 store taken from a consistent SQLite snapshot, or
check an existing bundle without restoring it (`lore backup verify <path>`)

positional arguments:
  [verify] PATH  destination for a new bundle, or `verify PATH` to check one

options:
  -h, --help     show this help message and exit
```

## restore

Replace the v2 store with the one in a bundle.

Writes: `<output-dir>/index_v2.sqlite` (and removes that store's `-wal`/`-shm` sidecars).

The bundle is verified first, and nothing is written when verification fails.
An existing store is only replaced with `--force`; without it the command
refuses and exits `1`. The snapshot is extracted to a sibling temp file, its
sha256 is re-checked there, and only then moved over the store with an atomic
replace, so a failed restore leaves the existing store as it was. A bundle
recorded by an older Lore is restored as it was written and migrated on the
next open; a bundle from a newer schema is refused. A completed restore stamps
`store_meta.generated_at` and `content_hash` as the write it is: the snapshot
carries the stamp of the moment it was taken, and against a newer local
`index.json` the reader would otherwise treat the restored store as stale and
serve the archive the restore just replaced.

```text
usage: lore restore [-h] [--force] bundle

positional arguments:
  bundle      bundle written by `lore backup`

options:
  -h, --help  show this help message and exit
  --force     Replace an existing store
```

## bundle

Export a portable archive bundle, or merge one from another machine.

Writes: one zip file per export. An import writes the v2 store (`<output-dir>/index_v2.sqlite`) and one append-only row in `bundle_imports`; `--dry-run` writes nothing at all.

A portable bundle is the [backup](#backup) snapshot plus the provenance needed to
merge it somewhere else. `manifest.json` records `format: "lore-portable-bundle"`,
`format_version`, a `provenance` object (the source namespace scope the store's rows
carry, the machine scope, the creation time and the Lore version), the store schema
and every applied migration, one sha256 and size per member, the row count of every
archived table, and an `evidence` fingerprint: how many `source_revisions` and
`source_artifacts` rows the archive holds plus a digest over every captured
`(source_identity, content_sha256)` pair. `db/index_v2.sqlite` is the same
SQLite-consistent snapshot `lore backup` writes, taken with SQLite's online-backup
API; it carries the raw evidence, so the join between a session and the bytes it was
parsed from cannot come apart in transit.

`lore bundle import` verifies the manifest, every member checksum, the evidence
fingerprint, the snapshot's `PRAGMA integrity_check`, the row counts and the schema
compatibility **before** the live store is opened, so a tampered archive writes
nothing. It then merges:

- revisions dedupe on `(source_identity, content_sha256)`; changed bytes become an
  extra `revision_seq`, never an overwritten revision;
- an identical session changes nothing; a divergent copy of the same provider id is
  stored under a qualified id (`<tool>:<scope>:<id>#2`) with `copy_of_session_id`
  naming the row it diverges from — a local row is never replaced;
- imported rows keep their original `source_namespace`, and the command prints how
  many sessions and revisions came from which origin;
- tags, knowledge, summaries, memory (with its tags, ratings and evidence links),
  deletion tombstones and per-source health are merged. Local annotation is never
  overwritten, a tombstone for a session this store still holds is skipped with a
  count, and another host's counters never replace a local health counter;
- embeddings are not imported: a vector belongs to the model that computed it, so
  the receiving host rebuilds it.

`--merge` (the default) keeps everything local. `--replace` installs the archived
store as the live one — the first import onto an empty machine — and `--dry-run`
merges into a temporary copy, so it reports the exact counts of a real import while
writing nothing. Whichever form writes, it re-stamps `store_meta.generated_at` and
`content_hash`: a merged store then holds session rows the index build did not write,
and an installed snapshot carries the stamp of the machine it came from — either one
would make the freshness gate skip the store and serve the archive instead. Neither
form can import a credential: the bundle holds the store and
nothing else (see [CONFIGURATION.md](./CONFIGURATION.md#data-directory-and-backups)).

```text
usage: lore bundle [-h] [--merge | --replace] [--dry-run] {export,import} path

Write a portable bundle of the v2 store (snapshot plus source provenance), or
merge one from another machine into this store without overwriting local
content

positional arguments:
  {export,import}  write a bundle, or import one
  path             destination for export, bundle file for import

options:
  -h, --help       show this help message and exit
  --merge          Merge into the local store (default)
  --replace        Replace the local store with the archive
  --dry-run        Report what import would change and write nothing
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

Writes: the title cache `~/.lore/title_cache/titles.json`. Calls a provider: local by default; `gemini-cli` (cloud) only when selected with `--provider`. Selecting it is not enough: it needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1` (otherwise exit 2, nothing sent). The local default needs no opt-in.

```text
usage: lore generate-titles [-h] [--strategy {fast,keyword,smart,auto}]
                            [--tool TOOL] [--since SINCE] [--force]
                            [--provider {local,gemini-cli}] [--model MODEL]
                            [--allow-cloud] [--include-automated]

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
  --allow-cloud         Allow cloud providers to receive session text (or set
                        LORE_ALLOW_CLOUD=1)
  --include-automated   Also include automated (exec/cron/subagent) sessions,
                        hidden by default
```

Note: Titles use `--provider local` by default and stay on this machine. `gemini-cli` is a cloud provider; it is only used when you choose it, it needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`, and it receives a transcript excerpt per title.

## reindex

Rebuild the index from every source on disk, which fixes sessions missing after a sync.

Writes: `index.json` and the SQLite index under the output directory.

```text
usage: lore reindex [-h]

options:
  -h, --help  show this help message and exit
```

## embed-messages

Populate the optional per-message embedding vectors that `lore search --messages`
uses (#190). Incremental: only messages that have no vector from the current
embedding model are embedded, so re-running after an interruption (or a budget
stop) continues where it stopped. Without the optional `semantic` extra
(`pipx inject lore fastembed sqlite-vec`) the command exits 1 and message search
stays on the FTS path.

Mode: writes `message_embeddings` + `message_embedding_meta` rows inside the
existing v2 SQLite store.

```text
usage: lore embed-messages [-h] [--budget BUDGET]

options:
  -h, --help         show this help message and exit
  --budget BUDGET    Seconds of embedding work (0 = unlimited, default:
                     LORE_EMBED_BUDGET_SECONDS or 60)
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

Writes: memory embeddings, knowledge entries and lesson candidates in the output directory. Calls a provider: only the extract_knowledge stage, and only when `--provider` or `LORE_LLM_PROVIDER` is set (`--dry-run` calls nothing). A non-local provider needs `--allow-cloud` or `LORE_ALLOW_CLOUD=1`, as with `analyze`, `knowledge`, `format` and `summarize`; without it the command exits with code 2 and sends nothing. With the opt-in, a notice on stderr names the provider before the run.

```text
usage: lore improve [-h] [--provider PROVIDER] [--model MODEL] [--allow-cloud]
                    [--limit LIMIT]
                    [--stage {embed_memories,extract_knowledge,distill_lessons,from_summaries}]
                    [--dry-run] [--json] [--include-automated]

options:
  -h, --help            show this help message and exit
  --provider PROVIDER   LLM provider for extract_knowledge
  --model MODEL         Model name for extract_knowledge
  --allow-cloud         Allow cloud providers to receive session text (or set
                        LORE_ALLOW_CLOUD=1)
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

Writes: the redacted handoff context to a unique mode-0600 file
`~/.lore/handoff/context-<id>.md` (no longer the shared
`/tmp/ai-session-context.md`). The file is removed after the launched tool
exits (kept for the manual tools, whose block you paste from it). Stale files older
than 24 hours are removed the next time a handoff file is written, not on a timer.

Sends nothing by itself. Switching to `gemini` or `codex` -- they receive the
context as a referenced file (the process list shows only its path, never the text) -- requires the cloud opt-in
`--allow-cloud` or `LORE_ALLOW_CLOUD=1`; without it the command prints an
error and exits with code 2 (the MCP tool `switch_to_tool` returns the same refusal as text). The context is redacted before it is written or
passed. `claude` is continued with `--continue`, and the manual tools
(`cursor`, `vscode`) only print how to paste the block; both send nothing.

The Gemini handoff needs a Gemini CLI that supports `--include-directories` and `@path`
references; it gets read access to the one private folder of this handoff only.
Google has retired the standalone Gemini CLI for individual accounts (it answers
`IneligibleTierError` and points to Antigravity, seen 2026-10-26); the Gemini handoff and
`--provider gemini-cli` titles then fail, and `codex` or the local `ollama` provider are the
working choices. Other Gemini CLI account types were not tested.

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

MCP server. Stdio is the default and has no `--help`-driven behaviour to speak of:
started without flags it speaks JSON-RPC on stdin/stdout and is driven by the MCP
client. `--http` serves the same tools over streamable HTTP instead. See
[MCP.md](./MCP.md) for the tool list, both transports and client configuration.

```text
usage: lore-mcp [-h] [--http] [--host HOST] [--port PORT] [--path PATH]
                [--token-file TOKEN_FILE] [--timeout TIMEOUT]
```

```text
  --http                Serve the streamable-HTTP transport instead of stdio.
  --host HOST           HTTP bind host (default 127.0.0.1, or $LORE_MCP_HTTP_HOST).
                        A non-loopback host requires an auth token.
  --port PORT           HTTP bind port (default 8787, or $LORE_MCP_HTTP_PORT).
  --path PATH           HTTP endpoint path (default /mcp, or $LORE_MCP_HTTP_PATH).
  --token-file TOKEN_FILE
                        File holding the bearer token (or set
                        $LORE_MCP_HTTP_TOKEN). Required for a non-loopback bind;
                        the token is read from the file, never from argv.
  --timeout TIMEOUT     Per-request deadline in seconds (default 60, or
                        $LORE_MCP_HTTP_TIMEOUT).
```

A non-loopback `--host` without a token exits `2` with a message on stderr and
never binds (`--http` also exits `2` for an unreadable or empty token file, or a
malformed `--path`).

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
  `lore_cli.py` (via `require_cloud_opt_in` in `lore/services/summarize_cli.py`; `improve` and `generate-titles` call `require_cloud_opt_in` directly)
  returns `2` for `gemini`, `gemini-cli`, `openrouter`, `jev`, or an off-host `ollama`
  without `--allow-cloud` or `LORE_ALLOW_CLOUD=1`; nothing is sent and nothing
  is written. `lore-session switch` and `lore-session continue` also exit with
  `2` when the target tool is `gemini` or `codex` without the opt-in.
  The value `2` is not unique to this refusal: argparse usage errors (an unknown
  option, or a missing argument) also exit with code `2`.

Automation can therefore treat any non-zero code as failure. Because argparse
usage errors share the code `2`, confirm the refusal by its stderr message
before treating `2` as a rejected cloud opt-in.

The other entry points have their own codes. `lore-web-probe` returns `2` when
`--user` is given without a usable password and `3` when the probe matrix was
blocked; `lore-web` and `lore-mcp` are long-running servers, so their exit code
reflects shutdown rather than a command result.
