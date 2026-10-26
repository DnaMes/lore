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

### 2026-10-26 doc pass — done in the release commit

Shipped together on 2026-10-26: the archive move of UMBAU, ROADMAP, PLAN-hybrid-search,
REVIEW, SESSION-HANDOFF, NEXT-SESSION-*, EXECUTION-PROMPT into `docs/archive/`; the new
`docs/CLI.md`, `docs/CONFIGURATION.md`, `docs/PRIVACY.md` and the README rewrite; the cloud
opt-in gate (`require_cloud_opt_in` in `lore/services/summarize_cli.py`, used by analyze,
format, knowledge, improve and summarize); and a standalone `docker-compose.yml` with the
maintainer's Traefik setup in `docker-compose.traefik.example.yml`.

## Backlog sweep 2026-10-26 — deferral decisions

The sweep (`docs/triage/LEDGER.md`) verified 44 open issues against the code. Two were
closed as already done (#176, #192), one as obsolete (#191: no PyPI publication, so
install-from-clone is the only documented path and the distribution stays `lore`).
The following are confirmed gaps, not guesses, and are deliberately **not** in this
session's scope. Each stays open with its `deferred` label.

- **Capture and identity spine (#151, #153, #154, #155, #156, #157, #158, #160)** — one
  design, several migrations: an append-only source-revision store, a canonical message
  envelope, namespaced session identity and stable message evidence references. The
  archived plan already says these need a written spec each before any migration.
  **Recommended decision:** write `docs/superpowers/specs/2026-XX-source-revision-store-design.md`
  and the identity/envelope spec, run both through the review gate, then implement in the
  order #151 → #154 → #155 → #160; #153, #156, #157 and #158 follow on top of them.
- **Backup, bundle import and health (#161, #162, #163)** — `lore backup`/`restore` on the
  SQLite online-backup API, a per-source health table and portable bundle import. #161 is
  the one with real data-loss risk today (there is no way to take a consistent backup).
  **Recommended decision:** implement #161 first and alone, with a restore-verification
  test that compares counts, identities, FTS rows and annotations.
- **Export and reading experience (#165, #166, #167, #168, #169, #170)** — the exporters
  were never migrated to the canonical tool-call envelope the web path already uses, so
  Markdown/HTML exports show "Unknown Tool" and drop results. These are additive and
  independent of the migrations.
  **Recommended decision:** #166 first (export the stored `tool_events`), then #165 and
  #169 (full Markdown, AI-independent), then #168/#167/#170 as the reading contract.
- **Query and analysis (#171, #172, #173, #174, #175, #177, #178, #179, #180)** — the
  archived plan fixes the order #172 → #173 → #174 → #171 → #175; #177/#178/#179/#180
  follow the analysis pipeline.
  **Recommended decision:** keep the planned order; #174 (filter before limit) is the one
  that makes the search API worth freezing for #29.
- **Native-CLI adapters and analysis groundwork (#181, #182, #183, #184, #186, #187, #188,
  #189, #190)** — no adapter layer exists yet; every one of these needs #180's provider
  boundary first, and #188/#189 need a written metric vocabulary.
  **Recommended decision:** #180, then #181/#182/#184 (native CLIs, native auth only), then
  #186/#187 on top of the job table from #179. #183 ships only the contract note and a
  stub until the Antigravity CLI contract can be confirmed offline.
- **#119 (split `web.py`)** — still 2700 lines, and the archived plan puts it last because
  every other web item touches the file.
  **Recommended decision:** do it after the #165-#170 export/reading work, one blueprint per
  commit, keeping the late-import patch targets that ~15 test files rely on.
- **#194 (browser interaction checks)** — `tools/playwright_e2e_regression.py` is not a
  pytest fixture yet.
  **Recommended decision:** wrap it as a `@pytest.mark.browser` test that skips when
  Playwright is unavailable, then add the release-checklist section to `docs/DOCKER.md`.


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
