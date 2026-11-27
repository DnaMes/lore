"""Group a session's messages into prompts and agent turns (web + Markdown)."""

from __future__ import annotations

import copy
import html
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from lore.core.models import normalize_tool_call
from lore.exporters.tool_calls import CanonicalToolCall, canonical_tool_calls
from lore.interfaces.session_view_prep import fold_tool_bursts, strip_tool_blocks
from lore.utils.injected import classify_injected_display
from lore.utils.text_processing import (
    _canonical_tool_status,
    tool_call_target,
)

_TOOL_ONLY_RE = re.compile(r"^\[Tool:\s*([^\]]+)\]\s*(.*)\Z", flags=re.DOTALL)
_EXIT_CODE_RE = re.compile(r"^\s*Exit code (\d+)")
_EXPORT_TOOL_STATUS_RE = re.compile(r"<summary>Tool:[^<]*?\u2014\s*(\w+)\s*</summary>")

MARKDOWN_OUTPUT_FOLD_LINES = 10

_FENCE_RE = re.compile(r"^(```+|~~~+)")

DEFAULT_NOISE_RULES: Dict[str, Dict[str, bool]] = {
    "default": {
        "strip_ansi_sequences": True,
        "session_classify_injected_events": True,
    },
    "claude-code": {
        "claude_strip_command_xml": True,
        "claude_summarize_file_operations": True,
    },
    "cursor": {
        "cursor_summarize_file_operations": True,
    },
    "gemini-cli": {
        "gemini_summarize_file_operations": True,
    },
    "codex": {
        "codex_summarize_file_operations": True,
    },
    "copilot-cli": {
        "copilot_summarize_file_operations": True,
    },
    "vscode-copilot": {
        "vscode_summarize_file_operations": True,
    },
    "opencode": {
        "opencode_strip_user_caveat": True,
        "opencode_summarize_file_operations": True,
        "opencode_summarize_unset_env_vars": True,
    },
    "antigravity": {
        "antigravity_summarize_file_operations": True,
    },
}


def is_tool_result_record(message: Any) -> bool:
    """True for a user-role record whose text is a ``[Tool Result]`` block."""
    original = getattr(message, "original_content", None) or message.content or ""
    return original.lstrip().startswith("[Tool Result]")


def flat_tool_call(message: Any) -> Optional[Dict[str, Any]]:
    """Parse a flattened ``[Tool: X] args`` block from export-markdown text."""
    raw = (getattr(message, "original_content", None) or message.content or "").strip()
    match = _TOOL_ONLY_RE.match(raw)
    if not match or "[Tool:" in match.group(2):
        return None
    body = match.group(2)
    status = _EXPORT_TOOL_STATUS_RE.search(body)
    return {
        "tool": match.group(1).strip(),
        "input": body.split("<details>", 1)[0].strip(),
        "output": None,
        "status": status.group(1) if status else None,
    }


def tool_only_calls(message: Any) -> Optional[List[Dict[str, Any]]]:
    """Tool calls of an assistant record that has no prose or reasoning."""
    display_role = getattr(message, "display_role", None) or message.role.value
    if display_role != "assistant" or message.reasoning:
        return None
    structured = getattr(message, "tool_calls", None)
    if not structured:
        flat = flat_tool_call(message)
        return [flat] if flat else None
    if strip_tool_blocks(message.content or "").strip():
        return None
    events = getattr(message, "tool_events", None) or []
    aligned = len(events) == len(structured)
    calls = []
    for index, raw in enumerate(structured):
        call = dict(raw) if isinstance(raw, dict) else {"tool": str(raw), "input": {}}
        event = events[index] if aligned and isinstance(events[index], dict) else None
        if event:
            if call.get("status") is None:
                call["status"] = event.get("status")
            if call.get("duration_ms") is None:
                call["duration_ms"] = event.get("duration_ms")
            call["is_error"] = bool(call.get("is_error") or event.get("is_error"))
        calls.append(call)
    return calls


def attach_result_to_calls(calls: List[Dict[str, Any]], output: str) -> bool:
    """Give ``output`` to the first call still without one."""
    target = next((call for call in calls if call.get("output") is None), None)
    if target is None:
        return False
    target["output"] = output
    exit_match = _EXIT_CODE_RE.match(output)
    if exit_match:
        target.update(status="failed", exit_code=int(exit_match.group(1)))
    elif output.lstrip().startswith("<tool_use_error>"):
        target["status"] = "failed"
    return True


@dataclass
class MarkdownSegment:
    """One rendered block inside an agent turn."""

    kind: str  # prose, tools, thinking, event, system
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    event_label: str = ""
    event_body: str = ""


@dataclass
class SessionTurn:
    """One user prompt and the agent's following work."""

    number: int
    timestamp: Any
    prompt_text: str
    day_label: str = ""
    segments: List[MarkdownSegment] = field(default_factory=list)


def _provider_rules(
    session: Any, noise_rules: Optional[Dict[str, Dict[str, bool]]]
) -> Dict[str, bool]:
    tool = getattr(getattr(session, "tool", None), "value", "")
    configured = noise_rules or DEFAULT_NOISE_RULES
    shared = configured.get("default", {})
    specific = configured.get(tool, {}) if tool else {}
    rules = dict(shared)
    rules.update(specific)
    return rules


def _longest_backtick_run(text: str) -> int:
    longest = 0
    run = 0
    for char in text:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _fenced_codeblock_lines(info: str, content: str) -> List[str]:
    """Fence lines whose backtick count exceeds nested runs inside ``content``."""
    fence_len = max(3, _longest_backtick_run(content) + 1)
    fence = "`" * fence_len
    body = content.splitlines()
    return [f"{fence}{info}", *body, fence]


def _message_display_role(message: Any) -> str:
    role = message.role.value
    if role == "user" and is_tool_result_record(message):
        return "tool"
    return role


def _prompt_text(tool: str, message: Any, provider_rules: Dict[str, bool]) -> str:
    original = getattr(message, "original_content", None) or message.content or ""
    injected = classify_injected_display(tool, message.role.value, original, provider_rules)
    if injected.kind == "slash_prompt":
        title = injected.prompt_title.strip()
        return f"/{title}" if title and not title.startswith("/") else title
    return message.content or ""


def _segments_from_responses(responses: List[Any]) -> List[MarkdownSegment]:
    segments: List[MarkdownSegment] = []
    run: List[Any] = []
    calls: List[Dict[str, Any]] = []
    shown_outputs = False

    def _flush_calls() -> None:
        if not calls:
            return
        folded = fold_tool_bursts(calls)
        segments.append(MarkdownSegment(kind="tools", tool_calls=folded))
        run.clear()
        calls.clear()

    for resp in responses:
        display = getattr(resp, "display_role", _message_display_role(resp))
        if display == "tool":
            original = getattr(resp, "original_content", None) or resp.content or ""
            output = re.sub(r"^\s*\[Tool Result\]\s*", "", original)
            if calls:
                if attach_result_to_calls(calls, output):
                    continue
                if shown_outputs and resp.role.value != "tool":
                    continue
                if resp.role.value != "tool":
                    _flush_calls()
                    continue
            elif shown_outputs and resp.role.value != "tool":
                continue
            _flush_calls()
            if resp.role.value == "tool" and output.strip():
                segments.append(
                    MarkdownSegment(
                        kind="tools",
                        tool_calls=[
                            {
                                "tool": getattr(resp, "tool_name", None) or "Tool result",
                                "input": {},
                                "output": output,
                                "unmatched_result": True,
                            }
                        ],
                    )
                )
            continue

        new_calls = tool_only_calls(resp)
        if new_calls:
            run.append(resp)
            calls.extend(new_calls)
            shown_outputs = any(call.get("output") for call in new_calls)
            continue

        _flush_calls()
        shown_outputs = bool(getattr(resp, "tool_calls", None))

        if display == "event":
            label = getattr(resp, "event_label", "") or "Event"
            body = getattr(resp, "original_content", None) or resp.content or ""
            segments.append(MarkdownSegment(kind="event", event_label=label, event_body=body))
            continue

        if resp.reasoning:
            segments.append(MarkdownSegment(kind="thinking", text=resp.reasoning))

        role_val = resp.role.value
        if role_val == "system":
            if resp.content:
                segments.append(MarkdownSegment(kind="system", text=resp.content))
            continue

        content = resp.content or ""
        if getattr(resp, "tool_calls", None):
            prose = strip_tool_blocks(content).strip()
            if prose:
                segments.append(MarkdownSegment(kind="prose", text=prose))
            folded = fold_tool_bursts(list(resp.tool_calls))
            if folded:
                segments.append(MarkdownSegment(kind="tools", tool_calls=folded))
            continue

        events = getattr(resp, "tool_events", None) or []
        if events and not content.strip():
            for call in canonical_tool_calls(resp):
                segments.append(
                    MarkdownSegment(
                        kind="tools",
                        tool_calls=[
                            {
                                "tool": call.name,
                                "input": {},
                                "output": call.output,
                                "status": call.status,
                                "is_error": call.is_error,
                                "id": call.call_id,
                            }
                        ],
                    )
                )
            continue

        if content.strip():
            segments.append(MarkdownSegment(kind="prose", text=content))

    _flush_calls()
    return segments


def build_session_turns(
    session: Any,
    *,
    noise_rules: Optional[Dict[str, Dict[str, bool]]] = None,
) -> List[SessionTurn]:
    """Partition messages into prompt turns with agent segments."""
    tool = getattr(getattr(session, "tool", None), "value", "")
    provider_rules = _provider_rules(session, noise_rules)
    turns: List[SessionTurn] = []
    current_responses: List[Any] = []
    prompt_number = 0
    previous_day = None

    current_turn: Optional[SessionTurn] = None

    def _close_turn() -> None:
        nonlocal current_turn, current_responses
        if current_turn is None:
            return
        current_turn.segments = _segments_from_responses(current_responses)
        turns.append(current_turn)
        current_turn = None
        current_responses = []

    for message in session.messages:
        display_role = _message_display_role(message)
        original = getattr(message, "original_content", None) or message.content or ""
        injected = classify_injected_display(tool, message.role.value, original, provider_rules)

        if injected.kind == "event":
            view = copy.copy(message)
            view.display_role = "event"
            view.event_label = injected.label
            if current_turn is None:
                current_turn = SessionTurn(
                    number=0,
                    timestamp=message.timestamp,
                    prompt_text="",
                )
            current_responses.append(view)
            continue

        if display_role == "user":
            _close_turn()
            prompt_number += 1
            stamp = message.timestamp
            day = stamp.date() if stamp else None
            day_label = stamp.strftime("%A, %Y-%m-%d") if day and day != previous_day else ""
            if day:
                previous_day = day
            current_turn = SessionTurn(
                number=prompt_number,
                timestamp=stamp,
                prompt_text=_prompt_text(tool, message, provider_rules),
                day_label=day_label,
            )
            continue

        if current_turn is None:
            current_turn = SessionTurn(
                number=0,
                timestamp=message.timestamp,
                prompt_text="",
            )
        view = copy.copy(message)
        view.display_role = display_role
        current_responses.append(view)

    _close_turn()
    return turns


def escape_markdown_outside_fences(text: str) -> str:
    """Escape ``<`` outside fenced code blocks so raw HTML cannot inject."""
    if not text:
        return ""
    parts: List[str] = []
    lines = text.split("\n")
    in_fence = False
    fence_char = ""
    fence_len = 0
    buf: List[str] = []

    def _flush_plain() -> None:
        if not buf:
            return
        chunk = "\n".join(buf)
        parts.append(html.escape(chunk, quote=False))
        buf.clear()

    for line in lines:
        stripped = line.lstrip()
        fence_match = _FENCE_RE.match(stripped)
        if fence_match and not in_fence:
            _flush_plain()
            in_fence = True
            fence_char = fence_match.group(1)[0]
            fence_len = len(fence_match.group(1))
            parts.append(line)
            continue
        if in_fence and stripped.startswith(fence_char * fence_len):
            parts.append(line)
            in_fence = False
            continue
        if in_fence:
            parts.append(line)
        else:
            buf.append(line)
    _flush_plain()
    return "\n".join(parts)


def _md_inline_code(text: str) -> str:
    text = text or ""
    if not text:
        return "``"
    if "`" in text:
        tick = "``"
        while tick in text:
            tick += "`"
        return f"{tick}{text}{tick}"
    return f"`{text}`"


def _tool_status_suffix(call: Dict[str, Any]) -> str:
    status = _canonical_tool_status(call.get("status"), is_error=bool(call.get("is_error")))
    exit_code = call.get("exit_code")
    if status == "failed":
        if isinstance(exit_code, int):
            return f" — ✗ exit {exit_code}"
        return " — ✗"
    if status == "ok":
        return " — ✓"
    if call.get("unmatched_result"):
        return " — ? unmatched"
    return ""


def _tool_label(name: str) -> str:
    if name.islower():
        return name.title()
    return name.split(" ×", 1)[0]


def _render_tool_output(output: str) -> List[str]:
    lines: List[str] = []
    body = (output or "").strip()
    if not body:
        return lines
    text_lines = body.splitlines()
    if len(text_lines) > MARKDOWN_OUTPUT_FOLD_LINES:
        lines.append("<details>")
        lines.append(f"<summary>Output ({len(text_lines)} lines)</summary>")
        lines.append("")
        lines.extend(_fenced_codeblock_lines("text", body))
        lines.append("</details>")
    else:
        lines.append("")
        lines.extend(_fenced_codeblock_lines("text", body))
    return lines


def render_tool_call_line(call: Dict[str, Any]) -> List[str]:
    """Compact Markdown list item for one tool call, optional folded output."""
    normalized = normalize_tool_call(call)
    name = _tool_label(str(normalized.get("tool") or "tool"))
    burst = call.get("_burst_count", 1) if isinstance(call, dict) else 1
    if burst > 1:
        name = f"{name} ×{burst}"
    target = tool_call_target(normalized.get("input"))
    target_bit = f" {_md_inline_code(target)}" if target else ""
    line = f"- **{name}**{target_bit}{_tool_status_suffix(normalized)}"
    out_lines = [line]
    output = normalized.get("output")
    if isinstance(output, str) and output.strip():
        out_lines.extend(_render_tool_output(output))
    return out_lines


def render_canonical_tool_line(call: CanonicalToolCall) -> List[str]:
    """Markdown list line from a canonical envelope call."""
    raw = {
        "tool": call.name,
        "input": call.arguments or {},
        "output": call.output,
        "status": call.status,
        "is_error": call.is_error,
        "id": call.call_id,
    }
    return render_tool_call_line(raw)


def render_turns_to_lines(turns: List[SessionTurn]) -> List[str]:
    """Render grouped turns as Markdown body lines (after the Conversation header)."""
    lines: List[str] = []
    for turn in turns:
        if turn.day_label:
            # Prompt headings carry the full date; the day change is a separator, so the
            # outline stays Conversation > Prompt n.
            lines.append(f"*{turn.day_label}*")
            lines.append("")
        if turn.number > 0:
            if turn.timestamp is not None:
                stamp = turn.timestamp.strftime("%Y-%m-%d %H:%M")
                lines.append(f"### Prompt {turn.number} · {stamp}")
            else:
                lines.append(f"### Prompt {turn.number}")
            lines.append("")
            if turn.prompt_text:
                lines.append(escape_markdown_outside_fences(turn.prompt_text))
                lines.append("")
        elif turn.prompt_text:
            lines.append(escape_markdown_outside_fences(turn.prompt_text))
            lines.append("")

        # Exports keep the original roles (#168): the prompt heading is the user, the
        # answer is marked once per turn, system notes are labelled.
        answered = False
        for segment in turn.segments:
            if segment.kind == "prose":
                if not answered:
                    lines.append("**Assistant**")
                    lines.append("")
                    answered = True
                lines.append(escape_markdown_outside_fences(segment.text))
                lines.append("")
            elif segment.kind == "system":
                lines.append(f"**System:** {escape_markdown_outside_fences(segment.text)}")
                lines.append("")
            elif segment.kind == "thinking":
                lines.append("<details>")
                lines.append("<summary>Thinking</summary>")
                lines.append("")
                lines.append(escape_markdown_outside_fences(segment.text))
                lines.append("</details>")
                lines.append("")
            elif segment.kind == "event":
                label = escape_markdown_outside_fences(segment.event_label)
                lines.append("<details>")
                lines.append(f"<summary>{label}</summary>")
                lines.append("")
                lines.append(escape_markdown_outside_fences(segment.event_body))
                lines.append("</details>")
                lines.append("")
            elif segment.kind == "tools":
                for raw_call in segment.tool_calls:
                    lines.extend(render_tool_call_line(raw_call))
                lines.append("")
    return lines
