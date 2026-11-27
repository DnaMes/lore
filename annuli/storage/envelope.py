"""Canonical message envelope — structured message data that survives storage (#154).

The ``messages`` table keeps ``content`` as flattened text because FTS, exporters
and the UI depend on it. Everything an extractor knows *beyond* that text —
native ids, ordered content blocks, tool calls with arguments and results, a
message status, provider-specific fields nobody modelled yet — lives in the
envelope columns added by migration 20 and is written/read through this module.

Contract: ``docs/superpowers/specs/2026-10-26-source-revision-and-identity-design.md``.

Rules enforced here:

- unknown provider fields are stored verbatim (no key filtering, no renaming);
- ``None`` and an empty container stay distinguishable (missing vs. empty);
- an over-large value is replaced by a *labelled* stub with its sha256 and
  original length, never dropped silently;
- legacy rows (``envelope_version = 0``) are readable and report themselves as
  legacy instead of pretending to be a complete envelope.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ..core.models import UnifiedMessage

#: Version of the envelope written by this module. Bump when the shape changes.
ENVELOPE_VERSION = 1

#: Per-field cap. A larger value is stored as a truncation stub.
MAX_ENVELOPE_CHARS = 256 * 1024

#: Message statuses. Mirrors the UI's state vocabulary (#168).
MESSAGE_STATUSES = ("ok", "failed", "cancelled", "pending", "running", "unmatched", "unknown")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _truncation_stub(text: str, field: str) -> Dict[str, Any]:
    return {
        "_truncated": True,
        "field": field,
        "sha256": _sha256(text),
        "original_length": len(text),
    }


def _prune_oversized(value: Any, field: str) -> Any:
    """Replace oversized strings in place, keeping the container structure.

    A tool result of 2 MB inside a two-element list must leave the list shape
    intact (one entry, one stub) instead of collapsing the whole field.
    """
    if isinstance(value, str):
        return _truncation_stub(value, field) if len(value) > MAX_ENVELOPE_CHARS else value
    if isinstance(value, list):
        return [_prune_oversized(item, field) for item in value]
    if isinstance(value, dict):
        return {key: _prune_oversized(item, field) for key, item in value.items()}
    return value


def dump_envelope_value(value: Any, field: str) -> Optional[str]:
    """Serialise one envelope field, replacing oversized values with a stub.

    Returns ``None`` for a missing value so the column stays ``NULL``; an empty
    list/dict serialises to ``"[]"``/``"{}"`` and therefore survives as "present
    but empty". Structure is preserved: only the offending string becomes a stub.
    """
    if value is None:
        return None
    pruned = _prune_oversized(value, field)
    try:
        serialised = json.dumps(pruned, ensure_ascii=False, sort_keys=False, default=str)
    except (TypeError, ValueError):
        serialised = json.dumps({"_unserialisable": str(value)}, ensure_ascii=False)
    # A provider can hand over JS-truncated emoji (lone surrogates); SQLite cannot
    # store them and the write would fail. Replace them like ``writer._scrub``
    # does for the flattened text, so an odd payload never blocks a session.
    serialised = serialised.encode("utf-8", "replace").decode("utf-8")
    if len(serialised) > MAX_ENVELOPE_CHARS:
        # The structure itself is over the cap (many small items): one stub for
        # the whole field, still labelled so nothing looks complete by accident.
        return json.dumps(_truncation_stub(serialised, field), ensure_ascii=False, sort_keys=True)
    return serialised


def load_envelope_value(raw: Optional[str]) -> Any:
    """Parse a stored envelope field; damaged JSON becomes ``None`` (never raises)."""
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def is_truncation_stub(value: Any) -> bool:
    return isinstance(value, dict) and value.get("_truncated") is True


def normalise_status(status: Optional[str]) -> str:
    """Map an extractor's status onto the closed set (``unknown`` when absent)."""
    if not status:
        return "unknown"
    lowered = str(status).strip().lower()
    return lowered if lowered in MESSAGE_STATUSES else "unknown"


def envelope_for(message: UnifiedMessage) -> Dict[str, Optional[str]]:
    """Build the envelope column values for one message."""
    return {
        "native_id": message.message_id or None,
        "content_blocks_json": dump_envelope_value(
            getattr(message, "content_blocks", None), "content_blocks"
        ),
        "tool_calls_json": dump_envelope_value(message.tool_calls, "tool_calls"),
        "status": normalise_status(getattr(message, "status", None)),
        "extensions_json": dump_envelope_value(getattr(message, "extensions", None), "extensions"),
    }


def message_from_row(row: Any, *, include_reasoning: bool = False) -> UnifiedMessage:
    """Build a ``UnifiedMessage`` from a v2 ``messages`` row (with envelope columns).

    Works for rows read by ``reader.load_session_messages_v2``. Missing columns
    (a database below migration 20) degrade to ``None`` instead of raising, so the
    reader can pass the same row shape for old and new stores.
    """
    from datetime import datetime

    from ..core.models import Role

    keys = set(row.keys())
    role_value = row["role"]
    try:
        role = Role(role_value)
    except ValueError:
        role = Role.USER

    timestamp = None
    if row["timestamp"]:
        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
        except ValueError:
            timestamp = None

    tokens = load_envelope_value(row["tokens_json"]) if row["tokens_json"] else None
    tool_events = load_envelope_value(row["tool_events_json"]) if "tool_events_json" in keys else []
    if not isinstance(tool_events, list):
        tool_events = []
    tool_events = [event for event in tool_events if isinstance(event, dict)]

    version = (
        int(row["envelope_version"])
        if "envelope_version" in keys and row["envelope_version"]
        else 0
    )
    content_blocks = (
        load_envelope_value(row["content_blocks_json"]) if "content_blocks_json" in keys else None
    )
    tool_calls = load_envelope_value(row["tool_calls_json"]) if "tool_calls_json" in keys else None
    extensions = load_envelope_value(row["extensions_json"]) if "extensions_json" in keys else None
    native_id = row["native_id"] if "native_id" in keys else None
    status = row["status"] if "status" in keys and row["status"] else None

    message = UnifiedMessage(
        role=role,
        content=row["content"] or "",
        timestamp=timestamp,
        message_id=native_id or None,
        model=row["model"],
        tokens=tokens if isinstance(tokens, dict) else None,
        tool_calls=tool_calls if isinstance(tool_calls, list) else [],
        reasoning=(row["reasoning"] if include_reasoning and "reasoning" in keys else None),
        kind=(row["kind"] if "kind" in keys and row["kind"] else "text"),
        parent_id=(row["parent_id"] if "parent_id" in keys else None),
        active=(bool(row["active"]) if "active" in keys and row["active"] is not None else True),
        tool_events=tool_events,
    )
    message.content_blocks = content_blocks if isinstance(content_blocks, list) else None
    message.status = normalise_status(status)
    message.extensions = extensions if isinstance(extensions, dict) else None
    message.envelope_version = version
    return message


def legacy_message_row(keys: List[str]) -> Dict[str, Any]:
    """Column defaults for a store below migration 20, used by tolerant readers."""
    return {key: None for key in keys}
