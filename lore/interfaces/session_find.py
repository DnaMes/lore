"""In-session find: scan enriched session pairs for substring matches (B5.7a)."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

_MAX_MATCHES = 200
_MAX_QUERY_LEN = 100
_MIN_QUERY_LEN = 2
_MAX_SNIPPET = 160
_MAX_SCAN_PER_MESSAGE = 200_000
_MAX_TOTAL_SCAN = 5_000_000  # characters scanned per request, across all messages

_SKIP_VISIBLE_TAGS = frozenset({"script", "style", "textarea", "input", "mark"})


class _VisibleTextExtractor(HTMLParser):
    """Collect text-node strings from HTML (mirrors client find highlight walk)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.nodes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _SKIP_VISIBLE_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIP_VISIBLE_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        self.nodes.append(data)


def visible_text_nodes_from_html(html: str) -> list[str]:
    """Return visible text segments in document order (one list entry per text node)."""
    if not html:
        return []
    parser = _VisibleTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.nodes


def _visible_html_for_find(message: Any, *, include_reasoning: bool) -> str:
    parts: list[str] = []
    if include_reasoning:
        reasoning_html = getattr(message, "formatted_reasoning", "") or ""
        if reasoning_html:
            parts.append(reasoning_html)
    # The exact HTML the page renders inside `.prose` (tool cards included), so server
    # and client count the same text nodes.
    content_html = getattr(message, "formatted_content", "") or ""
    if content_html:
        parts.append(content_html)
    combined = "\n".join(parts)
    if len(combined) > _MAX_SCAN_PER_MESSAGE:
        combined = combined[:_MAX_SCAN_PER_MESSAGE]
    return combined


def _snippet_at(full_visible: str, match_pos: int, match_len: int) -> str:
    radius = max(0, (_MAX_SNIPPET - match_len) // 2)
    start = max(0, match_pos - radius)
    end = min(len(full_visible), start + _MAX_SNIPPET)
    if end - start < _MAX_SNIPPET and start > 0:
        start = max(0, end - _MAX_SNIPPET)
    snippet = full_visible[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(full_visible):
        snippet = snippet + "…"
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


def _scan_visible_message(
    nodes: list[str],
    query: str,
) -> tuple[list[tuple[int, int]], str, int]:
    """Return (node_index, pos_in_node) hits, concatenated visible text, chars scanned."""
    scanned = sum(len(n) for n in nodes)
    hits: list[tuple[int, int]] = []
    for node_idx, node_text in enumerate(nodes):
        for pos in _occurrence_positions(node_text, query):
            hits.append((node_idx, pos))
    full_visible = "".join(nodes)
    return hits, full_visible, scanned


def _global_offset(nodes: list[str], node_idx: int, pos_in_node: int) -> int:
    return sum(len(nodes[i]) for i in range(node_idx)) + pos_in_node


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

        def _emit(role_name: str, message: Any, message_pos: int) -> None:
            nonlocal scanned, total, truncated
            html = _visible_html_for_find(message, include_reasoning=include_reasoning)
            if not html:
                return
            nodes = visible_text_nodes_from_html(html)
            node_hits, full_visible, msg_scanned = _scan_visible_message(nodes, q)
            scanned += msg_scanned
            if scanned >= _MAX_TOTAL_SCAN:
                truncated = True
            occurrence = 0
            for node_idx, pos_in_node in node_hits:
                global_pos = _global_offset(nodes, node_idx, pos_in_node)
                total += 1
                if len(matches) < _MAX_MATCHES:
                    matches.append(
                        {
                            "index": anchor,
                            "page": page,
                            "role": role_name,
                            "message": message_pos,
                            "occurrence": occurrence,
                            "snippet": _snippet_at(full_visible, global_pos, len(q)),
                        }
                    )
                else:
                    truncated = True
                occurrence += 1

        user = pair.get("user")
        if user is not None:
            _emit("user", user, 0)

        for resp_idx, resp in enumerate(pair.get("responses") or [], start=1):
            _emit("assistant", resp, resp_idx)

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
