<!-- Research report, agent-generated on 2026-10-14 for issue #197 follow-up work.
     Facts marked NV / UNVERIFIED were not verified by the author. Star counts and
     dates are a snapshot. Local-machine observations describe one developer machine. -->

# Open-source landscape for Lore (snapshot 2026-10-14)

Stars, last push and license come from `gh api repos/...` fetched today. Feature claims come from each project's README or docs; I ran none of the tools. No clones were made, and the scratch dir is deleted.

## Ranked table

| # | Repo | Stars | Last push | License | Lang | Ingests | Storage / UI / summarization | Has what Lore lacks |
|---|---|---|---|---|---|---|---|---|
| 1 | kenn-io/agentsview | 6095 | 2026-10-13 | MIT | Go | ~65 agents, plus Claude.ai/ChatGPT/Gemini export zips | SQLite FTS5, opt-in semantic/hybrid via any OpenAI-compatible endpoint, PG/DuckDB/ClickHouse mirrors. Web, desktop, CLI, MCP. LLM "generated insights" and opt-in Recall extraction | Cost tracking, session health scores, SSE live updates |
| 2 | Dicklesworthstone/coding_agent_session_search (cass) | 1176 | 2026-10-13 | MIT + OpenAI/Anthropic rider (GitHub: "other") | Rust | ~33 agents incl. ChatGPT desktop | SQLite source of truth, derived BM25 and MiniLM vectors, RRF hybrid. TUI plus `--robot` JSON. No LLM summary seen | SSH/rsync multi-machine sync, encrypted HTML export, atomic index swap |
| 3 | vshulcz/deja-vu | 1188 | 2026-10-13 | MIT | Go | 41 agents | Own inverted index, "no model, no embeddings". TUI, MCP, hooks. Extractive brief | Auto-recall hooks, secret redaction, reverted-decision marking |
| 4 | jhlee0409/claude-code-history-viewer | 2247 | 2026-10-13 | MIT | TS | 31 assistants incl. Cline, Kiro, Goose, Zed | Desktop app and headless web server. Analytics. Index details not checked | One-click backup archives, skill/subagent analytics |
| 5 | specstoryai/getspecstory | 1352 | 2026-10-13 | Apache-2.0 | Go | ~12 agents incl. Cursor, Copilot, Droid | Markdown in `.specstory/history/`, optional cloud. CLI plus IDE extensions. Separate `lore` skill mines sessions into skills | Cloud chat/share, `specstory run <agent>` wrapper |
| 6 | entireio/cli | 5178 | 2026-10-13 | MIT | Go | Claude Code, Codex, Cursor, Antigravity, Pi | Git refs `refs/entire/checkpoints/*`, commit trailer links. CLI. Configurable summary provider | Commit-linked checkpoints, resume from checkpoint |
| 7 | ccusage/ccusage | 19001 | 2026-10-13 | MIT per LICENSE (API: NOASSERTION) | Rust | 18 sources | Reads files in place, CLI reports. No summarization | Daily/weekly/session cost, 5-hour block view, statusline |
| 8 | jazzyalex/agent-sessions | 901 | 2026-10-13 | MIT | Swift | Codex, Claude, Antigravity, Kimi, Grok, Cline, Cursor, others | Local index, macOS app | Quota-burn tracking, resume in Terminal/iTerm/Warp |
| 9 | mem0ai/openmemory | 98 | 2026-07-29 | MIT | TS | Claude Code, Codex, OpenCode | TUI wizard, `port --from --to` | Previewed, selectable, bidirectional session porting |
| 10 | thedotmack/claude-mem | 100418 | 2026-10-13 | Apache-2.0 | TS | Claude Code, OpenCode, Pi, Antigravity, others via hooks | Worker plus web viewer. LLM observer writes semantic summaries. Cloud tier | Progressive disclosure, `<private>` tags, citations |
| 11 | basicmachines-co/basic-memory | 4165 | 2026-10-12 | AGPL-3.0 | Python | MCP clients | Markdown files, local index, knowledge graph (observations and wikilinks). Cloud optional | Human-editable memory files, tool behavior hints |
| 12 | tigerless-labs/agent-memory | 3398 | 2026-10-13 | MIT | Python | Claude Code, Codex, Muse | Markdown source of truth, disposable SQLite FTS5 cache, RRF | L0/outline/full recall levels, sleep-time consolidation |
| 13 | vectorize-io/hindsight | 49235 | 2026-10-13 | MIT | Python | Library/server | Postgres server, LLM-driven retain/recall/reflect, benchmarks | Mental models, reflect operation |
| 14 | harbor-framework/harbor (ATIF-v1.7) | 6079 | 2026-10-13 | Apache-2.0 | Python | Trajectory interchange format | JSON spec, RFC 0001, `harbor-atif2otel` converter | Canonical schema |
| 15 | open-telemetry/semantic-conventions-genai | 437 | 2026-10-13 | Apache-2.0 | Python | OTel spans | `invoke_agent`, `gen_ai.*` attributes, status "development" | Standard export target |

**Memory frameworks, for reference.** These are not session archives; they are server frameworks (all Apache-2.0 per `gh`):
- mem0ai/mem0: 67132 stars, Python
- getzep/graphiti: 31720 stars, Python, temporal graph
- topoteretes/cognee: 32196 stars, Python
- letta-ai/letta: 25122 stars, pushed 2026-09-10

**Smaller or quieter projects worth knowing:**
- simonw/claude-code-transcripts: 1700 stars, Apache-2.0, last push 2026-02-12. Publishes HTML/gist transcripts. Quiet but not dead.
- daaain/claude-code-log: 1242 stars, MIT. JSONL to HTML/Markdown.
- es617/claude-replay: 844 stars, MIT. Animated replay of sessions from several agents.
- meridianlabs-ai/inspect_scout: 80 stars, MIT. LLM scanners over agent transcripts.
- surenode-ai/skill-extractor: 15 stars, Apache-2.0. Scores candidate skills with utility weighted by outcome, redacts secrets before any model call, and requires human approval.
- liaohch3/claude-tap: 3280 stars, MIT. Proxies API traffic.
- yoavf/ai-sessions-mcp: 31 stars, MIT.

**Closest competitors:** agentsview, cass and deja-vu (rows 1-3). Each does multi-agent session search with MCP and has reached 30-65 agents. Only agentsview also has cost analytics, curated Recall with evidence links and a review queue, a web UI and a desktop app.

## Top 8 ideas worth taking

1. **Per-session token and cost analytics** (agentsview, ccusage). Price from stored token and cache fields using LiteLLM rates with an offline fallback, and add health/outcome heuristics. It is pure local arithmetic on data Lore already ingests. Effort S to M.
2. **Index-time secret redaction plus `<private>` exclusion** (deja-vu, skill-extractor, claude-mem). It replaces keys and tokens with `[redacted:kind]` before they reach FTS, embeddings or exports. It is a prerequisite for shipping a product and for any LLM step. Effort S to M.
3. **Proactive recall via hooks and tiered disclosure** (deja-vu, claude-mem, agent-memory). Inject recall at session start and after command failures. Return a one-line L0 hit list first, then outline, then full text. This saves tokens, and Lore's MCP and relevance floor are already the base. Effort M.
4. **Auto-extracted memory candidates with message-level evidence and an approve queue** (agentsview Recall, skill-extractor). Lore already has candidate status, provenance and feedback, so add an opt-in extractor over sessions that links each claim to supporting messages. Keep a deterministic "no model" mode as the default. Effort M.
5. **Git and staleness linkage** (Entire, deja-vu, agentsview). Link sessions to commits and files, and warn "N files touched by this session changed since". Mark reverted decisions as rejected on recall. It uses local git only. Effort M.
6. **SSH/rsync multi-machine sync with host discovery** (cass, deja-vu). Pull session stores from other hosts over `~/.ssh/config` or Tailscale, keeping per-source provenance. It fits local-first because no cloud is involved. Effort M to L.
7. **ATIF and OTel GenAI export (and import)** (Harbor ATIF-v1.7, OTel genai conventions). Make one canonical transcript schema and emit ATIF JSON and `invoke_agent` spans. Other tools (viewers, eval harnesses, OTel backends) then consume Lore data. Effort S to M. ATIF is the more concrete spec; the OTel conventions are still marked "development".
8. **Native-format cross-agent port with preview picker** (openmemory, deja `handoff`, Entire resume). Write a real native session into the target tool rather than injecting the last N messages as `lore-session switch` does. This is high value but fragile, because formats change monthly. Effort L.

## Where Lore is already ahead

I judged this from Lore's README and git log, not from tests.
- **Agent-authored memory.** `memory_write` / `memory_recall` / `memory_feedback` over MCP give a curated store with kinds (fact, decision, lesson), provenance, 1-5 ratings with a bounded weight, atomic supersession with similar-memory hints and a candidate state. In the others, memory is either derived from sessions (deja-vu, agentsview Recall), generated by an LLM observer (claude-mem) or a separate server (mem0, hindsight).
- **Archive and memory in one store** with hybrid recall and a tunable relevance floor. Others split these into separate products.
- **No account or cloud dependency.** SpecStory, claude-mem and basic-memory all steer toward paid cloud tiers.
- **Threads view, derived Rules, SpecStory-style `lore run` PTY wrapper and a web security probe matrix** (`lore-web-probe`). I found no equivalents among the repos I read.

## Coverage gaps

I checked which tools the leaders support that Lore's README does not list (Lore lists Claude Code, Cursor, Copilot VS Code, Copilot CLI, Gemini CLI, Warp, Codex, OpenCode, Antigravity, Aider).

**Most common (3+ projects):**
- Cline/Roo Code/Kilo Code
- Amp
- Goose
- Crush
- Kimi
- Qwen Code
- Kiro/Amazon Q
- Zed
- Windsurf
- Pi
- OpenClaw/Hermes
- Factory Droid
- OpenHands
- Junie
- Trae
- Continue/PearAI

**Others (1-2 projects each):**
- ForgeCode
- CodeBuddy
- Grok Build
- Devin CLI
- DeepSeek TUI/Harness
- Mistral Vibe
- gptme
- Open Interpreter
- Simon Willison's `llm`
- Claude Cowork
- Visual Studio Copilot
- Cursor Agent transcripts (separate from the IDE source)

**Non-agent chat exports:** Claude.ai, ChatGPT and Gemini Apps export zips (agentsview); ChatGPT desktop (cass).

## Product risks I found

- **`pip install lore` is wrong.** PyPI project `lore` is Instacart's "framework for building and using data science" (v0.8.6, fetched today). Lore's README badges and install line point at that name, so it needs a distinct distribution name.
- **Name collision with SpecStory's "Lore".** It is a skill-forging tool at `getspecstory/lore`, Apache-2.0, "Your sessions are your lore". It targets the same user base. Consider a rebrand check before launch.
- **License traps.** basic-memory is AGPL-3.0, and cass uses a non-OSI rider. Ideas can be taken; do not copy code.
- **Distribution gap.** All top competitors ship a single binary or desktop app (Go/Rust/Swift/Tauri). Lore's Flask/pip install is the weakest part of the product story.

## Not verified
- deja-vu's "97.2% R@5 LongMemEval-S" and "half the tokens" are the author's own self-published claims.
- Storage internals of claude-mem and claude-code-history-viewer.
- Whether cass has LLM summarization (none seen in the README).
- Last-push dates show repos are active, not that they are stable. agentsview and cass both mark their Recall and semantic features experimental.
