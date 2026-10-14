"""Prep-layer: turn a UnifiedMessage into render-ready HTML from STRUCTURED data.

Why this exists (the §0 root problem from docs/UMBAU.md):

The old render path took a message's flattened ``content`` string — which for
Claude looks like ``"prose\n[Tool: Read] /x\n[Tool Result]\n..."`` — and
re-parsed the tool calls back out of it with regex. That threw away the
structured data the extractor already had (args as a dict, the paired result,
status) and produced call/result blocks that were never linked to each other.

Now that the extractor attaches each tool result to its ``tool_use`` dict as
``output`` (Axis 1), we can render straight from ``message.tool_calls``:
the prose comes from ``content`` with the ``[Tool: …]`` noise stripped, and the
tool cards come from the structured list — call and its result rendered
together, in order.

The actual HTML for a tool card still comes from the existing, already-decent
``format_tool_display`` / ``format_tool_result`` helpers; this layer only
decides WHAT to render from WHICH source, so the messy routing logic lives here
instead of inside the 2700-line template file. Output is sanitized HTML (the
formatters escape their inputs and the caller runs nh3 over the composed page).
"""

from __future__ import annotations

import re

# Matches the "[Tool: Name] ...args... [Tool Result] ...output..." blocks the
# extractor inlines into content. Used to strip them from prose so we don't show
# the noisy text form alongside the structured cards.
_TOOL_BLOCK_RE = re.compile(
    r"\[Tool:[^\]]*\].*?(?=\n\[Tool:|\n\n|\Z)"
    r"|\[Tool Result\].*?(?=\n\[Tool:|\n\[Tool Result\]|\n\n|\Z)",
    re.DOTALL,
)


def strip_tool_blocks(content: str) -> str:
    """Remove inlined ``[Tool: …]`` / ``[Tool Result]`` blocks from prose.

    Leaves the human-readable text and any fenced code that is part of the
    assistant's actual message, so the structured tool cards are the single
    source for tool activity.
    """
    if not content:
        return ""
    cleaned = _TOOL_BLOCK_RE.sub("", content)
    # Collapse the blank lines the removal leaves behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def has_structured_tool_calls(message) -> bool:
    """True if the message carries structured tool_calls we can render directly."""
    calls = getattr(message, "tool_calls", None)
    return bool(calls)


def fold_tool_bursts(tool_calls):
    """Collapse runs of identical consecutive tool calls into one ``×N`` card (#83).

    Long tool-heavy sessions often repeat the exact same call (same tool, same
    args, same output) many times in a row. Folding those into a single card with
    a count keeps the transcript scannable. Only *exactly identical consecutive*
    calls fold — anything that differs renders normally, so no information is lost.
    """
    from lore.core.models import normalize_tool_call

    if not tool_calls:
        return []

    folded = []
    for raw in tool_calls:
        call = normalize_tool_call(raw)
        if folded:
            prev = folded[-1]
            same = (
                prev["tool"] == call["tool"]
                and prev.get("input") == call.get("input")
                and prev.get("output") == call.get("output")
            )
            if same:
                prev["_burst_count"] = prev.get("_burst_count", 1) + 1
                continue
        call["_burst_count"] = 1
        folded.append(call)
    return folded


def render_message_body(
    message,
    *,
    format_message_content_fn,
    format_tool_calls_fn,
) -> str:
    """Compose a message's body HTML, preferring structured tool_calls.

    - With structured ``tool_calls``: render the prose (tool noise stripped)
      from ``content`` via the markdown formatter, then append the structured
      tool cards (call + paired result). This is the path that surfaces the
      full, non-truncated tool output the extractor now keeps.
    - Without ``tool_calls`` but with ``content``: the plain markdown path.
    - ``tool_calls`` only, no prose: just the cards.

    Returns sanitized HTML (the formatters escape their inputs).
    """
    content = getattr(message, "content", "") or ""

    if has_structured_tool_calls(message):
        prose_html = ""
        prose_source = strip_tool_blocks(content)
        if prose_source:
            prose_html = format_message_content_fn(prose_source)
        tools_html = format_tool_calls_fn(
            fold_tool_bursts(message.tool_calls),
            getattr(message, "tool_events", None),
        )
        return "\n".join(part for part in (prose_html, tools_html) if part)

    if content:
        return format_message_content_fn(content)

    return ""
