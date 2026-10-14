"""In-session find: scan enriched session pairs for substring matches (B5.7a)."""

from __future__ import annotations

import html
import re
from typing import Any

from lore.interfaces.session_view_prep import strip_tool_blocks

_MAX_MATCHES = 200
_MAX_QUERY_LEN = 100
_MIN_QUERY_LEN = 2
_MAX_SNIPPET = 160
_MAX_SCAN_PER_MESSAGE = 200_000
_MAX_TOOL_OUTPUT_SCAN = 20_000
_MAX_TOTAL_SCAN = 5_000_000  # characters scanned per request, across all messages

_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text_for_scan(message: Any, *, include_reasoning: bool) -> str:
    """Message body text eligible for find (content only, bounded)."""
    parts: list[str] = []
    content = getattr(message, "content", "") or ""
    if content:
        prose = strip_tool_blocks(content)
        if prose:
            parts.append(prose[:_MAX_SCAN_PER_MESSAGE])
    if include_reasoning:
        reasoning = getattr(message, "reasoning", None) or ""
        if reasoning:
            parts.append(reasoning[:_MAX_SCAN_PER_MESSAGE])
    return "\n".join(parts)


def _snippet_at(text: str, match_pos: int, match_len: int) -> str:
    plain = _TAG_RE.sub("", html.unescape(text))
    plain = re.sub(r"\s+", " ", plain).strip()
    lower = plain.lower()
    needle_lower = plain[match_pos : match_pos + match_len].lower()
    pos = lower.find(needle_lower)
    if pos < 0:
        pos = match_pos
    radius = max(0, (_MAX_SNIPPET - match_len) // 2)
    start = max(0, pos - radius)
    end = min(len(plain), start + _MAX_SNIPPET)
    if end - start < _MAX_SNIPPET and start > 0:
        start = max(0, end - _MAX_SNIPPET)
    snippet = plain[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(plain):
        snippet = snippet.rstrip() + "…"
    return snippet.replace("<", "").replace(">", "")


def _occurrence_positions(text: str, query: str) -> list[int]:
    if not text or not query:
        return []
    # lower() (not casefold()) matches what the browser's toLowerCase() does when the page
    # highlights hits, so server matches and DOM highlights agree ("ss" must not hit "ß").
    hay = text.lower()
    needle = query.lower()
    positions: list[int] = []
    start = 0
    while start <= len(hay) - len(needle):
        idx = hay.find(needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + max(1, len(needle))
    return positions


def _pair_page(pair_index: int, per_page: int) -> int:
    return pair_index // per_page + 1


def _anchor_index(pair: dict[str, Any]) -> int:
    user = pair.get("user")
    if user is not None and getattr(user, "index", None) is not None:
        return int(user.index)
    responses = pair.get("responses") or []
    if responses:
        return int(getattr(responses[0], "index", 0) or 0)
    return 0


def find_session_matches(
    session_obj: Any,
    query: str,
    *,
    include_reasoning: bool = False,
    per_page: int = 40,
) -> dict[str, Any]:
    """Return JSON-serializable find results for one session."""
    q = (query or "").strip()
    pairs = getattr(session_obj, "pairs", []) or []
    matches: list[dict[str, Any]] = []
    total = 0
    truncated = False

    scanned = 0
    for pair_idx, pair in enumerate(pairs):
        if scanned >= _MAX_TOTAL_SCAN:
            truncated = True
            break
        page = _pair_page(pair_idx, per_page)
        anchor = _anchor_index(pair)
        for role_name, message in (
            ("user", pair.get("user")),
            *(("assistant", resp) for resp in (pair.get("responses") or [])),
        ):
            if message is None:
                continue
            text = _plain_text_for_scan(message, include_reasoning=include_reasoning)
            if not text:
                continue
            scanned += len(text)
            for pos in _occurrence_positions(text, q):
                total += 1
                if len(matches) < _MAX_MATCHES:
                    matches.append(
                        {
                            "index": anchor,
                            "page": page,
                            "role": role_name,
                            "snippet": _snippet_at(text, pos, len(q)),
                        }
                    )
                else:
                    truncated = True

    if total > _MAX_MATCHES:
        truncated = True

    return {
        "query": q,
        "total": total,
        "matches": matches,
        "truncated": truncated,
    }


def validate_find_query(query: str) -> str | None:
    """Return an error message when ``query`` is invalid, else None."""
    q = (query or "").strip()
    if len(q) < _MIN_QUERY_LEN:
        return "Query too short"
    if len(q) > _MAX_QUERY_LEN:
        return "Query too long"
    return None
