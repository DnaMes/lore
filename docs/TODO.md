# TODO — open decisions and stopped work

Entries here are owned by the maintainer. Each one carries a recommended
decision so nobody has to start from zero.

## Open decisions

### Product name (blocks any public launch)

"Lore" collides in three places (checked 2026-10-14): an unrelated framework
owns `lore` on PyPI, `getspecstory/lore` is a project of the same name for the
same audience, and another product named "Lore" was announced on Hacker News in
June 2026.

**Recommended decision:** rename before any public launch. Keep the import
package `lore` and the repository as they are until a name has been checked on
PyPI, GitHub and a trademark search; branding is not part of the engineering
program in `docs/superpowers/plans/2026-10-14-coverage-summaries-ui.md`.

### Distribution

Comparable tools ship a single binary or a desktop app. Lore is a Python package
with a Flask UI.

**Recommended decision:** keep Python. After tool coverage is finished, add a
`pipx`-installable entry point and a Docker image. Do not rewrite.

### macOS and Windows

Storage paths for those systems come from other projects' documentation and
were not verified on real machines.

**Recommended decision:** release Linux first and label the other systems
"best effort" until someone runs them.

### Competitors worth tracking

`agentsview`, `coding_agent_session_search` (cass) and `deja-vu` cover 33 to 65
tools. Licence notes: `basic-memory` is AGPL-3.0 and cass carries an additional
clause, so take ideas, not code. See `docs/research/2026-10-14-oss-landscape.md`.

### Semantic recall is English-only (bge-small-en-v1.5)

The 2026-10-14 floor calibration (`docs/research/2026-10-14-recall-floor-calibration.md`)
shows two misses no floor value can fix: a German query against an English memory scores 0.430
(inside the unrelated range) and a keyword-poor paraphrase lands at 0.530. The keyword path covers
the second one when the wording overlaps; neither is a tuning problem.

**Recommended decision:** when #92 calibrates the session-level cutoff, evaluate a multilingual
local model (bge-m3) against a German+English pair set in the same script and switch only if it
beats bge-small on *both* languages. The local-only embedding policy stays either way.

### Review gate approves without seeing the diff (harness)

`review-gate` approved every commit of the 2026-10-14 Wave-0/Track-L pass, but each reviewer
repeated the same note: the prompt carried only the stat, not the hunks, so nothing could be
inspected line by line (report files in `~/.local/state/ai-stack/review-gate/`). The approvals are
real decisions, just made without the diff.

**Recommended decision:** file a `harness-idea` against ai-stack's `review-gate` to embed the
diff itself (it is already capped by `MAX_DIFF_CHARS`) instead of the stat, and until that lands
treat an APPROVE for this repo as "no finding raised", not as "line-level reviewed". Do not hold
Lore commits back over it.

## Deferred roadmap (label `deferred` on GitHub)

### #29 — MCP-over-HTTP transport (streamable-http)

Lore's MCP server runs over stdio only. An HTTP transport is real work and the
`/api/search` shape it would have to expose is about to change under #174 and
#175.

**Recommended decision:** implement after #174 and #175 are closed, so the
transport freezes a search surface that is already final. Until then the issue
stays open with the `deferred` label.

### #48 — JSON-retirement exit criteria

`index.json`/`index.sqlite` are still written next to the v2 store; the coexistence
is annoying but reversible, and five exit criteria remain (tombstones into v2,
search routed through v2, staleness assert, CI equivalence test, stop writing).

**Recommended decision:** start only when the CI equivalence test between v2 and
JSON exists (sub-task 4 of the issue). Everything before it is safe; the final
step is the one that cannot be rolled back cheaply without that test.

## Stopped work

None yet. The overnight loop appends entries here when a task fails four times
in a row or needs something outside its constraints.
