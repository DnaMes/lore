# Plan — Hybrid search over the session archive (#56, remaining item)

Status: **design approved 2026-07-01** — sqlite-vec + per-session vector + RRF fusion.
Validated: sqlite-vec v0.1.9 loads and runs KNN (`float[384]`) in this env
(`enable_load_extension: True`, sqlite 3.53.3). fastembed 0.8.0 already installed;
model `BAAI/bge-small-en-v1.5` (384-dim) already used for memory.

## Goal

Add semantic retrieval to the **session** archive and fuse it with the existing
v2 FTS5 keyword search into one ranked result — so `search_history` / web
`/api/search` / `lore search` return keyword **and** meaning-based matches.
Today embeddings are wired to *memory only* (`memory_embeddings`, `_semantic_recall`);
sessions have no vector path at all.

## Design decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Vector store | **sqlite-vec** `vec0` virtual table in `index_v2.sqlite` | No new service (honors "no Postgres/Redis"), real KNN in SQL, optional dep w/ graceful fallback |
| Granularity | **Per-session** (1 vector) — Phase 1 | ~1513 vectors, tiny (~2 MB), fast build; message-level deferred |
| Fusion | **RRF** `Σ 1/(k+rank)`, k=60 | Robust, no score normalization, no α-tuning |
| Model | `BAAI/bge-small-en-v1.5` (reuse memory's) | Already installed, 384-dim, CPU-only |
| Distance cutoff | Drop vec hits with **L2 distance > 0.88** before fusion (`LORE_SEMANTIC_MAX_DISTANCE`) | Measured on a labelled pair set, see [Distance cutoff](#distance-cutoff-92) |
| Fallback | Missing sqlite-vec **or** fastembed → FTS-only, no error | Same pattern as `embeddings_available()` |

## Non-goals (Phase 1)

- Message-/chunk-level embeddings (fills reserved `entity_type='message'` FTS later).
- Re-embedding on every reload — embed on index build, incremental only.
- Tuning α weights (RRF avoids it).

## Architecture

```
IndexBuilder.build_index()  →  writer.write_sessions()  →  [NEW] embed_sessions()
                                                              ↓
index_v2.sqlite:  search_index (FTS5)   [exists]
                  session_embeddings (vec0 float[384])  [NEW, migration 13]

search path (services/index.py::search_index):
   fts_hits  = storage.search_sessions(...)          [exists, v2 FTS5]
   vec_hits  = storage.semantic_search_sessions(...)  [NEW, sqlite-vec KNN]
   fused     = rrf_merge(fts_hits, vec_hits)          [NEW]
```

## Work breakdown (each = 1 CI-green PR, branch off `main`)

### PR 1 — vector store foundation ✅ (branch `feat/hybrid-search-vec-foundation`)
- Optional dep: `pyproject.toml` `semantic = ["fastembed>=0.3", "sqlite-vec>=0.1.9"]`.
- `storage/embeddings.py`: add `sqlite_vec_available()` guard + `load_vec(conn)` helper
  (enable_load_extension → `sqlite_vec.load` → disable). Reuse existing `embed_text`,
  `pack_vector`/`unpack_vector`.
- Migration 13 in `storage/schema.py`:
  `CREATE VIRTUAL TABLE session_embeddings USING vec0(session_id TEXT PRIMARY KEY, model TEXT, embedding float[384])`
  — guarded: skip cleanly if the extension can't load (record migration anyway so it's not retried destructively; document that vec rows are rebuilt, not migrated).
- Tests: extension loads, table creates, insert+KNN roundtrip; all skip if unavailable.

### PR 2 — populate session vectors ✅ (branch `feat/hybrid-search-populate-vectors`)
- `storage/embeddings.py` (or new `storage/session_vectors.py`): `embed_sessions(conn, sessions)`
  — one vector per session from the same text `writer.py` feeds FTS (title + message
  bodies, truncated to model's max tokens). Best-effort, mirrors `memory._store_embedding`.
- Wire into `writer.write_sessions` **after** the FTS write, inside the same
  transaction guard but non-fatal on embed failure (a bad embed never blocks the index).
- Incremental: only (re)embed sessions whose row changed — full replace is fine for
  Phase 1 (1513 rows), but key on `session_id` with `ON CONFLICT DO UPDATE`.
- Tests: after `write_sessions`, N sessions → N vec rows; fallback path writes 0 and doesn't raise.

### PR 3 — semantic search + RRF fusion ✅ (branch `feat/hybrid-search-rrf-fusion`)
- `storage/search.py`: `semantic_search_sessions(output_dir, query, *, tool, project, limit)`
  — embed query, KNN over `session_embeddings`, join `sessions`, apply tool/project
  filter, return the same `[{"session": dict, "score": float}]` shape as `search_sessions`.
- New `storage/fusion.py`: `rrf_merge(*ranked_lists, k=60, limit=50)` — pure, unit-tested.
- `services/index.py::search_index`: when v2 + both backends available, run FTS + semantic,
  fuse with RRF; else current behavior unchanged. Add `LORE_HYBRID_SEARCH` env
  (default on when available) so it can be disabled.
- Tests: fusion ordering; a query that only semantic finds surfaces; FTS-only fallback intact.

### PR 4 — surface + consistency ✅ (branch `feat/hybrid-search-surface`)
- **Fix CLI drift** (the actual fix): `cmd_search` bypassed the v2 router and hit
  legacy `SearchEngine` directly → now routes through `services/index.py::search_index`,
  so CLI, web, MCP share one search path (and the CLI gets hybrid for free). Verified
  live: `lore search` returns RRF-scored results from the v2 router.
- MCP `search_history` / web `/api/search`: **no change** — they already call the router,
  so hybrid is transparently active. The optional `mode` arg was **dropped** (YAGNI):
  hybrid is default-on via `LORE_HYBRID_SEARCH`, a per-request override wasn't worth the
  extra API surface + a new router param.
- Docs: CLAUDE.md architecture section now documents the hybrid search data flow.

**Known limitation (Phase-2):** semantic KNN has no distance cutoff, so a hybrid query
with zero keyword matches still returns nearest neighbours. Acceptable for Phase 1;
a relevance threshold on vector distance is the fix.

## Distance cutoff (#92)

Neighbours whose distance is too large are padding, not matches: RRF would happily rank a
session that has nothing to do with the query. `semantic_search_sessions` therefore drops every
hit with `distance > semantic_max_distance()` **before** the results reach `rrf_merge`.

The metric is sqlite-vec's default on `session_embeddings` (vec0, no `distance_metric`): **L2
over unit-normalised vectors**, i.e. `distance = sqrt(2 - 2 * cosine)` — 0 means identical,
`sqrt(2)` ≈ 1.414 means orthogonal.

Measured 2026-10-14 on `docs/research/2026-10-14-semantic-cutoff-labelled-pairs.json`
(28 labelled session-body/query pairs, real bge-small, vector norm mean 1.0000):

| label | n | min | median | p95 / p05 | max |
| --- | --- | --- | --- | --- | --- |
| related (L2, lower = closer) | 14 | 0.610 | 0.690 | 0.777 (p95) | 0.794 |
| unrelated (L2) | 14 | 0.980 | 1.023 | 0.986 (p05) | 1.104 |

The groups are separated (related max 0.794 < unrelated min 0.980), and **0.88** is the
midpoint of related-p95 (0.777) and unrelated-p05 (0.986) → 0.881, rounded to 0.88. On this set
it keeps 14/14 related and drops 14/14 unrelated, with margin on both sides. Reproduce:

```bash
python tools/calibrate_semantic_cutoff.py        # distribution + recommended cutoff
python tools/calibrate_semantic_cutoff.py --json # machine-readable
```

| Variable | Effect |
| --- | --- |
| `LORE_SEMANTIC_MAX_DISTANCE=0.5` | stricter cutoff (only close neighbours) |
| `LORE_SEMANTIC_MAX_DISTANCE=0` | disable the cutoff entirely (everything KNN returns is fused) |
| unset or unparsable | the calibrated default `0.88` — a bad value never breaks search |

Keyword hits are never cut by this (FTS has its own ranking); the cutoff only stops the vector
pass from *adding* sessions the keyword pass would not have surfaced.

## Risks / watch-items

- **Loadable extensions can be disabled** in some Python sqlite builds. Verified OK here;
  guard + fallback means "off" degrades to FTS-only, never crashes. Document in HANDOFF.
- **Model max tokens**: bge-small truncates ~512 tokens; per-session text far exceeds that.
  Phase-1 accepts coarse (title + head). Message-level (Phase 2) is the real fix.
- **Index staleness**: vec table lives in `index_v2.sqlite`, already staleness-guarded by
  `reader.py`. Embed on build keeps it in lockstep with FTS.
- **`.venv` shebang broken** (points at old `ai-history` path, Rename-Altlast) — unrelated
  cleanup, but rebuild the venv before shipping so `[dev]`/`[semantic]` installs are clean.

## Verification (per Core Rule 1 — real output, not "should work")

Each PR ships with: pytest green (3.11/3.12), and for PR 3/4 a live check —
`lore search "<semantic-only phrase>"` returns a session that keyword search misses,
pasted into the PR. Final: MCP `search_history` handshake with `mode=hybrid` over the
real `~/.lore` archive.
