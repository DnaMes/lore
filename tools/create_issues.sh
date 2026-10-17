#!/usr/bin/env bash
# Create issues from docs/ROADMAP.md on Forgejo or GitHub.
#
# Usage:
#   tools/create_issues.sh forgejo                        # Forgejo (Tailscale only)
#   tools/create_issues.sh github <owner> <repo>          # e.g. github DnaMes lore
#
# Requirements:
#   - Forgejo: token at ~/.config/forgejo/token, Tailscale connected
#   - GitHub:  `gh auth status` working, repo already exists

set -euo pipefail

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 forgejo | github <owner> <repo>" >&2
  exit 1
fi

# ── helpers ────────────────────────────────────────────────────────────────

create_label_forgejo() {
  local name="$1" color="$2" desc="$3"
  curl -sf -X POST "${FORGEJO_API}/labels" \
    -H "Authorization: token ${FORGEJO_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${name}\",\"color\":\"${color}\",\"description\":\"${desc}\"}" \
    > /dev/null || true
}

create_label_github() {
  local name="$1" color="$2" desc="$3"
  gh label create "${name}" --color "${color}" --description "${desc}" \
    --repo "${GH_REPO}" 2>/dev/null || true
}

create_issue_forgejo() {
  local title="$1" body="$2" labels="$3"
  local label_ids=()
  IFS=',' read -ra LNAMES <<< "$labels"
  for lname in "${LNAMES[@]}"; do
    lid=$(curl -sf "${FORGEJO_API}/labels" \
      -H "Authorization: token ${FORGEJO_TOKEN}" \
      | python3 -c "import json,sys; labels=json.load(sys.stdin); \
        match=[l['id'] for l in labels if l['name']=='${lname}']; \
        print(match[0] if match else '')" 2>/dev/null || echo "")
    [[ -n "$lid" ]] && label_ids+=("$lid")
  done
  local ids_json
  ids_json=$(printf '%s\n' "${label_ids[@]}" | python3 -c \
    "import sys,json; print(json.dumps([int(x) for x in sys.stdin.read().split() if x]))")
  local title_json body_json
  title_json=$(python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$title")
  body_json=$(python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$body")
  curl -sf -X POST "${FORGEJO_API}/issues" \
    -H "Authorization: token ${FORGEJO_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"title\":${title_json},\"body\":${body_json},\"labels\":${ids_json}}" \
    > /dev/null
  echo "  ✓ $title"
}

create_issue_github() {
  local title="$1" body="$2" labels="$3"
  gh issue create \
    --repo "${GH_REPO}" \
    --title "${title}" \
    --body "${body}" \
    --label "${labels}" 2>/dev/null
  echo "  ✓ $title"
}

create_label() { "create_label_${TARGET}" "$@"; }
create_issue() { "create_issue_${TARGET}" "$@"; }

# ── target setup ───────────────────────────────────────────────────────────

if [[ "$TARGET" == "forgejo" ]]; then
  FORGEJO_TOKEN=$(cat ~/.config/forgejo/token 2>/dev/null | tr -d '\n')
  if [[ -z "$FORGEJO_TOKEN" ]]; then
    echo "Error: Forgejo token not found at ~/.config/forgejo/token" >&2; exit 1
  fi
  FORGEJO_API="http://100.119.46.15:3000/api/v1/repos/erdna/ai-history"
  if ! curl -sf "${FORGEJO_API}" -H "Authorization: token ${FORGEJO_TOKEN}" > /dev/null; then
    echo "Error: Cannot reach Forgejo. Are you on Tailscale?" >&2; exit 1
  fi
elif [[ "$TARGET" == "github" ]]; then
  GH_OWNER="${2:-}"; GH_REPO_NAME="${3:-}"
  if [[ -z "$GH_OWNER" || -z "$GH_REPO_NAME" ]]; then
    echo "Usage: $0 github <owner> <repo>" >&2; exit 1
  fi
  GH_REPO="${GH_OWNER}/${GH_REPO_NAME}"
  if ! gh auth status &>/dev/null; then
    echo "Error: gh not authenticated. Run: gh auth login" >&2; exit 1
  fi
else
  echo "Unknown target: $TARGET (use 'forgejo' or 'github')" >&2; exit 1
fi

# ── labels ─────────────────────────────────────────────────────────────────
echo "Creating labels..."
create_label "p0"          "d73a4a" "Blocker — must fix before release"
create_label "p1"          "e4e669" "High priority — first month"
create_label "p2"          "0075ca" "Medium — next quarter"
create_label "p3"          "cfd3d7" "Low — strategic / v3"
create_label "bug"         "d73a4a" "Something is broken"
create_label "security"    "b60205" "Security vulnerability or hardening"
create_label "performance" "0e8a16" "Speed or resource usage improvement"
create_label "enhancement" "a2eeef" "New feature or improvement"
create_label "architecture" "1d76db" "Internal structure / tech debt"
create_label "qa"          "f9d0c4" "Test coverage or CI improvements"
create_label "docs"        "0075ca" "Documentation"
create_label "extractor"   "e4e669" "AI tool extractor (new or existing)"
create_label "competitor"  "d4c5f9" "Feature parity with competing tools"
echo "Labels done."
echo "Creating issues..."

# ── P0 still open ──────────────────────────────────────────────────────────
create_issue "[#1b] Rename _new suffix entry-point modules" \
"## Problem
\`ai_history_mcp_new.py\` and \`ai_history_web_new.py\` are root-level legacy artifacts that will appear on PyPI as top-level modules.

## Action
- Move \`ai_history_mcp_new.py\` → \`lore/cli/mcp.py\`
- Move \`ai_history_web_new.py\` → \`lore/cli/web.py\`
- Update \`pyproject.toml\` console scripts and \`py-modules\` list
- Update \`Dockerfile CMD\`

**Estimated effort:** 1h" "p0,architecture"

create_issue "[#1d] safe_copy_db leaks temp SQLite copies in /tmp" \
"## Problem
\`utils/paths.py:64-92\` copies SQLite databases to \`/tmp\` but never deletes them. Every Sync leaves \`*.vscdb\`, \`*.sqlite\`, plus WAL/SHM files behind.

## Action
Wrap in a context manager with \`finally\` cleanup:
\`\`\`python
@contextmanager
def safe_copy_db(src: Path) -> Generator[Path, None, None]:
    tmp = Path(tempfile.mktemp(suffix=src.suffix))
    try:
        shutil.copy2(src, tmp)
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)
        for ext in ('-wal', '-shm'):
            (tmp.parent / (tmp.name + ext)).unlink(missing_ok=True)
\`\`\`

**Estimated effort:** 30m" "p0,bug"

create_issue "[#1e] Extractor exceptions silently swallowed" \
"## Problem
When an extractor raises during a Sync job it is caught at \`logger.debug\` (\`web_data.py:213\`). Users see zero indication that e.g. the Cursor extractor failed.

## Action
- Surface exception type + message in job result metadata (\`errors\` key)
- Show per-extractor error badges in Sync status UI
- Upgrade to \`logger.warning\` with traceback for unexpected exceptions

**Estimated effort:** 2h" "p0,bug"

create_issue "[#PyPI] Publish to PyPI + pyproject.toml cleanup" \
"## Checklist
- [ ] Bump \`requires-python\` to \`>=3.11\`
- [ ] Add author email to \`authors\`
- [ ] Add \`[project.urls]\` (Homepage, Source, Tracker, Changelog)
- [ ] Add \`CHANGELOG.md\` (Keep-a-Changelog format)
- [ ] Test: \`python -m build && twine upload --repository testpypi dist/*\`
- [ ] Verify \`pip install lore\` works in a clean venv
- [ ] Create GitHub release tagged \`v2.0.0\`

**Estimated effort:** 2h" "p0,docs"

create_issue "[#README] Add screenshot + quickstart to README" \
"## Problem
No screenshot or GIF. Quickstart is buried. No badges.

## Action
- Add screenshot (or \`vhs\`/\`asciinema\` GIF) near top of README
- Reorder: screenshot → one-liner install → 3-command quickstart → features
- Add CI badge and PyPI version badge
- Set GitHub repo topics: python, ai, developer-tools, claude, mcp, history, cursor, gemini

**Estimated effort:** 1h" "p0,docs"

create_issue "[#COC] Add CODE_OF_CONDUCT.md" \
"Add \`CODE_OF_CONDUCT.md\` using Contributor Covenant 2.1 template. Required for GitHub community health score." "p0,docs"

# ── P1 security ────────────────────────────────────────────────────────────
create_issue "[#7] Replace CSP unsafe-inline with nonces" \
"## Problem
\`web.py:496-497\` sets \`Content-Security-Policy: script-src 'self' 'unsafe-inline'\`. This defeats CSP entirely.

## Action
- Generate a per-request nonce in \`render()\`
- Add \`nonce=\"{nonce}\"\` to every inline \`<script>\` and \`<style>\` in \`web_templates.py\`
- Change CSP to \`script-src 'self' 'nonce-{nonce}'\`

**Estimated effort:** 4h (lots of inline scripts)" "p1,security"

create_issue "[#8] Only trust X-Forwarded-For when TRUSTED_PROXY env is set" \
"\`web_utils.py:75\` reads \`X-Forwarded-For\` unconditionally. Any client can spoof this and bypass rate limits.

**Fix:** Only read \`X-Forwarded-For\` when \`TRUSTED_PROXY=1\` env var is set. Otherwise use \`request.remote_addr\`.

**Estimated effort:** 30m" "p1,security"

create_issue "[#9] Warn when SECRET_KEY is ephemeral in production" \
"\`web.py:143\` generates a random key on every restart. In production this invalidates all sessions on restart.

**Fix:** Log WARNING when \`FLASK_SECRET_KEY\` is unset. Raise \`RuntimeError\` when \`FLASK_ENV=production\` and key is missing. Document in \`.env.example\`.

**Estimated effort:** 30m" "p1,security"

create_issue "[#10] Add pip-audit + bandit to CI" \
"Add dependency vulnerability scanning and code security scanning to \`.github/workflows/ci.yml\`:
\`\`\`yaml
- run: pip-audit
- run: bandit -r lore/ -ll
\`\`\`

**Estimated effort:** 1h" "p1,security,qa"

create_issue "[#15] Tighten validate_session_id regex" \
"Current regex allows \`:\`, \`*\`, \`?\`, spaces — characters that could construct path-traversal payloads.

**Fix:** \`re.match(r'^[A-Za-z0-9_.\-]{1,128}$', value)\`

**Estimated effort:** 30m" "p1,security"

create_issue "[#15a] Migrate from bleach to nh3" \
"\`bleach\` was archived by Mozilla in 2024. \`nh3\` is the Rust-based successor with identical API.

**Fix:** Replace \`bleach.clean()\` calls with \`nh3.clean()\`. Remove \`bleach\` and \`webencodings\` from deps.

**Estimated effort:** 2h" "p1,security"

create_issue "[#15e] Switch pre-commit to ruff (replace black+isort+flake8)" \
"Pre-commit uses black+isort+flake8 but project rules specify ruff. Two toolchains is confusing.

**Fix:** Replace with \`ruff format\` + \`ruff check --fix\`. Add \`[tool.ruff]\` to \`pyproject.toml\`.

**Estimated effort:** 1h" "p1,architecture"

# ── P1 performance ─────────────────────────────────────────────────────────
create_issue "[#16] Incremental index sync — headline UX improvement" \
"## Problem
Every Sync rebuilds the full index from scratch: re-parses all 1,672+ sessions (~30s).

## Action
- Store \`(path, mtime, size)\` fingerprint per session in SQLite
- On Sync: only re-parse sessions where mtime/size changed
- Expected: O(changed) instead of O(all) — typical sync <1s

**Estimated effort:** 1 day
**Impact:** ⭐⭐⭐⭐⭐ — most impactful UX win available" "p1,performance,enhancement"

create_issue "[#17] Split index.json to reduce 19 MB dashboard payload" \
"## Problem
\`load_index()\` returns 19 MB on every page load. Dashboard uses only 7 of ~15 fields. \`search_text\` and \`keywords\` account for ~75% of the size.

## Action
Split into \`index_meta.json\` (always loaded) + \`index_search.json\` (on-demand).

**Estimated effort:** 2h" "p1,performance"

create_issue "[#19] Vendor Tailwind CSS + highlight.js (restore offline use)" \
"## Problem
CDN-loaded assets break air-gapped installations (documented use case). Also blocks SRI hashes (#12).

## Action
- Pin + download both libraries
- Serve from \`/static/\` via Flask
- Update CSP to remove CDN origins

**Estimated effort:** 4h" "p1,performance,security"

# ── P1 QA ──────────────────────────────────────────────────────────────────
create_issue "[#26] Parametrized contract tests for all 11 extractors" \
"Add \`@pytest.mark.parametrize('extractor_cls', ALL_EXTRACTORS)\` asserting Tool/Role enums, \`is_available()\`, iterator return types. Currently only Claude/Gemini/Codex are checked.

**Estimated effort:** 2h" "p1,qa"

create_issue "[#28] API v1 route contract tests" \
"Lock the public \`/api/v1/*\` surface. Extend \`tests/test_api_and_mcp.py\` to cover:
- \`/api/v1/search\`
- \`/api/v1/threads\`
- Error responses (404, 400, 422)
- Pagination / limit / filter params

**Estimated effort:** 3h" "p1,qa"

create_issue "[#29] pytest-cov + 80% coverage gate" \
"Add \`pytest --cov=lore --cov-report=html --cov-fail-under=80\` to CI. Publish HTML artifact. Current estimated coverage: ~45%.

**Estimated effort:** 1h setup + work to reach 80%" "p1,qa"

# ── P1-P2 features / competitor gaps ───────────────────────────────────────
create_issue "[#34] Token cost dashboard (competitor gap — top requested feature)" \
"## Competitor gap
\`jhlee0409/claude-code-history-viewer\` (1.2k★) ships token stats and cost calculation. This is the #1 missing feature in this space.

## Action
- Extract token counts from \`UnifiedMessage.tokens\` into \`UnifiedSession.total_tokens\`
- Add cost table (Claude/GPT/Gemini pricing, configurable)
- Web UI: session list shows token count; detail page shows cost breakdown
- Projects page: aggregate cost per project per month

**Impact:** ⭐⭐⭐⭐⭐
**Estimated effort:** 1 week" "p1,enhancement,competitor"

create_issue "[#35] Semantic search via local embeddings (unique differentiator)" \
"## Opportunity
No competitor has shipped local semantic search. 'Find sessions where I debugged SQLite locking' — FTS completely misses this.

## Tech stack
- \`sqlite-vec\` (pure Python SQLite vector extension)
- \`nomic-embed-text\` via Ollama (free, local, Apache license)
- Hybrid BM25 + cosine via Reciprocal Rank Fusion

## Action
- \`lore embed\` CLI command to build embeddings
- Hybrid mode on \`/search\` when embeddings available
- MCP \`search_history\` gains \`semantic=true\` param

**Impact:** ⭐⭐⭐⭐⭐ — nobody else has this
**Estimated effort:** 1 week" "p2,enhancement"

create_issue "[#51] Aider extractor" \
"## Competitor gap
\`jhlee0409/CCHV\` already supports Aider. Aider stores sessions as Markdown in \`~/.aider/\`.

## Action
- Add \`lore/extractors/aider.py\`
- Parse Aider Markdown conversation format
- Add to \`get_all_extractors()\` factory
- Contract test

**Estimated effort:** 4h" "p2,enhancement,extractor,competitor"

create_issue "[#52] File watcher / auto-sync daemon" \
"## Competitor gap
SpecStory ships \`specstory watch\` — indexes sessions on JSONL append. Users currently must click Sync manually.

## Action
- \`lore watch\` CLI command using \`watchdog\` library
- Watch Claude/Cursor/etc. dirs for JSONL changes
- On change: trigger incremental index update (#16) for affected session only
- Optional systemd user service template

**Estimated effort:** 1 day" "p2,enhancement,competitor"

create_issue "[#53] Scoped MCP search (user_only / assistant_only / tool_results)" \
"## Competitor gap
\`claude-historian-mcp\` exposes 11 search scopes. Useful for agents that want only commands run or only user prompts.

## Action
Add \`scope\` param to MCP \`search_history\` tool: \`all\` (default), \`user_only\`, \`assistant_only\`, \`tool_results\`.

**Estimated effort:** 2h" "p2,enhancement,competitor"

create_issue "[#54] Session resume button in web UI" \
"## Competitor gap
\`raine/claude-history\` has \`--resume\` to reopen a Claude Code session. The web UI should have one-click resume.

## Action
- 'Resume in Claude' button on session detail (Claude Code sessions only)
- Calls \`claude --resume <session_id>\` via new \`/api/v1/sessions/<id>/resume\` POST route
- Only show when \`claude\` CLI is in PATH

**Estimated effort:** 4h" "p2,enhancement,competitor"

create_issue "[#55] Git commit linking — tag sessions with active branch/SHA" \
"## Competitor gap
\`cursor-chat-history-mcp\` links conversations to the git commit they occurred during. Enables 'what was I building during this session?'.

## Action
- During indexing: run \`git -C <project_path> log --format='%H %D' -1\`
- Store \`git_branch\` + \`git_sha\` in \`UnifiedSession\` and index
- Show on session detail page
- Expose in MCP \`get_session\` response

**Estimated effort:** 1 day" "p2,enhancement,competitor"

create_issue "[#43] lore digest — weekly summary command" \
"A \`lore digest\` CLI command printing a weekly summary: sessions by day, breakdown by tool, top 5 projects, total tokens/cost.

Low effort, high habit-forming value.

**Action:** Add \`digest\` subcommand to \`ai_history_cli.py\`. Reuse index data. Add \`--since 7d\` / \`--format markdown\` flags.

**Estimated effort:** 4h" "p2,enhancement"

create_issue "[#36] Shareable static HTML export per session" \
"Export a session as a self-contained HTML file (no external deps) for sharing via email/Slack/GitHub. SpecStory does cloud links — we do offline-first.

**Action:** \`lore export --format html <session_id>\`. Inline CSS + transcript. Web UI 'Export as HTML' button.

**Estimated effort:** 1 day" "p2,enhancement"

create_issue "[#41] MCP-over-HTTP transport (streamable-http)" \
"The MCP server only runs over stdio. Cursor and other hosts that can't spawn local processes need HTTP transport.

**Action:** Add \`--transport streamable-http\` flag. Implement MCP streamable-HTTP (POST /mcp, SSE response). Document in README + API_REFERENCE.md.

**Estimated effort:** 1 day" "p2,enhancement"

# ── P2 architecture ────────────────────────────────────────────────────────
create_issue "[#25c] Move HTML from web_templates.py to Jinja2 template files" \
"\`web_templates.py\` is 2,036 LOC of HTML/JS/CSS in Python strings. \`web.py:142\` already sets \`template_folder=\` but it's unused.

**Action:** Extract each template to \`lore/templates/*.html\`. Use \`render_template()\`. Build Jinja2 \`Environment\` once at startup.

**Estimated effort:** 1 day (mechanical, needs good test coverage first)" "p2,architecture"

create_issue "[#25b] Decompose mcp.create_server() — 445 LOC" \
"\`create_server()\` defines all 7 tool handlers inline as closures. Hard to test in isolation.

**Action:** Extract each handler to a top-level async function. Keep \`create_server()\` as thin registration.

**Estimated effort:** 2h" "p2,architecture"

create_issue "[#44] Single SQLite source of truth (v3 milestone)" \
"## Problem
Index lives in both \`index.json\` (19 MB) and \`index.sqlite\` (FTS). Every write touches both.

## Action (v3 milestone)
- Make SQLite the single store: sessions table + FTS5 virtual table
- Drop \`index.json\`
- \`load_index()\` queries SQLite; \`IndexBuilder\` writes only SQLite
- Eliminates 19 MB JSON parse on every page load

**Requires:** #16 (incremental sync) first
**Estimated effort:** 3-5 days" "p3,architecture"

echo ""
echo "Done. All issues created on ${TARGET}."
