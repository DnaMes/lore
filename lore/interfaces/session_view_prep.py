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
from collections import Counter
from typing import Any

_AT_A_GLANCE_PROMPT_MAX = 240

_AT_A_GLANCE_PROMPT_MAX = 240

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
        calls = fold_tool_bursts(message.tool_calls)
        events = getattr(message, "tool_events", None)
        # Two-argument call only when events exist: one-argument formatters keep working.
        tools_html = format_tool_calls_fn(calls, events) if events else format_tool_calls_fn(calls)
        return "\n".join(part for part in (prose_html, tools_html) if part)

    if content:
        return format_message_content_fn(content)

    return ""


def _clamp_glance_text(text: str, limit: int = _AT_A_GLANCE_PROMPT_MAX) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _duration_label(session) -> str | None:
    created = getattr(session, "created_at", None)
    updated = getattr(session, "last_updated", None)
    if not created or not updated:
        return None
    seconds = int((updated - created).total_seconds())
    if seconds < 0:
        return None
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def at_a_glance(session) -> dict[str, Any]:
    """Deterministic session stats for the summary card when no AI summary exists."""
    from lore.core.models import Role
    from lore.services.summary_input import extract_artifacts, from_unified

    messages = list(getattr(session, "messages", None) or [])
    first_prompt = ""
    for message in messages:
        role = getattr(message, "role", None)
        if role == Role.USER or getattr(role, "value", None) == "user":
            first_prompt = _clamp_glance_text(getattr(message, "content", "") or "")
            break

    tool_calls = 0
    failed_tool_calls = 0
    tool_names: Counter[str] = Counter()
    for message in messages:
        for raw in getattr(message, "tool_calls", None) or []:
            if not isinstance(raw, dict):
                continue
            tool_calls += 1
            name = str(raw.get("tool") or "tool")
            tool_names[name] += 1
            status = str(raw.get("status") or "").lower()
            if status in {"failed", "error"} or raw.get("is_error"):
                failed_tool_calls += 1

    files_touched: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for raw in getattr(message, "tool_calls", None) or []:
            if not isinstance(raw, dict):
                continue
            inp = raw.get("input")
            if not isinstance(inp, dict):
                continue
            for key in ("path", "file_path", "file"):
                path = inp.get(key)
                if isinstance(path, str) and path and path not in seen:
                    seen.add(path)
                    files_touched.append(path)
                    if len(files_touched) >= 8:
                        break
            if len(files_touched) >= 8:
                break
        if len(files_touched) >= 8:
            break
    if len(files_touched) < 8 and messages:
        artifacts = extract_artifacts(from_unified(messages))
        for path in (*artifacts.files_modified, *artifacts.files_read):
            if path in seen:
                continue
            seen.add(path)
            files_touched.append(path)
            if len(files_touched) >= 8:
                break

    prompts = int(getattr(session, "prompt_count", 0) or 0)
    return {
        "first_prompt": first_prompt,
        "prompts": prompts,
        "tool_calls": tool_calls,
        "failed_tool_calls": failed_tool_calls,
        "tools_used": tool_names.most_common(6),
        "files_touched": files_touched,
        "duration": _duration_label(session),
    }
