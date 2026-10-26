# Next-session prompt — Issue #96 (IndexBuilder RAM)

Copy everything below the line into a fresh Lore session.

---

Work on **issue #96**: the reload/reindex path holds all ~960 sessions in RAM
(~1.7 GB) and I want that bounded. Read the issue first (`gh issue view 96 -R
DnaMes/lore`) — it has the measurements. Also read `HANDOFF.md` (the "NEXT
SESSION — #96" block) for the entry points already located.

**Established facts (verified 2026-07-02 — do NOT redo):**
- Extracting all 962 sessions into a list = **1740 MB RSS**, measured with zero
  embedding. The embedding model is only +263 MB. So this is a pre-existing
  IndexBuilder problem, unrelated to hybrid search (#87).
- `IndexBuilder.build_index(sessions: List[UnifiedSession], …)` in
  `lore/exporters/index.py:88` takes a materialised list. It feeds several
  consumers from that one list: the JSON `sessions`/`stats`/keyword inverted
  index, `_build_sqlite_index`, and the v2 dual-write (`write_sessions_safe` →
  `writer.write_sessions`, which iterates again + embeds).
- Callers that eagerly build the list: `lore_cli.py` (`sessions.append` loops at
  ~145/218/268/315; `build_index(all_sessions, …)` at ~298/454/518/784/1214) and
  `lore/services/extraction.py:119,262` (the **web reload** path — the one that
  matters for the Sync button).
- Extractors already return iterators (`extract_sessions() -> Iterator`), so the
  source is lazy — the callers force it into a list.

**Goal:** make extraction→index-write streaming/batched so peak RSS is bounded
regardless of archive size, **without changing behaviour** (identical
index.json, v2 rows, FTS, and vectors).

**Approach to evaluate (brainstorm first — this is architecture, not a quick fix):**
- Two-pass: a cheap metadata-only pass for stats/counts, then a streamed
  body pass that writes each session and drops it. Or a single pass with running
  aggregates + an iterator threaded through `build_index` and `write_sessions`.
- Watch the coupling: JSON index, SQLite FTS, and v2 all consume the same data;
  a naive "just pass a generator" breaks the second consumer. Design for all of
  them before coding.
- Batching (e.g. flush every N sessions inside one transaction) may be simpler
  than full streaming and enough to bound memory — measure.

**Guardrails:**
- Start with the `brainstorming` skill (design before code), then `/steelman`
  the chosen approach — this touches the core write path.
- Keep it surgical; don't refactor unrelated IndexBuilder logic.
- **Verify with a real before/after RSS measurement**, not "should be lower":
  extract-all into memory (or run a reload) and read `/proc/<pid>/status` VmRSS.
  Target: peak stays roughly flat as session count grows.
- CI must stay green (1109 tests baseline). Add a test that asserts the streaming
  contract (e.g. `build_index` never holds more than a batch — inject a counter).

**Repo reminders:** default branch `main`, remote is `github` (not origin).
Branch off `main`, one CI-green PR. `.venv` is Python 3.12 with `[dev]` +
sqlite-vec. Lore is out of Syncthing now — sync via git only; if `.git` shows
ref corruption see the `ai-workstation-syncthing-mirror` memory for the repair
recipe. Stage specific files (autosync may pre-stage others).
