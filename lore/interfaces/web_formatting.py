"""Message formatting and HTML sanitization for the web interface.

This module handles content formatting, tool call display, and HTML sanitization
for the web interface.
"""

import html
import json
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Optional

import markdown
import nh3

from lore.core.models import AUTOMATED_ORIGINS
from lore.utils.text_processing import strip_ansi

from .web_data import threadsafe_lru_cache

_thread_local = threading.local()


def _get_markdown_parser():
    """Get or create a thread-local Markdown parser instance.

    The python-markdown Markdown class is not thread-safe. Caching a thread-local
    instance significantly improves performance in multithreaded environments by
    avoiding repeated instantiations of the parser and its extensions.
    """
    if not hasattr(_thread_local, "md"):
        _thread_local.md = markdown.Markdown(extensions=["fenced_code", "tables", "nl2br"])
    return _thread_local.md

_COMPARE_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)


def _normalize_compare_text(value: Optional[str]) -> str:
    if not value:
        return ""
    lowered = str(value).casefold()
    return _COMPARE_STRIP_RE.sub("", lowered)


def should_show_prompt_outline(
    title: Optional[str],
    outline: Optional[str],
    prefix_len: int = 40,
) -> bool:
    """True when outline adds information beyond the title prefix (list row snippet)."""
    if not outline or not str(outline).strip():
        return False
    norm_title = _normalize_compare_text(title)
    norm_outline = _normalize_compare_text(outline)
    if not norm_outline:
        return False
    title_prefix = norm_title[:prefix_len]
    outline_prefix = norm_outline[:prefix_len]
    if title_prefix and norm_outline.startswith(title_prefix):
        return False
    if outline_prefix and norm_title.startswith(outline_prefix):
        return False
    return True


def format_compact_token_count(tokens: Optional[int]) -> str:
    if tokens is None or tokens < 0:
        return ""
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        scaled = tokens / 1000.0
        if scaled >= 100:
            return f"{int(scaled)}k"
        text = f"{scaled:.1f}k"
        return text.replace(".0k", "k")
    scaled = tokens / 1_000_000.0
    if scaled >= 100:
        return f"{int(scaled)}M"
    text = f"{scaled:.1f}M"
    return text.replace(".0M", "M")


def _parse_local_naive(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp; aware values are converted to local time, then made naive.

    The index mixes naive local timestamps with ``Z``/offset ones, so every comparison and
    display goes through this one normalisation.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def format_session_duration(created: Optional[str], updated: Optional[str]) -> str:
    start = _parse_local_naive(created)
    end = _parse_local_naive(updated)
    if start is None or end is None:
        return ""
    total_minutes = int((end - start).total_seconds()) // 60
    if total_minutes < 1:
        return ""
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def format_session_row_time(
    iso_value: Optional[str],
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    """Return (visible label, datetime attribute) for a session row timestamp."""
    dt = _parse_local_naive(iso_value)
    if dt is None:
        return ("", "")
    ref = now or datetime.now()
    if ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    time_part = dt.strftime("%H:%M")
    if dt.date() == ref.date():
        label = time_part
    else:
        label = f"{dt.strftime('%b')} {dt.day} {time_part}"
    return (label, dt.isoformat(timespec="minutes"))


def session_day_iso(session: dict) -> Optional[str]:
    """Local calendar day (YYYY-MM-DD) from ``updated`` or ``created``."""
    dt = _parse_local_naive(session.get("updated") or session.get("created"))
    if dt is None:
        return None
    return dt.date().isoformat()


def format_day_header_label(day_key: str, now: Optional[datetime] = None) -> str:
    """Human label for a day header (Today, Yesterday, weekday, or month date)."""
    try:
        day = date.fromisoformat(day_key)
    except ValueError:
        return day_key
    ref = now or datetime.now()
    if ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    ref_day = ref.date()
    delta = (ref_day - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if 2 <= delta <= 7:
        return f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}"
    if day.year != ref_day.year:
        return f"{day.strftime('%b')} {day.day}, {day.year}"
    return f"{day.strftime('%b')} {day.day}"


def group_sessions_for_list(
    sessions: list,
    *,
    group_mode: str = "day",
    after_day: Optional[str] = None,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Group sessions by local day for list rendering."""
    if group_mode == "flat":
        return [
            {
                "day_key": None,
                "label": "",
                "count": len(sessions),
                "sessions": list(sessions),
                "show_header": False,
            }
        ]
    ref = now or datetime.now()
    if ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    groups: list[dict[str, Any]] = []
    for session in sessions:
        day_key = session_day_iso(session)
        if not groups or groups[-1]["day_key"] != day_key:
            label = format_day_header_label(day_key, ref) if day_key else "Unknown date"
            groups.append(
                {
                    "day_key": day_key,
                    "label": label,
                    "sessions": [session],
                }
            )
        else:
            groups[-1]["sessions"].append(session)
    for index, group in enumerate(groups):
        group["count"] = len(group["sessions"])
        skip = index == 0 and after_day and group.get("day_key") == after_day
        group["show_header"] = not skip
    return groups


def session_date_range_chips(
    *,
    now: Optional[datetime] = None,
    active_start: str = "",
    active_end: str = "",
) -> list[dict[str, Any]]:
    """Preset date-range chips for the sessions list (start/end query params)."""
    ref = now or datetime.now()
    if ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    today = ref.date()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=6)
    month_start = today - timedelta(days=29)

    def _active(start: Optional[date], end: Optional[date]) -> bool:
        if start is None and end is None:
            return not active_start and not active_end
        if not active_start or not active_end:
            return False
        try:
            s = date.fromisoformat(active_start)
            e = date.fromisoformat(active_end)
        except ValueError:
            return False
        return s == start and e == end

    specs = [
        ("today", "Today", today, today),
        ("yesterday", "Yesterday", yesterday, yesterday),
        ("7d", "7 days", week_start, today),
        ("30d", "30 days", month_start, today),
        ("all", "All", None, None),
    ]
    chips: list[dict[str, Any]] = []
    for chip_id, label, start, end in specs:
        chips.append(
            {
                "id": chip_id,
                "label": label,
                "start": start.isoformat() if start else "",
                "end": end.isoformat() if end else "",
                "active": _active(start, end),
            }
        )
    return chips


def session_row_tokens(entry: dict) -> Optional[int]:
    raw = entry.get("tokens")
    if raw is None:
        raw = entry.get("total_tokens")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_automated_origin(origin: Optional[str]) -> bool:
    return origin in AUTOMATED_ORIGINS


# HTML sanitization constants (nh3 requires sets, not lists)
SANITIZE_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "u",
    "code",
    "pre",
    "a",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "div",
    "span",
    # Used by tool-call / thinking collapsible blocks generated internally
    "details",
    "summary",
}

SANITIZE_ATTRS = {
    "a": {"href", "title"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
    "details": {"class"},
    "summary": {"class"},
}

SANITIZE_URL_SCHEMES = {"http", "https", "mailto"}


def sanitize_rendered_html(rendered_html: str) -> str:
    """Sanitize HTML content for safe display."""
    return nh3.clean(
        rendered_html or "",
        tags=SANITIZE_TAGS,
        attributes=SANITIZE_ATTRS,
        url_schemes=SANITIZE_URL_SCHEMES,
        strip_comments=True,
        link_rel=None,
    )


@threadsafe_lru_cache(maxsize=4096)
def _format_message_content_cached(raw_content: str) -> str:
    """Format message content with caching for performance."""
    from lore.utils.text_processing import (
        format_thinking,
        format_tool_display,
        format_tool_result,
    )

    content = strip_ansi(raw_content)
    placeholders = {}
    placeholder_idx = 0

    def next_key(prefix: str) -> str:
        nonlocal placeholder_idx
        placeholder_idx += 1
        return f"{prefix}{placeholder_idx}"

    def repl_think(m):
        k = next_key("THINK")
        placeholders[k] = format_thinking(m.group(1))
        return k

    content = re.sub(
        r"<(?:thinking|think)>(.*?)</(?:thinking|think)>",
        repl_think,
        content,
        flags=re.DOTALL,
    )

    def repl_cmd_msg(m):
        k = next_key("CMDMESSAGE")
        placeholders[k] = f'<code class="cmd-tag">{html.escape(m.group(2))}</code>'
        return k

    content = re.sub(
        r"<(command-message)>(.*?)</\1>",
        repl_cmd_msg,
        content,
        flags=re.DOTALL,
    )

    def repl_cmd_name(m):
        k = next_key("CMDNAME")
        placeholders[k] = f'<code class="cmd-name">{html.escape(m.group(2))}</code>'
        return k

    content = re.sub(
        r"<(command-name)>(.*?)</\1>",
        repl_cmd_name,
        content,
        flags=re.DOTALL,
    )

    def repl_cmd_args(m):
        k = next_key("CMDARGS")
        placeholders[k] = f'<code class="cmd-args">{html.escape(m.group(2))}</code>'
        return k

    content = re.sub(
        r"<(command-args)>(.*?)</\1>",
        repl_cmd_args,
        content,
        flags=re.DOTALL,
    )

    def repl_res(m):
        k = next_key("TOOLRES")
        placeholders[k] = format_tool_result(m.group(1))
        return k

    content = re.sub(
        r"\[Tool Result\]\s*\n(.*?)(?=\n\[Tool:|\n\[Tool Result\]|\Z)",
        repl_res,
        content,
        flags=re.DOTALL,
    )

    def repl_call(m):
        k = next_key("TOOLCALL")
        placeholders[k] = format_tool_display(m.group(1), m.group(2))
        return k

    content = re.sub(
        r"\[Tool:\s*([^\]]+)\]\s*(.*?)(?=\n\[Tool:|\n\[Tool Result\]|\nTOOLRES\d+|\Z)",
        repl_call,
        content,
        flags=re.DOTALL,
    )

    if markdown:
        content = _get_markdown_parser().reset().convert(content)
        content = nh3.clean(
            content,
            tags=SANITIZE_TAGS | {"code"},
            attributes=SANITIZE_ATTRS,
            url_schemes=SANITIZE_URL_SCHEMES,
            strip_comments=True,
            link_rel=None,
        )
    else:
        content = html.escape(content).replace("\n", "<br>")

    for k, v in placeholders.items():
        content = content.replace(f"<p>{k}</p>", v).replace(k, v)

    # Final sanitization pass over the fully-composed string; catches any
    # injection that slipped through placeholder re-injection above.
    return sanitize_rendered_html(content)


def format_message_content(content):
    """Format message content for display."""
    if not content:
        return ""
    return _format_message_content_cached(content)


def _tool_event_for_call(
    tool_events, index: int, call_id, tool_name: str = "", aligned: bool = True
) -> dict | None:
    """Find the compact event for a call: by id, else by position when no folding happened.

    Identical consecutive calls are folded upstream, which shifts positions; an event is then
    only used when its name also matches, so a status never lands on the wrong call.
    """
    if not tool_events:
        return None
    if call_id:
        for ev in tool_events:
            if isinstance(ev, dict) and ev.get("id") == call_id:
                return ev
    if aligned and index < len(tool_events) and isinstance(tool_events[index], dict):
        ev = tool_events[index]
        name = ev.get("name")
        if not name or not tool_name or str(name).lower() == tool_name.split(" ")[0].lower():
            return ev
    return None


def _tool_group_label(raw_tool: str) -> str:
    from lore.utils.text_processing import _normalize_tool_name

    base = str(raw_tool).split(" ×", 1)[0]
    return _normalize_tool_name(base).title()


def _tool_group_name_line(entries: list[tuple[str, int]]) -> str:
    """First-appearance tool labels with optional ×N counts; at most five then +K more."""
    parts: list[str] = []
    extra_names = 0
    for index, (label, count) in enumerate(entries):
        if index >= 5:
            extra_names += 1
            continue
        safe = html.escape(label)
        parts.append(f"{safe}×{count}" if count > 1 else safe)
    if extra_names:
        parts.append(f"+{extra_names} more")
    return " · ".join(parts)


def _tool_group_status_html(
    failed_count: int, duration_ms: int | None, all_confirmed_ok: bool = True
) -> str:
    from lore.utils.text_processing import format_duration_ms

    if failed_count:
        return (
            '<span class="tool-group-status tool-group-status-fail">'
            '<span class="tool-status-icon" aria-hidden="true">✗</span> '
            f"{html.escape(str(failed_count))} failed</span>"
        )
    duration_label = format_duration_ms(duration_ms) if duration_ms is not None else None
    duration_bit = (
        f'<span class="action-meta">{html.escape(duration_label)}</span>' if duration_label else ""
    )
    if not all_confirmed_ok:  # no failure seen, but not every call reported success
        return '<span class="tool-group-status tool-group-status-neutral">status unknown</span>' + (
            duration_bit
        )
    return (
        '<span class="tool-group-status tool-group-status-ok">'
        '<span class="tool-status-icon" aria-hidden="true">✓</span> all ok</span>' + duration_bit
    )


def _wrap_tool_group(
    inner_html: str,
    *,
    total_calls: int,
    name_entries: list[tuple[str, int]],
    failed_count: int,
    duration_ms: int | None,
    all_confirmed_ok: bool = True,
) -> str:
    open_attr = " open" if failed_count else ""
    default_open = ' data-default-open="1"' if failed_count else ""
    names = _tool_group_name_line(name_entries)
    status = _tool_group_status_html(failed_count, duration_ms, all_confirmed_ok)
    return (
        f'<details class="tool-group"{open_attr}{default_open}>'
        '<summary class="tool-group-summary">'
        f'<span class="tool-group-count">{total_calls} tool calls</span>'
        f'<span class="tool-group-names"> · {names}</span>'
        f"{status}"
        "</summary>"
        f'<div class="tool-group-body">{inner_html}</div>'
        "</details>"
    )


def format_tool_calls(tool_calls, tool_events=None):
    """Format tool calls for display, grouping consecutive calls when there are two or more."""
    from lore.utils.text_processing import (
        _canonical_tool_status,
        format_duration_ms,
        format_tool_display,
        format_tool_result,
    )

    if not tool_calls:
        return ""
    from lore.core.models import normalize_tool_call

    events = tool_events or []
    aligned = len(events) == len(tool_calls)
    rendered: list[str] = []
    name_entries: list[tuple[str, int]] = []
    total_calls = 0
    failed_count = 0
    ok_count = 0
    duration_sum = 0
    all_durations_known = True

    for idx, raw_tc in enumerate(tool_calls):
        tc = normalize_tool_call(raw_tc)
        tool_name = str(tc["tool"])
        burst = raw_tc.get("_burst_count", 1) if isinstance(raw_tc, dict) else 1
        total_calls += burst
        label = _tool_group_label(tool_name)
        if name_entries and name_entries[-1][0] == label:
            name_entries[-1] = (label, name_entries[-1][1] + burst)
        else:
            name_entries.append((label, burst))

        display_name = tool_name
        if burst > 1:
            display_name = f"{tool_name} ×{burst}"
        tool_input = tc["input"]
        if isinstance(tool_input, (dict, list)):
            tool_args = json.dumps(tool_input, indent=2, ensure_ascii=False)
        else:
            tool_args = str(tool_input)

        ev = _tool_event_for_call(events, idx, tc.get("id"), str(tc["tool"]), aligned)
        duration_ms = tc.get("duration_ms")
        if duration_ms is None and ev:
            duration_ms = ev.get("duration_ms")
        status = tc.get("status")
        if status is None and ev:
            status = ev.get("status")
        is_error = bool(tc.get("is_error")) or bool(ev and ev.get("is_error"))
        exit_code = tc.get("exit_code")
        if exit_code is None and ev:
            exit_code = ev.get("exit_code")

        status_word = _canonical_tool_status(status, is_error=is_error)
        if status_word == "failed":
            failed_count += burst
        elif status_word == "ok":
            ok_count += burst

        if duration_ms is None or format_duration_ms(duration_ms) is None:
            all_durations_known = False
        else:
            duration_sum += int(duration_ms) * burst

        parts = [
            format_tool_display(
                display_name,
                tool_args,
                status=status,
                duration_ms=duration_ms,
                is_error=is_error,
            )
        ]
        tool_output = tc["output"]
        if isinstance(tool_output, str) and tool_output.strip():
            parts.append(
                format_tool_result(
                    tool_output,
                    status=status,
                    duration_ms=duration_ms,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    is_error=is_error,
                )
            )
        rendered.append("\n".join(parts))

    body = "\n".join(rendered)
    if len(tool_calls) < 2:
        return body
    group_duration = duration_sum if all_durations_known else None
    return _wrap_tool_group(
        body,
        total_calls=total_calls,
        name_entries=name_entries,
        failed_count=failed_count,
        duration_ms=group_duration if all_durations_known else None,
        all_confirmed_ok=ok_count >= total_calls,
    )


__all__ = [
    "SANITIZE_TAGS",
    "SANITIZE_ATTRS",
    "SANITIZE_URL_SCHEMES",
    "_get_markdown_parser",
    "sanitize_rendered_html",
    "_format_message_content_cached",
    "format_message_content",
    "format_tool_calls",
    "should_show_prompt_outline",
    "format_compact_token_count",
    "format_session_duration",
    "format_session_row_time",
    "session_row_tokens",
    "is_automated_origin",
]
