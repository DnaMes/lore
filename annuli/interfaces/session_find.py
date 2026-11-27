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

# `input` is a void element (no text children, no end tag): skipping it would swallow the rest.
_SKIP_VISIBLE_TAGS = frozenset({"script", "style", "textarea", "mark"})


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


def _message_text_nodes(message: Any) -> list[tuple[str, bool]]:
    """Text nodes of the rendered message in DOM order as ``(text, is_reasoning)``.

    The page always renders reasoning inside ``.prose`` (folded, not removed), so it must
    count towards occurrence numbers even when it is not searched.
    """
    out: list[tuple[str, bool]] = []
    budget = _MAX_SCAN_PER_MESSAGE
    for attr, is_reasoning in (("formatted_reasoning", True), ("formatted_content", False)):
        html = (getattr(message, attr, "") or "")[: max(0, budget)]
        budget -= len(html)
        out.extend((text, is_reasoning) for text in visible_text_nodes_from_html(html))
    return out


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
            if scanned >= _MAX_TOTAL_SCAN:
                truncated = True
                return
            nodes = _message_text_nodes(message)
            scanned += sum(len(text) for text, _ in nodes)
            # Snippets are cut from the text the user can search (reasoning only if asked).
            searched = [t for t, is_reasoning in nodes if include_reasoning or not is_reasoning]
            full_visible = "".join(searched)
            occurrence = 0
            offset = 0
            for text, is_reasoning in nodes:
                counted = include_reasoning or not is_reasoning
                for pos in _occurrence_positions(text, q):
                    this_occurrence = occurrence
                    occurrence += 1
                    if not counted:
                        continue
                    total += 1
                    if len(matches) < _MAX_MATCHES:
                        matches.append(
                            {
                                "index": anchor,
                                "page": page,
                                "role": role_name,
                                "message": message_pos,
                                "occurrence": this_occurrence,
                                "snippet": _snippet_at(full_visible, offset + pos, len(q)),
                            }
                        )
                    else:
                        truncated = True
                if counted:
                    offset += len(text)

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
