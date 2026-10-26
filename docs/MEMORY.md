# Shared memory

Lore keeps a shared local store of facts, decisions, lessons and links that any AI tool
can write to and recall later, either through the CLI or over MCP. Memories live in the
same SQLite index as the extracted sessions, under `~/.lore`; the memory tables
(`memory`, `memory_tags`, `memory_sources`, `memory_embeddings`, `memory_feedback`) are
defined in `lore/storage/schema.py`. Because they share the unified search index, a
single query can surface both past sessions and recorded knowledge. Nothing leaves the
machine.

## Kinds and statuses

Every memory has a kind: `fact`, `decision`, `todo`, `snippet`, `link`, `lesson` or
`note`. The default kind is `note`.

Two statuses are writable: `candidate` and `confirmed`. Ordinary writes take a
confirmed memory, while imported material whose type cannot be classified lands as a
candidate note. A third status, `rejected`, is only reachable through the dedupe path or
the storage API, never at write time; rejected memories stay in the database with their
provenance.

Writes are superseding, not editing. `--supersedes ID` replaces one active, confirmed
memory of the same project: the new row is inserted and the old row's `superseded_by`
points at it, in a single transaction. The old belief remains visible for audit.

## Record

```bash
lore memory add \
    --kind decision \
    --title "Use Postgres 16" \
    --body "Standardise the stack on PG16" \
    --project /path/to/repo \
    --tag db --tag infra
```

- `--kind` one of the kinds above (default `note`)
- `--title` short memory title (required)
- `--body` the memory content (required)
- `--project` optional project scope; omit it for a global memory
- `--author` who recorded it, default `human`
- `--tag` tag, repeatable (`--tag db --tag infra`)
- `--from-session` id of the session the memory came from, for provenance
- `--supersedes ID` id of an active, confirmed memory of the same project to replace

## Find

```bash
lore memory list
lore memory list --kind decision --project /path/to/repo --limit 20
lore memory list --all                      # include superseded memories
lore memory search postgres
lore memory search "database performance"   # keyword plus meaning when available
lore memory search "database performance" --semantic
lore memory search '"exact phrase"'         # quoted phrase
lore memory show 7
```

- `lore memory list` filters by `--kind`, `--project`, `--limit`, and hides superseded
  rows unless `--all` is given.
- `lore memory search QUERY` filters by `--kind`, `--project`, `--limit`.
- `--semantic` ranks by meaning only, with no relevance floor. Without the flag, search
  uses keywords plus meaning when the optional semantic extra is installed, and stays
  keyword-only otherwise. See `./INSTALL.md` for the extra.

In the default mode only semantic candidates above the calibrated recall floor are added
to the keyword results, so an unrelated query is not padded with nearest neighbours. The
floor is configurable through `LORE_MEMORY_RECALL_MIN_SIMILARITY`; see
`./CONFIGURATION.md` for the value and the calibration record.

By default only confirmed memories are returned.

## Context for an agent

`lore memory context` renders a prompt block for injection into an agent session:

```bash
lore memory context --project /path/to/repo --budget 2000
lore memory context --project /path/to/repo --tool claude --format json
```

- `--project` project scope, default the current working directory, resolved
- `--budget` token budget estimate, default 2000, computed as `ceil(chars / 4)`; the
  whole output, footer included, fits the budget
- `--tool` optional tool scope filter
- `--format` `text` or `json`, default `text`
- `--limit` maximum number of memories to consider

Ranking is by scope tier: an exact project match first, then related scopes, then global
memories. A related scope is a descendant of the project, or an ancestor that is specific
enough. Overly broad ancestor scopes are never treated as related: the filesystem root,
the home directory, and any scope with fewer than four path components. Within a tier,
feedback weight, update time and kind priority decide the order. The output is redacted
and prints one line per memory.

## Import from Claude Code

`lore memory import-claude` seeds the store from Claude Code auto-memory files:

```bash
lore memory import-claude                  # dry run: count what would be imported
lore memory import-claude --apply           # write the memories
lore memory import-claude --root /path/to/.claude/projects --apply
```

- Dry run is the default; `--apply` writes.
- `--root` Claude projects directory, default `~/.claude/projects`.
- Type mapping: `user` becomes `fact`, `feedback` becomes `lesson`, `project` becomes
  `fact`, `reference` becomes `link`.
- A missing or unknown type, or a file without a usable name and description, is
  imported as a candidate `note`.
- `MEMORY.md` index files, files without frontmatter and files with empty bodies are
  skipped.
- Re-running writes nothing for unchanged files. An edited file is written as a new
  memory that supersedes the previous import.
- Titles and bodies are redacted before writing.
- Identical normalized content is skipped across scopes, and duplicates are reported.

## Dedupe

`lore memory dedupe` finds exact duplicate groups among confirmed memories:

```bash
lore memory dedupe                          # dry run: print groups with kept/rejected ids
lore memory dedupe --apply                  # reject the planned duplicates
lore memory dedupe --restore dedupe-undo-2026-10-14T10-00-00Z.json
```

- Dry run is the default. `--apply` rejects the planned duplicates in one transaction and
  writes a JSON undo log under the store.
- The undo log records the store identity. `--restore FILE` refuses a log from a different
  store. Legacy logs without an identity restore with a warning.
- Restore reverses rejections recorded in the log, as long as they are still in place;
  restoring is idempotent.
- Every row and its provenance is preserved on apply; nothing is deleted, and a repeat
  run rejects nothing.
- Comparison collapses whitespace and unescapes quotes; titles ignore case, bodies remain
  case-sensitive. Dedupe never merges across `kind`, `scope_project` or `scope_tool`.
- Keeper order: highest feedback weight, then newest update, then highest id.

## Review queue

A memory written by an agent can arrive as `candidate`: stored, but outside recall
until a human accepts it.

```bash
lore memory review                      # list what is waiting
lore memory review accept 12 15         # confirm them
lore memory review reject 14            # keep the row, exclude it from recall
lore memory review --json               # machine-readable listing
```

The web UI has the same queue at `/memory?status=candidate`, with Accept and Reject
on each card; both post to `/memory/<id>/review`. Rejection is a status change, not
a delete: `set_memory_status(..., "confirmed", from_status="rejected")` restores a
memory, and `lore memory review` never touches a memory that is not a candidate
(it reports the id as unchanged).

## Rate

```bash
lore memory rate 7 5
lore memory rate 7 2 --rater alice
```

`ID` is the memory id, `SCORE` runs from 1 (wrong) to 5 (right and useful), and `--rater`
defaults to `human`. Feedback is stored as a bounded weight. It changes recall ranking
only when `LORE_FEEDBACK_INFLUENCE` is above 0; see `./CONFIGURATION.md`.

## Agents over MCP

MCP clients reach the same store through three tools:

- `memory_write` records a memory
- `memory_recall` searches memories
- `memory_feedback` records a rating

See `./MCP.md` for the setup and payloads, and `./API_REFERENCE.md` for the full tool
schemas.
