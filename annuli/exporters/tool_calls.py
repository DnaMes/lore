"""Canonical tool-call envelope shared by the Markdown and HTML exporters (#166).

Tool-call shapes historically diverged between extractors (``name`` vs ``tool``,
``arguments`` vs ``input``), and a message can carry two representations: the rich
``tool_calls`` dicts (arguments, output, status) and the compact ``tool_events``
that the v2 store keeps (name, status, duration, error, drop count). Rendering
only one of them loses content — which is how exports ended up with "Unknown
Tool" blocks and no results at all.

:func:`canonical_tool_calls` merges both into one view per call, in order, so a
renderer has exactly one shape to handle and no call disappears silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Keys under which extractors have stored a tool name.
NAME_KEYS = ("name", "tool", "tool_name")
# Keys under which extractors have stored the call arguments.
ARGUMENT_KEYS = ("input", "arguments", "args", "parameters")
# Keys under which extractors have stored the call result.
OUTPUT_KEYS = ("output", "result", "response")


@dataclass(frozen=True)
class CanonicalToolCall:
    """One tool call as the exporters present it.

    ``arguments`` is pretty-printed JSON when the stored value is JSON-like and
    the raw text otherwise, so a malformed payload is still readable and its
    original value is not lost (``arguments_is_json`` says which case applies).
    """

    name: str
    call_id: Optional[str] = None
    arguments: Optional[str] = None
    arguments_is_json: bool = True
    output: Optional[str] = None
    status: Optional[str] = None
    is_error: bool = False
    duration_ms: Optional[int] = None
    truncated: bool = False
    dropped_events: int = 0
    unmatched_result: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def state_label(self) -> str:
        """One word for the summary line: completed, failed, pending or unknown."""
        if self.is_error:
            return "failed"
        status = (self.status or "").strip().lower()
        if status in ("error", "failed", "failure"):
            return "failed"
        if status in ("completed", "complete", "success", "succeeded", "done"):
            return "completed"
        if status in ("pending", "running", "started"):
            return "pending"
        return status or "unknown"


def _first(mapping: Dict[str, Any], keys: tuple) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def _render_arguments(value: Any) -> tuple:
    """``(text, is_json)`` for a stored argument value."""
    if value is None or value == "":
        return None, True
    if isinstance(value, str):
        # Some extractors store the JSON as text; pretty-print it when it parses.
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value, False
        return json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=True), True
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True), True
    except (TypeError, ValueError):
        return str(value), False


def _render_output(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def canonical_tool_calls(message: Any) -> List[CanonicalToolCall]:
    """Merge a message's ``tool_calls`` and ``tool_events`` into one ordered list.

    ``tool_calls`` win where both describe the same call (they carry arguments
    and output); ``tool_events`` entries without a counterpart are appended, so a
    failed or truncated run stays visible even when its arguments were never
    captured. A ``TOOL``-role message (an unmatched result) produces one call with
    ``unmatched_result`` set when it has no pairing of its own.
    """
    calls: List[CanonicalToolCall] = []
    seen_ids: set = set()
    seen_names: Dict[str, int] = {}

    for raw in getattr(message, "tool_calls", None) or []:
        if not isinstance(raw, dict):
            continue
        name = str(_first(raw, NAME_KEYS) or "Unknown Tool")
        call_id = raw.get("id") or raw.get("call_id") or raw.get("tool_call_id")
        call_id = str(call_id) if call_id else None
        arguments, is_json = _render_arguments(_first(raw, ARGUMENT_KEYS))
        output = _render_output(_first(raw, OUTPUT_KEYS))
        status = raw.get("status")
        is_error = bool(raw.get("is_error")) or str(status or "").lower() in (
            "error",
            "failed",
            "failure",
        )
        duration = raw.get("duration_ms")
        truncated = bool(raw.get("truncated")) or bool(raw.get("output_truncated"))
        dropped = int(raw.get("dropped") or 0)
        calls.append(
            CanonicalToolCall(
                name=name,
                call_id=call_id,
                arguments=arguments,
                arguments_is_json=is_json,
                output=output,
                status=str(status) if status is not None else None,
                is_error=is_error,
                duration_ms=int(duration) if duration is not None else None,
                truncated=truncated,
                dropped_events=dropped,
                extra={
                    key: value
                    for key, value in raw.items()
                    if key
                    not in {
                        *NAME_KEYS,
                        *ARGUMENT_KEYS,
                        *OUTPUT_KEYS,
                        "id",
                        "call_id",
                        "tool_call_id",
                        "status",
                        "is_error",
                        "duration_ms",
                        "truncated",
                        "output_truncated",
                        "dropped",
                    }
                },
            )
        )
        if call_id:
            seen_ids.add(call_id)
        seen_names[name] = seen_names.get(name, 0) + 1

    for event in getattr(message, "tool_events", None) or []:
        if not isinstance(event, dict):
            continue
        name = str(_first(event, NAME_KEYS) or "Unknown Tool")
        # An event for a call already rendered from ``tool_calls`` adds nothing.
        if seen_names.get(name):
            seen_names[name] -= 1
            continue
        calls.append(
            CanonicalToolCall(
                name=name,
                status=str(event.get("status")) if event.get("status") is not None else None,
                is_error=bool(event.get("is_error")),
                duration_ms=int(event["duration_ms"])
                if event.get("duration_ms") is not None
                else None,
                truncated=bool(event.get("truncated")),
                dropped_events=int(event.get("dropped") or 0),
            )
        )

    if not calls and getattr(getattr(message, "role", None), "value", None) == "tool":
        content = getattr(message, "content", None)
        calls.append(
            CanonicalToolCall(
                name=str(getattr(message, "tool_name", None) or "Tool result"),
                output=content or "",
                unmatched_result=True,
            )
        )
    return calls


def _summary_line(call: CanonicalToolCall) -> str:
    """``name — state (duration)`` with the truncation note, shared by both renderers."""
    parts = [call.name, call.state_label]
    if call.duration_ms is not None:
        parts.append(f"{call.duration_ms} ms")
    line = " — ".join(parts[:2])
    if len(parts) > 2:
        line = f"{line} ({parts[2]})"
    if call.unmatched_result:
        line = f"{line} · unmatched result"
    if call.truncated:
        line = f"{line} · truncated"
    if call.dropped_events:
        line = f"{line} · {call.dropped_events} event(s) dropped"
    return line


def tool_call_markdown(call: CanonicalToolCall) -> List[str]:
    """Markdown block for one call: summary, identity, arguments, result (#166).

    Everything is inside ``<details>`` so a long transcript stays readable, and
    the values are literal: JSON arguments are pretty-printed, a non-JSON payload
    keeps its raw text, and the result is shown with fenced code.
    """
    lines = ["<details>", f"<summary>Tool: {_summary_line(call)}</summary>", ""]
    if call.call_id:
        lines.append(f"**Call id:** `{call.call_id}`")
        lines.append("")
    if call.arguments:
        language = "json" if call.arguments_is_json else "text"
        lines.append(f"**Arguments:**{' (not valid JSON)' if not call.arguments_is_json else ''}")
        lines.append("")
        lines.append(f"```{language}")
        lines.append(call.arguments)
        lines.append("```")
        lines.append("")
    if call.output is not None:
        label = "**Result:**"
        if call.is_error:
            label = "**Error:**"
        if call.truncated:
            label = f"{label} (truncated)"
        lines.append(label)
        lines.append("")
        lines.append("```text")
        lines.append(call.output)
        lines.append("```")
        lines.append("")
    if call.extra:
        lines.append("```json")
        lines.append(json.dumps(call.extra, indent=2, ensure_ascii=False, sort_keys=True))
        lines.append("```")
        lines.append("")
    lines.append("</details>")
    return lines


def tool_call_html(call: CanonicalToolCall) -> str:
    """HTML block for one call, mirroring :func:`tool_call_markdown`."""
    from html import escape

    parts = [
        "<details><summary>Tool: ",
        escape(_summary_line(call)),
        '</summary><div class="detail-body">',
    ]
    if call.call_id:
        parts.append(f'<p class="tool-id">Call id: <code>{escape(call.call_id)}</code></p>')
    if call.arguments:
        note = "" if call.arguments_is_json else " (not valid JSON)"
        parts.append(f"<p><strong>Arguments:</strong>{escape(note)}</p>")
        parts.append(f"<pre><code>{escape(call.arguments)}</code></pre>")
    if call.output is not None:
        label = "Error" if call.is_error else "Result"
        if call.truncated:
            label = f"{label} (truncated)"
        parts.append(f"<p><strong>{escape(label)}:</strong></p>")
        parts.append(f"<pre><code>{escape(call.output)}</code></pre>")
    if call.extra:
        parts.append(
            f"<pre><code>{escape(json.dumps(call.extra, indent=2, ensure_ascii=False, sort_keys=True))}"
            "</code></pre>"
        )
    parts.append("</div></details>")
    return "".join(parts)
