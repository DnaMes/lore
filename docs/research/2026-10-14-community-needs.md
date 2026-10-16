<!-- Research report, agent-generated on 2026-10-14 for issue #197 follow-up work.
     Facts marked NV / UNVERIFIED were not verified by the author. Star counts and
     dates are a snapshot. Local-machine observations describe one developer machine. -->

# Lore community research report

Research date: 2026-10-14. Reddit was not directly readable: WebFetch and WebSearch block reddit.com, and Reddit returned 403 to curl. I did not use the pullpush mirror because it refuses agents. Reddit evidence therefore comes from search-engine snippets (titles and opening lines of original posts, not comment trees). Reddit ages are the search tool's relative ages as of today. HN and GitHub dates are exact.

## 1. What people do today

- **Single-tool search and resume CLIs are the common answer.**
  - On HN, ccrider (2026-01-06) drew a "does /rename already solve this?" reply. The author answered that renaming only helps "if you remember to rename during the session".
  - search-sessions (2026-02-23) is a Rust binary over "1.6GB of JSONL" with "no database, no indexing step".
- **People write their own cross-tool hooks.**
  - An HN commenter (HN 46500801, 2026-01-05) dumps each session on compaction from "five machines" into DuckDB full-text search.
  - an HN commenter (HN 48828332, 2026-07-08) normalizes "Claude Code and Codex transcripts into one DB".
- **A cross-tool niche exists on Reddit.**
  - r/codex: "migrate session history between Claude Code, Codex, and Cursor CLI" (~7 months ago): https://www.reddit.com/r/codex/comments/1ru935f/
  - r/ClaudeCode: Rust CLI "to search and share context across your Cursor and Claude Code sessions" (~6 months ago): https://www.reddit.com/r/ClaudeCode/comments/1rz9qlx/
  - r/ClaudeCode: a skill so agents can cross-search Claude and Codex history: https://www.reddit.com/r/ClaudeCode/comments/1u3iqm5/
- **Sharing is an HTML export.** an HN commenter (HN, 2026-03-06) names simonw's claude-code-transcripts, which "only works with Claude Code". He calls coding_agent_session_search's TUI "kind of clunky" and notes it "only exports one session at a time".
- **Handoff files are the default workaround.**
  - HN 49304847 (2026-08-14): a portable /handoff file preferred over /compact or /clear.
  - r/ClaudeCode "Handover between CC and Codex" (~5 months ago): "Once CC quota finish I move to Codex": https://www.reddit.com/r/ClaudeCode/comments/1su7wxx/

## 2. Pain points, ranked by evidence

1. **Silent deletion.**
   - Claude Code's 30-day `cleanupPeriodDays` default produced a cluster of Reddit PSAs 1 to 6 months ago, e.g. https://www.reddit.com/r/ClaudeCode/comments/1sgtu5l/ and https://www.reddit.com/r/ClaudeCode/comments/1uyyxxm/ ("I built a local search engine for mine before they vanished").
   - GitHub issue #59248 (2026-05-14) is titled "Silent retention cleanup deletes session transcripts with no warning, opt-in, or recovery", with 41 thumbs-up.
   - Related issues: #62272 (JSONLs deleted despite a high setting) and #78659 (reinstalling Desktop wipes session history).
   - HN an HN commenter (2026-07-30): users must "rescue that log from their own client".
2. **Cursor chat loss.** r/cursor has recurring threads: moving or renaming a folder, updates, crashes ("third time" a long chat disappeared, ~9 months ago): https://www.reddit.com/r/cursor/comments/1pxxdrv/ and https://www.reddit.com/r/cursor/comments/1ju5q1d/.
3. **Compaction and handoff loss.**
   - "Claude chat history too long and handover not perfect... new chat is around 80% there": https://www.reddit.com/r/ClaudeAI/comments/1ufxh84/
   - GitHub #27242 (2026-02-20): "No working mechanism to review previous context after compaction".
4. **Finding "that conversation".**
   - r/ClaudeCode, ~2 months ago, 57 comments (via a search-result sidebar): "We went back and forth on payload size limits, agreed on an approach": https://www.reddit.com/r/ClaudeCode/comments/1voffc7/
   - "How do you carry *decisions* (not chat history)": "Keeping history is mostly solved now... What still..." (~3 weeks ago): https://www.reddit.com/r/ClaudeCode/comments/1wmoost/
5. **Token and cost visibility across tools.**
   - r/ChatGPTCoding: "Is there a Claude Code Usage but for Codex?" (1 year ago): https://www.reddit.com/r/ChatGPTCoding/comments/1n7mhlt/
   - A 15-engineer team "burning through Claude / Cursor credits" (~6 months ago): https://www.reddit.com/r/claude/comments/1sgy3s5/
6. **Secrets in transcripts.**
   - HN an HN commenter (2026-05-19): Claude read .env secrets and "session transcripts store them locally".
   - HN thread 47644190 (2026-04-04): "Claude Code caches unredacted session history and secrets in plaintext".
7. **Team and compliance.** AFK (HN 49226856, 2026-08-08) is aimed at teams, with shared sessions. r/ClaudeAI has an open-source "audit trail for AI coding" project (~5 months ago): https://www.reddit.com/r/ClaudeAI/comments/1snssp8/. Evidence here is thin.

## 3. Summaries

- **Wanted content.**
  - Handoff = "what changed, what is still open, what the next agent should check first" (r/ClaudeAI, ~4 months ago): https://www.reddit.com/r/ClaudeAI/comments/1tjzqrx/
  - Another thread: "Git versions the what and drops the why" (r/LLMDevs, ~4 months ago): https://www.reddit.com/r/LLMDevs/comments/1that1b/
  - "Commit 'Why', Decision Logs, and Diff Summaries" (r/ClaudeWorkflows).
- **Complaints about auto-summaries.**
  - Compaction "tries to summarize the entire conversation at once into a single..." (r/ClaudeAI, ~3 months ago): https://www.reddit.com/r/ClaudeAI/comments/1urujbz/
  - "Claude's default summarization loses five specific things": https://www.reddit.com/r/ClaudeAI/comments/1r06z4r/
  - After compaction it "loses track... which files it already read": https://www.reddit.com/r/ClaudeCode/comments/1qzo3xj/
- **Raw plus summary.**
  - an HN commenter (HN, 2026-01-05) gives the MCP both raw history and AI summaries so it can "answer questions about past decisions".
  - an HN commenter (HN, 2026-09-17) keeps "full tool inputs/outputs" because "tool_results help the agent figure out what is being referred too".
  - Compactdiff (HN, 2026-08-08) exists to show what compaction dropped.
- **Per-project vs daily.** Per-session summaries are best evidenced. Daily digests appear only as personal automations (r/ClaudeAI, ~8 months ago): https://www.reddit.com/r/ClaudeAI/comments/1qm8tvj/. I found no strong per-project demand.
- **Caveat.** an HN commenter (HN 49757631, 2026-09-18) reports that "handoff chains cause more mistakes than letting one session run longer".

## 4. Visual presentation

- GitHub issue #42670 (2026-04-02) is a "Critical UX Regression... No way to access conversation history" from the alternate screen buffer.
- Codex issue #22627 (2026-05-14) asks for "in-thread navigation / table of contents for long sessions".
- Gemini CLI #22290 (2026-03-13) asks for export to Markdown/HTML.
- r/ClaudeAI: "Claude has no way to navigate long conversations": https://www.reddit.com/r/ClaudeAI/comments/1tkto9w/
- Graph and timeline viewers are appearing: "your prompts run down the spine in time order, tool..." (~1 month ago): https://www.reddit.com/r/ClaudeAI/comments/1vujjol/
- HN replay tool claude-replay (2026-03-06): "video-like player".
- NOT FOUND in my sources: explicit comments on diffs, dark mode, mobile, or search highlight.

## 5. Tool coverage (ranked)

**Survey data.**
- Pragmatic Engineer, 906 respondents, Jan-Feb 2026:
  - Claude Code is the most used, at 71% among regular agent users.
  - Cursor is at 39%.
  - Copilot is at 46%.
  - Codex is at about 60% of Cursor's usage.
  - Gemini CLI and OpenCode are each at about 10%.
  - 70% use 2-4 tools: https://newsletter.pragmaticengineer.com/p/ai-tooling-2026
- JetBrains, 10k+ respondents, Jan 2026, at work:
  - Copilot 29%, Cursor 18%, Claude Code 18%.
  - Antigravity 6%, JetBrains AI Assistant 9%, Junie 5%.
  - https://blog.jetbrains.com/research/2026/04/which-ai-coding-tools-do-developers-actually-use-at-work/

**GitHub stars (gh api, 2026-10-14).**

| Tool | Stars |
|---|---|
| hermes-agent | 253k |
| opencode (anomalyco) | 213k |
| claude-code | 151k |
| codex | 129k |
| pi (earendil-works) | 115k |
| gemini-cli | 107k |
| zed | 92k |
| cline | 70k |
| goose | 55k |
| aider | 50k (last push 2026-05) |
| continue | 36k |
| crush | 29k |
| qwen-code | 28k |
| kilocode | 28k |
| roo-code | 24k |
| kimi-cli | 11k |
| copilot-cli | 11k |

Star counts are a hype proxy, not usage.

**Ranked guess.**
1. Claude Code.
2. Cursor.
3. Codex.
4. Copilot.
5. Gemini CLI and Antigravity.
6. OpenCode.
7. Pi, which is strong in r/LocalLLaMA and r/LocalLLM ("I love Pi", ~4 months ago): https://www.reddit.com/r/LocalLLaMA/comments/1th5t1b/
8. Hermes. r/hermesagent says "session history are yours, on your machine" (1 week ago).
9. Cline, Kilo, Roo, Zed, Aider, Goose, Crush, Qwen Code, Kimi.
10. Windsurf, Kiro and Droid, the weakest signals. The r/windsurf thread "Windsurf gone, what's next" (~6 months ago) explains the Windsurf weakness. One user said they "used Droid for a bit... felt the same".

I found no usage data for Amp or Junie beyond the JetBrains figure.

## 6. Willingness to pay and deal-breakers

- Strong norm: free, open source, local-only. Typical launch lines are "MIT license, completely free, no paid tier, no telemetry" (r/ClaudeCode, ~7 months ago) and "Everything is local. No telemetry" (r/ClaudeAI, ~6 months ago, https://www.reddit.com/r/ClaudeAI/comments/1rx232t/).
- A deal-breaker example on HN: an HN commenter (2026-03-14) won't use a plugin because "its privacy policy says prompts and context are sent to its servers".
- The only paid angle with traction is team or hosted sync: HN Skillsync (YC W26), AFK, and Contextify, which is a "free, source-available, self-hosted personal version" (HN 48716979, 2026-06-29).
- I found no direct evidence of individuals paying for a history tool. The sample is dominated by makers.

## Product implications for Lore

1. **Retention rescue as headline.** Continuously snapshot and archive before upstream cleanup runs, and warn when `cleanupPeriodDays` is the default. This is the most validated pain point (#59248, many PSAs).
2. **Lead with "find the conversation where we decided X".** Search by topic across all tools, returning decision-level hits rather than raw message hits. Resume from the result.
3. **Handoff export.** One click per session produces a next-agent brief (changed, open, check first) plus a link to the raw transcript. Keep tool inputs and outputs available.
4. **Summaries: per-session first.** Structure them as decisions, rationale, open TODOs, files changed. Never hide the raw log. Project-level rollups come second; daily digests are low-evidence.
5. **Session view.** Collapse tool-call noise by default, with expandable results. Add a navigable outline or table of contents for long sessions, search-term jump, and a timeline. Add compaction markers showing what was dropped, since people want pre-compaction history back.
6. **Coverage.** Treat Claude Code, Cursor, Codex, Gemini CLI/Antigravity, OpenCode and Copilot as table stakes. Then add Pi, Hermes and Cline/Kilo/Roo. Treat Zed, Goose, Crush, Qwen and Kimi as opportunistic, via a generic JSONL adapter.
7. **Trust posture.** Keep it local, offline, no telemetry, open licence, read-only on source files. Add a secrets scan and redaction for export or share, since plaintext keys in transcripts are a known fear.
8. **Cost and usage per session.** A tokens and cost column across tools is cheap and cited often (ccusage threads). Team sync, audit trail and compliance are a later, optional, self-hosted feature.

Name collision: HN has a different product, "Lore – Give your coding agent the decisions your team made" (2026-06-29).

## What I could not verify

- Actual Reddit comment text. I read only titles and opening-post snippets, so reactions, dislikes and "why I use X" detail are missing.
- r/ExperiencedDevs, r/GeminiCLI, r/vibecoding and r/opencodeCLI got only incidental hits.
- Whether the Reddit thread dates are exact. They are relative ages.
- Willingness to pay by individuals.
- Visual details such as diffs, dark mode, mobile and search highlight.
- Amp, Kiro and Junie usage.
- Anything on compliance buyers, and GitHub discussions beyond the issue titles listed above.
