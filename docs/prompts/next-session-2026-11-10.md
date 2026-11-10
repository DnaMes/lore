# Next session brief - 2026-11-10

- repo: /home/dnames/projects/ai-stack/lore
- target tool: claude
- branch: main
- HEAD: 2c3bea9a92e5ce67206ef45795b2d65291650bd7
- written by: `ops nextsession` (prompt-master Template M)

## Validation findings

- The four flagged paths (`Days/`, `Index.md`, `Projects/`, `Sessions/`) live under `~/.lore/library`, not in the repo; references now use the full path.

## Context
- Repo: `/home/dnames/projects/ai-stack/lore`, branch `main`. The library vault is built by `lore/library/builder.py` (`build_library`, `cmd_library` in `lore_cli.py`, subcommand `lore library`) into `~/.lore/library` (`~/.lore/library/Index.md`, `~/.lore/library/Projects/`, `~/.lore/library/Days/`, `~/.lore/library/Sessions/`). The `lore-worker` container runs that job after every host `lore-sync` stamp.
- The one-liner shown on session pages and in the project/day listings comes only from a stored LLM summary: `_one_liner()` in `lore/library/builder.py` reads `get_summary(output_dir, "session", f"session:{session_id}")` and returns `None` when nothing is stored.
- On this host no summaries exist: the worker reports summaries `skipped` (no local Ollama on p15, cloud off by default). Measured on the deployed vault: 1050 session pages under `~/.lore/library/Sessions`, 137 pages under `~/.lore/library/Projects`, and zero of the session pages carry a `summary:` front-matter key. Project-page bullets therefore read like `- [[Sessions/...|title]] · 2026-10-22 · claude-code` with nothing after the tool name.
- Cheap, already-available fallback source: the v2 store column `sessions.prompt_outline` in `/home/dnames/.lore/index_v2.sqlite` (12807 rows; 11162 non-empty, 11040 longer than 30 characters; examples: `Research task (read-only, very thorough). The user's old ThinkPad E14 ...`, `Search (read-only) for any prior notes/decisions about Logitech MX Master ...`). Some values are near-useless (`Fast mode OFF`) or empty, and the display title itself is sometimes a placeholder (`continue · 01a1686f`).
- The builder already has everything needed for a fallback: `meta` (with `prompt_outline`, `title`) is passed around, and `_fingerprint()` already mixes the summary and `prompt_outline` into the page fingerprint, so a changed one-liner triggers an incremental rewrite.
- Cost constraint that shapes the design: `tests/test_library.py::test_incremental_skips_message_load_for_unchanged_sessions` asserts that an unchanged session is not re-read via `load_session_messages_v2`. A fallback derived from messages would break that test and slow every incremental build, so the fallback must come from metadata that is already loaded (`prompt_outline`, then the display title).
- Toolchain in this repo: Python 3.12.14 in `.venv`; `.venv/bin/python -m pytest`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`. `docs/CLI.md` has a `## library` section (around line 1119) and is pinned against the parser by `tests/test_docs_cli_drift.py` — prose may be added, the usage block must stay as it is.
- Known dead ends, do not re-open: the summarize/coverage polish loop (HANDOFF open item 2, four Codex rounds each found one more edge), the stale public `*.local.erdlabs.com` DNS record (belongs upstream, needs an external change), the `NOT VERIFIED` platform items, and re-tagging v2.4.0.

## Target State
`lore/library/builder.py` derives a one-line description for every session in this order: a stored LLM summary wins; otherwise a cleaned `prompt_outline` (whitespace collapsed to one line, markdown/fence noise and boilerplate such as `Base directory: ...` stripped, clipped to a readable length with an ellipsis); otherwise the display title. The value is written to the `summary:` front-matter key of every session page and to the bullet lines on the project and day pages. The fallback is deterministic, offline and never calls a provider. Existing behaviour for stored summaries is unchanged.

## Scope
- Work only in: `lore/library/builder.py`, `tests/test_library.py`, `docs/CLI.md` (one short paragraph under `## library`), `CHANGELOG.md` (one entry line).
- Do NOT touch: `lore/services/summarize.py`, `lore/services/summarize_chunks.py`, `lore/services/summarize_cli.py`, `docker-compose.yml`, `Dockerfile`, `lore_cli.py` (no new flags), `docs/triage/LEDGER.md`, `.github/`, `uv.lock`, `pyproject.toml`, anything outside `/home/dnames/projects/ai-stack/lore`.

## Constraints
- Deterministic and offline: no provider call, no network request, no translation, no re-summarising of user text.
- No new dependencies, no new CLI flags, no new module: the change stays inside `lore/library/builder.py` plus tests.
- The fallback must not call `load_session_messages_v2` for sessions whose page is not rewritten; `tests/test_library.py::test_incremental_skips_message_load_for_unchanged_sessions` must stay green.
- `_fingerprint()` must keep covering the description, so an added or changed one-liner rewrites exactly that session page.
- One-line output: no newlines inside the value; the front-matter writer (`_inject_frontmatter`) already escapes scalars.
- User text stays verbatim (German and English sessions alike); only whitespace, boilerplate and length are normalised.
- Only make changes directly requested. No refactors of the builder, no new abstractions beyond the one fallback helper, no unrelated cleanups.

## Acceptance Criteria
- [ ] `cd /home/dnames/projects/ai-stack/lore && nice -n 10 .venv/bin/python -m pytest tests/test_library.py -q` exits 0, and the file contains new tests proving: (a) a session without a stored summary gets a non-empty one-liner from the fallback chain, (b) a stored summary takes precedence over the fallback, (c) the one-liner appears in the session page front matter and in the project-page bullet line.
- [ ] `cd /home/dnames/projects/ai-stack/lore && nice -n 10 .venv/bin/python -m pytest tests/test_docs_cli_drift.py -q` exits 0.
- [ ] `cd /home/dnames/projects/ai-stack/lore && .venv/bin/ruff check . && .venv/bin/ruff format --check .` exits 0 with no findings.
- [ ] `cd /home/dnames/projects/ai-stack/lore && .venv/bin/python lore_cli.py --output-dir /home/dnames/.lore --output /tmp/lore-library-check library` exits 0 and prints its stats JSON.
- [ ] `grep -L '^summary: .' $(find /tmp/lore-library-check/Sessions -name '*.md')` prints nothing, and the project bullets in `/tmp/lore-library-check/Projects/*.md` each end with a non-empty description (report the number of pages and projects checked).
- [ ] `git -C /home/dnames/projects/ai-stack/lore status --porcelain` lists only `lore/library/builder.py`, `tests/test_library.py`, `docs/CLI.md`, `CHANGELOG.md`.
- [ ] `git -C /home/dnames/projects/ai-stack/lore diff --stat` shows no change to `lore/services/`, `lore_cli.py`, `docker-compose.yml` or `uv.lock`.

## Action Boundaries
- Proceed with reversible, in-scope inspection, edits and validation.
- Stop and ask before: rebuilding the container image, recreating or restarting the `lore-worker` or `lore-app` containers, writing into the live `/home/dnames/.lore/library` tree, committing or pushing, and any change outside the four files in scope.
- Do not start an LLM/Ollama service to produce summaries, and do not modify the DNS records, the summarize/coverage code or the release tags.

## Progress Evidence
Report progress only when it changes or when a checkpoint is reached. Ground every completion claim in a tool result: paste the pytest summary line, the ruff result, the library build stats JSON, and the counts behind the `grep -L` check. If a check fails, report the exact failure and stop rather than widening the scope.

## Session Strategy
New session — the brief carries the full state, no prior context is needed. Start by reading `lore/library/builder.py` (functions `_one_liner`, `_index_entries`, `_render_session_page`, `_fingerprint`) and `tests/test_library.py`.

TARGET: Lore's Markdown library gets an offline one-line description per session, so the 1050 deployed session pages and 137 project pages are navigable without an LLM summary.
TIP: Open `/home/dnames/projects/ai-stack/lore/lore/library/builder.py` (`_one_liner`, `_index_entries`, `_render_session_page`) together with `tests/test_library.py`.

## Also open (after the target above; pick in this order)

1. Summaries: the worker skips them (no local Ollama on p15, cloud off by default). Decide a provider through `ops council` (subscription lane, daily cap; `LORE_WORKER_SUMMARY_DAILY_CAP`), so `session_summaries` fills; dashboard, library pages and "Recently in this project" already read them. Check: `docker exec lore-worker lore worker status --json`.
2. Deploy: `lore-worker` is a `docker run` container (command in HANDOFF.md) and must be recreated after every image rebuild; extend `.parachute/swap-lore-app.py` (gitignored) to swap both containers.
3. Embedding backlog drains 120 s per pass (`pending`); confirm it reaches zero and the job settles to `ok`.
4. Library limits: `--project`/`--since` leave pages of sessions deleted from the store until a full run; title-rule changes need `lore library --full`.
5. Session view phase 2/3 from `docs/research/2026-11-10-session-view-readability.md`: richer TOC (#6), thinking preview (#8), turn keyboard navigation (#9), sub-agent branches (#7), live follow (#11), word diffs (#12).
6. Optional: link `~/.lore/library` into the Obsidian vault following the `obsidian-keeper` rules.
