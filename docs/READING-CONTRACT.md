# Reading contract

What a reader can rely on when reading a transcript in Lore — and where the rule
is enforced. `tests/test_a11y_reading_contract.py` checks the machine-decidable
half over every page; the visual half (200 % zoom, a 380 px viewport, keyboard
walkthrough in a real browser) still needs a browser and is tracked in #194.

## Roles and identity

| Rule | Implementation |
|------|----------------|
| Every turn states its original role in the markup | `data-role="user\|assistant\|tool\|system\|info"` in `session_pairs.html` |
| Each role has its own badge and card | `.role-badge[data-role=…]` and `.message-card[data-role=…]` in `base.html` |
| Tool execution state is one closed vocabulary | `pending`, `running`, `ok`, `failed`, `cancelled`, `unmatched`, `unknown` — `canonical_tool_state()` in `lore/utils/text_processing.py` |
| A state is readable, not only coloured | `data-status` plus a label and an icon on every tool chip |
| Folding never hides a failure | a collapsed burst keeps `data-status="failed"` |
| Nothing in the pipeline rewrites the canonical text | `original_content` in `web_services.normalize_session_messages_for_display`; exports render the canonical roles and states |

## Typography and derived content

| Rule | Implementation |
|------|----------------|
| Colours come from theme variables | `var(--text)`, `var(--text-muted)`, `var(--card)`, `var(--border)` in `base.html` |
| Transcript text is never a fixed-height box | no `height: Npx` on `.message-card` / `.prose` (zoom must not clip) |
| Long tokens wrap, code scrolls | `overflow-wrap` on prose, `overflow-x: auto` on `pre` |
| Derived text is labelled | `title_source` in the export front matter, "not transcript content" notes for title and summary, "collapsed" for folded summaries |
| Folding is a view | the folded lines stay in a collapsed `details` block and in the export |

## Keyboard, focus and status

| Rule | Implementation |
|------|----------------|
| A skip link reaches the content | `.skip-link` → `#main` (checked per page) |
| Every link and button has a name | text, `aria-label` or `title` (checked per page) |
| Every form control has a name | `aria-label`, `aria-labelledby`, an enclosing `<label>` or `<label for>` |
| Focus is always visible | `:focus-visible` ring on every interactive component; a `:focus { outline: none }` is only allowed when the matching `:focus-visible` ring exists |
| Async feedback is announced | `role="status" aria-live="polite"` for action status, `role="alert"` for the error toast |
| Motion respects the OS setting | `@media (prefers-reduced-motion: reduce)` blocks |

## Structure

| Rule | Implementation |
|------|----------------|
| The document declares its language | `<html lang="en">` |
| One `h1` per page, no heading level jumps | page templates |
| Ids are unique | checked per page |
| `target="_blank"` links carry `rel` | checked per page |
| Deep links are durable | `/session/<id>/m/<seq>`; unknown session 404, unknown message 410 |

## What is not covered yet

- The browser pass: a real 380 px viewport at 200 % zoom, keyboard traversal
  with the produced HTML, and a contrast measurement over the three themes.
  Tracked in #194 together with the Playwright fixture.
- Colour contrast values are not asserted numerically; the rules above only
  keep them theme-driven.
