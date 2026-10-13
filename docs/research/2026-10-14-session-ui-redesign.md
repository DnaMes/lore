<!-- Research report, agent-generated on 2026-10-14 for issue #197 follow-up work.
     Built against a synthetic dataset; mock HTML files are in docs/research/mocks/. -->

# Lore sessions redesign: design spec

Repo status: `git status --short` is empty. Both servers (5871, 5872) are stopped and the browser is closed.

## 1. What is wrong today (measured)

I ran Lore against a scratch HOME at a scratch HOME with 25 synthetic sessions: 4 tools, 3 projects, the largest with 114 messages, 38 prompts, tool calls and code. The screenshots are below.

**List**
- **Density:** only 5 sessions fit in 900px, because each row is a bordered card of about 105px.
- **Duplicated text:** `prompt_outline` repeats the title ("Fix N+1 query… Please take a careful look").
- **Unused metadata:** `tokens`, `updated` and `messages` are already in the index but not shown. Only the date is shown, with no time, duration or token count.
- **Destructive action:** a red ✕ Delete sits on every row.
- **No grouping:** there are no day headers, and the sidebar's "Recent activity" duplicates the list.
- **At 390px:** the date clips ("2026-10-1"), the delete button is pushed off-screen, and 5 stacked filter controls eat about 250px.

**Detail**
- **No summary:** there is no AI summary and no sticky header (the title scrolls away).
- **Equal-weight actions:** Resume, Copy link, Dashboard, Export and Delete all look the same; Dashboard duplicates the sidebar.
- **Code blocks:** there are two frames, a "CODE/Copy" bar and a "Code Block · 7 lines" bar. At 390px the code clips and the Copy button overlaps it.
- **Tool cards:** the "70 CHARS" chip carries no useful information, and there is no ok/fail status.
- **Role labels:** roles are labelled three times (gutter "A", "AI CLAUDE" badge, card colour).
- **Outline on mobile:** the TOC is `hidden lg:block`, so mobile has no outline at all.
- **Bug found (not fixed):** `session.html` renders `data-session-id=""clau-…""` because `|tojson` does not escape `"`. This is the console `JSON.parse` error and it breaks the tag UI.

Markdown lists render as `- ` plain text lines. My synthetic export also leaked raw `<details>` into a tool card; this comes from the exporter format and may not be a UI bug (NOT VERIFIED).

## 2. Prior art (only what the fetches confirmed)

| Source | Pattern |
|---|---|
| Langfuse | Log View: all steps in one scrollable stream with whole-trace Cmd/Ctrl+F. Tree/timeline toggle. Sessions can be bookmarked or shared. |
| LangSmith Threads | List columns: first input, last output, start time, turns, latency, tokens, cost, feedback. Detail views switch with keys `T`/`D`: Trajectory (chat), Turns (collapsible cards), Details. |
| claude-code-log | Index with per-session summaries and time ranges, message-type filters with live counts, folded long content, "dense rows on a time rail". |
| claude-code-viewer | ⌘K search, in-page find, a Raw toggle for tool JSON, diff view, "unify sessions with same title". |
| claude-code-trace | `j`/`k`, `Tab` toggles, `e`/`c` expand/collapse all, `?` shows hints. |
| simonw/claude-code-transcripts | Index timeline of prompts and commits, paginated pages, gist share. |
| Linear | Group by X, sub-group, sticky group headers, per-property display toggles, `Cmd+B` layout. |
| Raycast | Sections, right-aligned accessories with relative date, split detail pane, empty view with an action. |
| GitHub PR | Resolved threads collapse. |

Sentry, Phoenix, Helicone, SpecStory and the LangSmith waterfall returned little or failed: NOT VERIFIED. I did not use them.

## 3. Information architecture

**List.** Toolbar, then active filter chips, then sessions grouped by day with sticky headers showing "N sessions · tokens". Rows are 1 to 2 lines and about 56px.
- **Line 1:** tool glyph, title, then accessories: project chip, prompts, tokens, duration, time. An optional live dot marks active sessions.
- **Line 2:** the AI one-liner marked with ✦ if a summary exists; otherwise the first prompt snippet, quoted, and only if it differs from the title. If neither applies, show counts such as "2 prompts · no tool calls".
- **Actions:** Resume and ⋯ appear on hover or focus. Delete lives only in ⋯ or bulk "Select". Selection is explicit, never via a persistent ✕.
- **Peek pane (≥1280px):** the focused row shows summary, decisions, files touched and a prompt list. This is the Raycast detail pattern.

**Detail.** Sticky header, then summary card, then outline and transcript.
- The sticky header holds crumb, title, chips (tool·model, branch, time·duration, prompts·msgs·tokens·tool calls with failures in red), primary **Resume**, then Copy link and ⋯.
- A control row has Tools [Hide|Fold|Open], Thinking [fold|show] and find. On scroll the header shrinks to 46px.

## 4. Wireframes

```
LIST (1440, inside existing shell)
Sessions  25 · 4 tools · 3.1M tok                          [Select]
[/ Filter title, prompt, file…] [Today|7d|30d|All] [Tool▾][Project▾] [Day|Project|None]
(Last 7 days ×) (Project: lore ×)
TODAY ──────────────────────────────────────── 4 sessions · 1.2M tok
▌C Fix N+1 query in session list endpoint   lore 38 prompts 906k 3h04m 13:24
▌  ✦ Cached sorted id tuple by index mtime; 1 test failing.
 X Add feedback ratings to memory recall    lore 31 prompts 412k 1h52m 10:03
   "can we derive a bounded weight from thumbs up/down…"
YESTERDAY ───────────────────────────────── 5 sessions · 1.6M tok
```
```
DETAIL (≥1024)
Sessions / lore / Oct 14
Fix N+1 query…                               [Resume][Copy link][…][Theme]
Claude Code·sonnet-5-5  feat/197  Oct 14 10:20–13:24  38 prompts·906k·91 calls (3 failed)
[Hide|Fold|Open] [Think: fold|show]                [Find in session 1/3 /]
┌ Prompts·38 ──┐ ┌ ▾ ✦ Summary ───────────────────────────────┐
│ 1 Fix N+1… 10:24│ │ one paragraph / Decisions / Open / Files    │
│ 3 run tests ● │ └─────────────────────────────────────────────┘
│ Commits       │ ▌YOU·1                                     10:24
└───────────────┘ model ↑3,884 ↓1,557 14s
                    ▸ Thinking · 6 lines
                    ▸ 5 tool calls  Read×2·Edit×2·Bash        all ok
                    prose… [code: lang · 7 lines · Copy]
```

## 5. Component list

- **List components:** `session-row` (replaces `session_rows.html` markup), `day-header`, `filter-bar` (segmented date, selects, chips), `peek-pane`, `bulk-bar`, `empty-state`.
- **Detail components:** `session-header` (sticky, compact on scroll), `summary-card`, `outline-rail` / `outline-sheet`, `turn` (prompt + response), `tool-group`, `think-fold`, `code-block` (single frame), `diff-lines`, `find-bar`, `turn-meta`.
- **Reuse:** `tag-chip`, `surface-card`, existing `data-action` delegation, `FocusTrap`, `lore:messages-appended`.

## 6. Interaction details

- **Keyboard (list):** `j`/`k` move, `Enter` opens, `/` focuses the filter, `x` selects, `?` shows hints.
- **Keyboard (detail):** `j`/`k` jump prompt, `e`/`c` expand/collapse all tool groups, `/` find, `Esc` closes.
- **Tool groups:** consecutive calls become one `<details>` group titled "5 tool calls · Read×2 · Edit×2 · Bash" with status. Failed groups open by default. Show success or failure and duration instead of "N chars". Edit calls render ±lines with add/del backgrounds.
- **Thinking:** `<details>`, folded by default.
- **Code:** one frame with lang, line count and Copy. Collapse above about 24 lines with "Show N more".
- **Find:** server-side or load-all-then-highlight with `<mark>` and n/N. Pages are 40 pairs (`MESSAGES_PER_PAGE`), so browser Cmd-F misses unloaded turns. The existing TOC jump already loads pages until the anchor appears.
- **View modes:** merge the five toggles (Clean, Ultra, Readable, Present, Compact) into Tools [Hide|Fold|Open], Thinking [fold|show] and a single Density option. No sixth toggle.
- **Day headers:** `/sessions/rows` paginates by 50, so pass the previous row's day so a header is not printed twice.

## 7. Summary: present vs absent

- **Present (from `session_knowledge`: `topic`, `key_points`, `decisions`, `tools_used`):** the card shows a paragraph, Decisions, Open, Files touched, and "generated <date> · <model>".
- **Absent:** a deterministic "At a glance" card with no model call. It shows the first prompt (clamped), counts (prompts, tool calls, failures), tools used, files touched from `tool_calls` inputs, and a **Generate summary** button.
- **In the list:** the ✦ one-liner appears only when a summary exists. Never show a placeholder row.

## 8. Mobile (390px)

- **List:** the checkbox column is hidden outside Select mode, so the glyph sits at the left edge. Accessories wrap to a second meta line (project, prompts, time). Filters collapse into one "Filters · N" sheet. The peek pane is hidden.
- **Detail:** the header is about 195px at the top and compacts to 46px once scrolled (verified). The outline moves behind a "Prompts ▾" button that opens a bottom sheet. The summary is open by default and long content scrolls inside `overflow-x:auto`.

## 9. Accessibility

- Rows are `role=option` in a `listbox`, with `aria-selected` and visible `:focus-visible`.
- Tool and thinking groups use `<details>/<summary>`, native and keyboard accessible with no JS.
- Status is never colour-only (the word "failed" plus an icon). Keep `--muted` ≥ AA (the existing tokens are already). Respect `prefers-reduced-motion`.
- The outline is a `nav` with `aria-current`. The bottom sheet gets `aria-modal` and `FocusTrap`. Matches use `<mark>` and a live "1 / 3" region.

## 10. Stack fit

- **CSS:** semantic classes in the `<style nonce>` block of `base.html` (`.compact-row`, `.tag-chip` pattern). New Tailwind utilities would force `scripts/build_tailwind.sh`.
- **JS:** a new `case` in the delegated `data-action` switch (around line 1668). No inline handlers.
- **Themes:** CSS variables, `html.dark`.
- **Server rendering:** all grouping and summaries happen in Jinja with no SPA.

## 11. Implementation order

1. **S:** fix `data-session-id`. Hide the outline when it duplicates the title. Drop the ✕ per row (use the ⋯ menu). Compact rows with tokens, duration and time. Fix the 390px clipping.
2. **S:** single-frame code block. Replace "N CHARS" with a status. Add `<details>` for thinking.
3. **M:** day headers with the pagination hand-off. Date chips and grouping toggle. Sticky compact header with Resume as the primary action. Move the other actions into ⋯.
4. **M:** summary card (from `session_knowledge`, plus the deterministic fallback). The ✦ line in the list.
5. **M:** `tool-group` folding and Hide/Fold/Open. Merge the five view toggles.
6. **M:** mobile outline sheet and "Filters" sheet. Bulk select.
7. **L:** in-session find across all pages (server search or load-all). Peek pane. `j`/`k`/`e`/`c` shortcuts.

## 12. Files

- **Mocks (light/dark toggle, no inline handlers):** `docs/research/mocks/mock-list.html`, `docs/research/mocks/mock-detail.html`
- **Current UI screenshots:** taken during the research (list/detail at 1440 and 390 px), not committed
- **Scratch data and scripts:** synthetic generator, not committed

Not checked: the dark theme in the mocks was not screenshotted, and the real-template Tailwind rebuild cost was not measured.
