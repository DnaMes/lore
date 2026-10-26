# HANDOFF — Lore

Older checkpoints (2026-10-14 backlog Wave 1, 2026-10-16 lore-sync FTS fix) are archived in
`.parachute/handoff-archive/2026-10-26.md`. Read them only on request.

## Goal (2026-10-26 session)

Make Lore **release-ready as v2.4.0 for strangers installing it out of the box**: Codex must agree
(READY), docs must match the code, everything fixed that blocks a clean install. The owner's active
goal: "fix everything until the release is done; agree with Codex again; Pi works the issues in
parallel". Pi (another agent session) shares this checkout: see Gotchas.

## Current progress

State on `origin/main` (= local main) at `c978a29`, all review-gate APPROVE:
- Security: Host allowlist (400) + strict same-origin check (403) with ASCII-only ports 1..65535
  (`lore/interfaces/web_utils.py`), `TRUSTED_PROXY` is now a real boolean (breaking: `0` used to enable).
- Cloud boundary in the library, not only the CLI: `lore/llm/gate.py` (`CloudOptInRequired`,
  `ensure_cloud_allowed`), `get_provider`, every cloud provider's transport, `run_improve`, remote-Ollama
  and gemini-cli title paths. Finished title/stats prompts are redacted (project path, titles).
- Data safety: `lore prune` deletes only exporter-named `.md` files in `projects/<p>/<tool>/` inside the
  archive, never store/control files or symlinks; archive preservation drops nothing implicitly;
  `ignored.json` shape/UTF-8 damage warns once instead of crashing; dedupe compares scopes like recall;
  search excludes pruned ids before the limit; ignored.json changes invalidate the index cache.
- Packaging: SPDX MIT license, `MANIFEST.in` keeps maintainer files out of the sdist, `[dev]` has
  pre-commit and bandit, `watch` extra removed, pip-audit step cleaned.
- Docker: self-contained compose (named volume `lore-data`, uid 10001, `LORE_PORT`, opt-in
  `docker-compose.sources.yml`, Traefik only in `docker-compose.traefik.example.yml`); verified with a real
  container (write, restart, read back, foreign Host -> 400).
- Docs: README rewritten (205 lines), `docs/{INSTALL,CLI,CONFIGURATION,MEMORY,MCP,WEB-UI,DOCKER,PRIVACY,HOWTO,DEPLOYMENT}.md`,
  CHANGELOG `2.4.0` (not tagged yet), old plans moved to `docs/archive/`.
- Test infra (new): `scripts/smoke_install.py` (stdlib, cross-platform, isolated HOME),
  `scripts/install-matrix.sh` (Docker matrix), `.github/workflows/install-matrix.yml` (ubuntu/macOS/windows x
  py3.11/3.13/3.14, semantic, pipx-from-git), `ci.yml` now also tests py3.13.

Evidence (commands run, observed):
- Full suite on our change set in an isolated worktree: 3152 passed / 24 skipped / 0 real failures, 86.75 % cov;
  `ruff check .` + `ruff format --check .` clean (362 files).
- `bash scripts/install-matrix.sh` (JOBS=3, with arm64): **10/10 PASS** (py3.11-3.14, 3.12+semantic, alpine
  musl, arm64 emulated, debian12/ubuntu24/fedora via pipx). Real wheel smoke on py3.11: 48 PASS / 0 FAIL.
- Codex rounds: r1 NOT-READY (7 blockers), r2 PARTIALLY, r3 NOT-READY (narrowed), **r4 READY-WITH-FIXES**;
  the three r4 items (remote-Ollama title gate, direct-provider gate, full-prompt redaction) were fixed in
  `b2d3d73`; a focused r5 Codex check on HEAD has NOT been run yet.

## UNCOMMITTED right now (ours)

- `lore/interfaces/web_utils.py` line ~73: `# nosec B104` on `WILDCARD_BIND_HOSTS` (the constant is matched
  against, never bound; CI `lint/bandit` failed on `1ccccac` because of it). Verified locally:
  `uvx --from 'bandit[toml]==1.9.4' bandit -r lore/ -c pyproject.toml -ll -q` exit 0, ruff clean.
  Needs commit+push via git-ops (explicit path only).

Not ours (Pi, leave alone): `lore/exporters/html.py`, `lore/exporters/markdown.py`,
`lore/exporters/tool_calls.py`, `tests/test_export_canonical.py`, `docs/triage/`.

## Next steps (ordered)

1. git-ops: commit `fix(ci): mark the wildcard bind constant as non-bindable for bandit` for
   `lore/interfaces/web_utils.py` only, push through `review-gate --repo <repo> --sha <sha>`.
2. `gh run list --limit 8` for `c978a29` and the bandit fix: read **Install matrix** (macOS, Windows, py3.14
   jobs), **CI** (py3.13), **Security Scan** (new pip-audit step), **Tests**. Fix real failures; known
   review-gate notes: matrix can pass with zero targets run if every image is skipped; `--worktree` export
   aborts on a deleted tracked file; Windows wheel path unquoted in the workflow.
3. Headless browser check against the real container (NOT VERIFIED yet): `LORE_PORT=5078
   COMPOSE_PROJECT_NAME=lore-e2e docker compose -f docker-compose.yml -f <override with container_name lore-e2e-app> up -d`,
   seed data via `docker exec lore-e2e-app lore ...`, drive it with the Playwright MCP (headless only), check pages,
   console errors, the code-block copy button; screenshots to /tmp; tear down with `down -v --rmi local`.
4. Focused Codex round 5 (`codex exec -s read-only -C <repo> -o /tmp/lore-release/codex5.md "<prompt>"`,
   previous verdicts in `/tmp/lore-release/codex{-verdict,2,3,4}.md`): verify the three r4 fixes and anything
   new in `1ccccac..HEAD`. Release only on READY or when only polish remains.
5. Docs: mention `scripts/install-matrix.sh` / `smoke_install.py` in `CONTRIBUTING.md`; finalise CHANGELOG
   `2.4.0` date; keep `docs/` in sync with any new behaviour.
6. Tag and release (authorised by the owner): git-ops `git tag -a v2.4.0` on the verified tip, push the tag,
   GitHub release notes from the CHANGELOG section; verify `lore --version` from a fresh wheel.
7. The production container `lore-app` (127.0.0.1:5000, bind-mounts ~/.lore) still runs the old image;
   rebuilding it is a production decision, not done.

## Decisions (and why)

- Codex-driven loop: adopt every finding unless it is wrong; separate real blockers from polish (owner delegates
  decisions, never present options).
- Archive preservation: never drop index rows implicitly; `lore prune` is the only way to remove them (an
  unplugged drive must not shrink the archive).
- Cloud gate sits at the transport/factory boundary so library callers cannot bypass the CLI check;
  default-deny for any provider not in `LOCAL_PROVIDERS`, Ollama only counts as local on loopback.
- Verification for macOS/Windows uses GitHub-hosted runners (free for public repos); the homelab servers
  (TrueNAS on-demand, e6230 runners) are deliberately not used.
- Isolated-worktree verification when Pi's work is dirty in the shared tree (see Gotchas).

## What did not work (do not repeat)

- Builders hitting the 900 s hop cap return only a `git diff --stat`; split work and re-verify yourself.
  Several worker reports were wrong or stale (e.g. "workflow not created" when it existed): check files.
- `docker manifest inspect` hangs under Docker Hub's anonymous rate limit: use `docker pull`/`image inspect`.
- Running tests with `PYTHONPATH` set breaks `tests/test_install_from_clone.py` (the clean venv inherits it).
- `sleep N` followed by a read is blocked; wait with `run_in_background` + `until` loops. A loop that `pgrep -f`s
  its own command line never ends.
- `pip show` prints `License-Expression` on new pips: use `importlib.metadata`.
- Pi's tag-filter commit made `cmd_list` read `args.tag`; our Namespace-based tests needed `tag=None`.

## Files changed this session (high level)

`lore/llm/{gate,factory,gemini,jev,ollama,openrouter}.py`, `lore/llm/tasks/stats.py`, `lore/titles/generator.py`,
`lore/interfaces/{web,web_utils}.py`, `lore/storage/{reader,search,schema}.py`, `lore/services/{extraction,index,improve,memory_dedupe,summarize_cli}.py`,
`lore/exporters/index.py`, `lore_cli.py`, `pyproject.toml`, `MANIFEST.in`, `Dockerfile`, `docker-compose*.yml`,
`.dockerignore`, `.env.example`, `scripts/{smoke_install.py,install-matrix.sh}`, `.github/workflows/{ci,install-matrix,security}.yml`,
README + docs (see above), many tests (`tests/test_*cloud*`, `*redaction*`, `*prune*`, `*ignored*`, `*smoke*`, `*host_allowlist*`).

## Gotchas

- **Pi shares this checkout** and commits in it. Never `git add .`/`-A`, stash, reset or checkout there; stage explicit
  paths; main-thread git writes are blocked (use git-ops). `review-gate` pushes `sha:refs/heads/main` to the remote as a
  fast-forward only and never touches a working tree; to isolate, make a worktree under `~/.worktrees/lore/<name>`
  (a post-merge hook removes merged worktrees) and run the gate with `--repo <worktree>`.
- Disk is ~90 % full: `docker image prune -af --filter label=lore-matrix=1` after matrix runs.
- A periodic reader touches `~/.lore/index_v2.sqlite-wal/-shm` every 60 s (not our scripts); the live archive is
  `~/.lore` (3 GB DB): tests and scripts must always isolate HOME.
- Quality gates: full suite needs `nice -n 10 .venv/bin/python -m pytest tests -q`; subsets need `-o addopts=""`
  (global 80 % coverage gate).

generated by context-parachute v1.4.1
