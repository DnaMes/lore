"""Session-level redaction for exports and sharing."""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import replace
from typing import Any

from ..core.models import UnifiedMessage, UnifiedSession
from ..utils.redact import redact

REDACTED_EXPORT_MARKER = ".redacted-export"
_MAX_DEPTH = 5
# Identifier-like fields are kept verbatim; every other exported string field is redacted.
_KEEP_FIELDS = frozenset(
    {"session_id", "thread_id", "source_path", "project_hash", "parent_session_id", "origin"}
)


def _redact_strings_in_obj(value: Any, depth: int = 0) -> tuple[Any, int]:
    if isinstance(value, str):
        result = redact(value)
        return result.text, result.count
    if isinstance(value, (dict, list)) and depth >= _MAX_DEPTH:
        return "[REDACTED:too_deep]", 1  # fail closed instead of passing nested data through
    if isinstance(value, dict):
        total = 0
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_item, count = _redact_strings_in_obj(item, depth + 1)
            if isinstance(key, str):
                key_result = redact(key)
                key, count = key_result.text, count + key_result.count
            out[key] = new_item
            total += count
        return out, total
    if isinstance(value, list):
        total = 0
        out_list: list[Any] = []
        for item in value:
            new_item, count = _redact_strings_in_obj(item, depth + 1)
            out_list.append(new_item)
            total += count
        return out_list, total
    return value, 0


def redact_session(session: UnifiedSession) -> tuple[UnifiedSession, int]:
    """Return a copy of ``session`` with secret-shaped strings redacted."""
    total = 0
    new_messages: list[UnifiedMessage] = []

    for message in session.messages:
        msg = copy.copy(message)
        content_result = redact(message.content or "")
        msg.content = content_result.text
        total += content_result.count

        if message.reasoning:
            reasoning_result = redact(message.reasoning)
            msg.reasoning = reasoning_result.text
            total += reasoning_result.count

        if message.tool_calls:
            redacted_calls, count = _redact_strings_in_obj(message.tool_calls)
            msg.tool_calls = redacted_calls
            total += count

        if message.tool_events:
            redacted_events, count = _redact_strings_in_obj(message.tool_events)
            msg.tool_events = redacted_events
            total += count

        new_messages.append(msg)

    title = session.title
    if title:
        title_result = redact(title)
        title = title_result.text
        total += title_result.count

    extra: dict[str, Any] = {}
    for field in dataclasses.fields(session):
        if field.name in _KEEP_FIELDS or field.name in ("title", "messages"):
            continue
        value = getattr(session, field.name)
        if isinstance(value, str) and value:
            result = redact(value)
            if result.count:
                extra[field.name] = result.text
                total += result.count

    redacted = replace(session, title=title, messages=new_messages, **extra)
    return redacted, total
