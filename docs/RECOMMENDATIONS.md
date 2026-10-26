# Harness recommendations: reviewed, evidence-backed, inactive by default

Issue: [#189](https://github.com/DnaMes/lore/issues/189).
Implementation: `lore/utils/rules.py` (build + store) and the `lore rules`
subcommands in `lore_cli.py`.

`lore/utils/rules.py` could already extract rule-like sentences. That output
was a flat list: no scope, no provenance, no way to review or reject a rule, and
no record of a correction. This document is the contract for the reviewed layer
that replaces it as the durable artifact.

## What a recommendation is

A recommendation is a candidate harness rule with everything a reviewer needs to
decide, and nothing that applies itself.

| Field | Meaning |
|-------|---------|
| `id` | `rec-` + 12 hex chars, derived from `sha256(scope, scope_target, text)`. Re-exporting the same rule in the same scope keeps the same id, so review state survives. |
| `text` | The rule sentence, redacted at build time. |
| `scope` | `project`, `tool` or `global`. |
| `scope_target` | The project path, the tool name, or `*` for global. |
| `state` | `inactive` (default) or `active`. Only an explicit activation changes it. |
| `created_at` | UTC ISO timestamp of the build that first produced the id. |
| `activated_by` / `activated_at` | Who activated it and when; `null` until then. |
| `current_verdict` | Latest history verdict, or `unreviewed`. Derived, read-only. |
| `generation` | `{provider, model, prompt_version}` of the generator that produced the text: `local` / `sentence-keyword-v1` / `rules-extract-v1`. |
| `evidence` | List of `{session_id, tool, message_seq, permalink, quote}`. |
| `history` | Append-only list of `{verdict, at, by, note}`. |

### Scope derivation

Scope is a claim about where the rule holds, so it is derived from the evidence,
not guessed:

1. **project** — every supporting message comes from sessions with a
   `project_path`, and they all name the *same* project.
2. **tool** — the evidence does not pin one project (mixed, or some session has
   no `project_path`) but all supporting sessions come from one tool.
3. **global** — anything else.

An evidence set where *some* session lacks `project_path` cannot produce project
scope: one unknown location is enough to widen the claim.

### Evidence links

`permalink` is `session:<session_id>#m<seq>`, where `seq` is the 0-based index of
the message in that session's message list. `quote` is the redacted sentence
itself, so a reviewer sees the supporting text without opening the archive.

### Generation request

The generator is local and deterministic, and the request is recorded anyway:
`provider: local`, `model: sentence-keyword-v1`, `prompt_version:
rules-extract-v1`. The record is what makes an export reproducible and what
makes "which generator produced this rule" answerable after an upgrade. A future
LLM-backed generator records its own triple; the field set does not change.

## Lifecycle

```text
            build/export                activate                 evaluate
(none) ─────────────────▶ inactive ──────────────▶ active ──────▶ active
                            │                       │               │
                            └── history: accepted / corrected / contradicted ──┘
```

- **Export** (`lore rules export --recommendations`) builds fresh evidence and
  merges it into the store. New recommendations are `inactive`. Existing ones
  keep their `state`, activation metadata and `history`; evidence is unioned by
  permalink, so a re-scan adds links and never erases the ones a reviewer
  already saw. A stored recommendation whose evidence no longer appears in the
  fresh scan is kept, not deleted — review history outlives a deleted session.
- **Activate** (`lore rules activate <id> [--by NAME]`) sets `state = active` and
  records who (`--by`, else `$USER`, else `unknown`) and when (UTC). This is the
  entire effect: no harness file is read or written.
- **Evaluate** (`lore rules evaluate <id> --verdict accepted|corrected|contradicted
  [--note TEXT] [--by NAME]`) appends one history entry. The original `text`,
  the evidence and the earlier entries are untouched. `current_verdict` (and the
  human-readable listing) shows the newest verdict, which is how contradictory or
  superseded evidence stays visible.
- **Review** (`lore rules review`) is a read-only listing.

A contradiction does **not** deactivate, rewrite or delete a recommendation. The
history is the record; the state is the human's decision. Both stay visible.

## Storage

One JSON file in the output directory: `recommendations.json`, mode `0600`
(it carries session-derived text).

```json
{
  "format": "lore-recommendations",
  "format_version": 1,
  "generated_at": "2026-04-02T00:00:00+00:00",
  "recommendations": [ { "id": "rec-...", "text": "...", "scope": "project", "...": "..." } ]
}
```

The same payload is what `--json` prints, so the file, the CLI output and a
machine reader never diverge. A damaged or foreign file raises rather than being
silently overwritten (`lore rules export` refuses to start), and a file written
by a newer `format_version` is refused the same way.

## Secrets

`text` and every `quote` pass through `lore.utils.redact.redact` before they
enter a `Recommendation`, so the store cannot contain a secret-shaped string and
`--json` output is safe to paste. Verified by
`test_no_secrets_in_export` (a planted `sk-proj-…` key comes out as
`[REDACTED:openai_key]`).

## What this does not do

- **It does not apply anything.** Neither export, activation nor evaluation
  writes `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json` or any other harness
  configuration. `test_export_does_not_modify_harness_configuration` pins this:
  after an export the harness tree is byte-identical and the store is the only
  new file.
- **It does not auto-activate on repetition.** A rule seen in ten sessions is
  still `inactive`; frequency is evidence, not consent.
- **It does not delete superseded recommendations.** Deleting the history would
  destroy the audit trail that justifies the review flow.
