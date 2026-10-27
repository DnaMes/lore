---
name: lore-memory
description: >
  Use Lore's shared memory store to recall what earlier agent sessions decided and to
  record durable facts, decisions and lessons, through the `lore memory` CLI or the MCP
  tools. Use at the start of a task (recall before you re-derive something) and right
  after a decision, a non-obvious lesson or a failed assumption worth not repeating.
  Triggers: lore memory, shared memory, record a decision, remember this, what did we
  decide, memory recall, cross-agent memory, lore-mcp memory_write.
---

# Lore shared memory

Lore keeps one local SQLite store of facts, decisions and lessons under `~/.lore`.
Every AI tool on the machine reads and writes the same store, so a decision recorded
by one session is recallable by the next one — nothing leaves the host.

Two rules make the store useful instead of noisy:

- **One memory, one fact.** If you want to say two things, write two memories.
- **Recall first.** Before re-deriving a design decision, a config value or a reason
  something was rejected, ask the store.

## Recall

```bash
lore memory search "database performance"        # keywords plus meaning when available
lore memory search "database performance" --semantic
lore memory search '"exact phrase"'
lore memory list --kind decision --project /path/to/repo --limit 20
lore memory show 7
lore memory list --all                            # include superseded rows
```

Recall returns confirmed memories only. The reason a memory exists is in its
`--body`; the `--title` is the one-line claim, so search on the words the claim would
use, not on the words of the task.

## Record

```bash
lore memory add --kind decision \
    --title "Standardise the stack on Postgres 16" \
    --body "Every service uses PG16; upgrading is a separate decision." \
    --project /path/to/repo --author <tool> --tag db --tag infra
```

- `--kind` is one of `fact`, `decision`, `todo`, `snippet`, `link`, `lesson`, `note`
  (default `note`). Use `lesson` for "what did not work and why", `decision` for a
  choice with its reason.
- `--project` scopes the memory. Omit it only for a fact that is true for the whole
  machine.
- `--author` names the tool that recorded it (`claude`, `codex`, `pi`, …).
- `--from-session <id>` records where the fact came from, when it came out of a
  transcript.
- `--supersedes ID` replaces an active memory of the same project. It is a
  supersede, never an edit: the old row stays readable for audit, and the new one
  carries the current belief.

Write in English, keep it under a few sentences, and never record a secret, a token,
a credential path or personal data. The store is local, but it is shared with every
agent and it is backed up.

## Review what an agent generated

Machine-generated memories arrive as `candidate` and stay out of recall until a human
accepts them:

```bash
lore memory review                    # what is waiting
lore memory review accept 12 15
lore memory review reject 14          # keeps the row, excludes it from recall
```

The web UI shows the same queue at `/memory?status=candidate`.

## Hand a budget to the agent

`lore memory context` renders a ranked, redacted block that fits a token budget —
project matches first, then related scopes, then global memories:

```bash
lore memory context --project /path/to/repo --budget 2000
lore memory context --project /path/to/repo --format json
```

Use it when a session should start with the relevant memory instead of a raw dump.

## Housekeeping

```bash
lore memory rate 7 5                  # 1 (wrong) … 5 (right and useful)
lore memory dedupe                    # dry run; --apply rejects duplicates
lore memory repair-index               # memory rows missing from the search index
lore memory import-claude --apply      # seed from Claude Code auto-memory files
```

Dedupe and import are dry runs by default; nothing is deleted, rejections are
reversible and every apply writes an undo log.

## Over MCP

The same store behind three MCP tools, for clients that cannot shell out:

| Tool | Purpose |
|------|---------|
| `memory_write` | record a memory (`title`, `body`, `kind`, `project`, `tags`, `author`, `source_session`, `supersedes`); the response lists near-duplicates in `similar` |
| `memory_recall` | search; `semantic=false` forces keywords only, a quoted query is an exact phrase |
| `memory_feedback` | rate a confirmed memory 1–5 |

`memory_recall` returns nothing until something has been written — an empty store is
not an error.

## What not to do

- Do not record the task you are working on; record what will still be true next week.
- Do not rewrite a memory in place; write a new one with `--supersedes`.
- Do not record a hypothesis as a `fact` — use `lesson` or `note`, or let it arrive as
  a `candidate` and get reviewed.
- Do not read or edit `~/.lore/index_v2.sqlite` directly; the CLI and MCP tools keep
  the search index and the embeddings consistent, direct writes do not.
