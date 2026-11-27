"""Shared FTS5 query builder for session search and memory recall (#197)."""

from __future__ import annotations

import re
from typing import Optional


def _quote(text: str) -> str:
    """Wrap ``text`` as one FTS5 string, doubling inner quotes."""
    return '"' + text.replace('"', '""') + '"'


# A quote that opens a token and a quote that closes one: ``"exact phrase"`` inside a longer query.
_QUOTED_PHRASE = re.compile(r'(?<!\S)"([^"]*\S[^"]*)"(?!\S)')


def phrase_of(query: str) -> Optional[str]:
    """Return the phrase text when ``query`` is one double-quoted phrase, else None."""
    text = (query or "").strip()
    if len(text) > 2 and text.startswith('"') and text.endswith('"') and text[1:-1].strip():
        return text[1:-1]
    return None


def build_fts_query(query: str) -> Optional[str]:
    """Turn user text into an FTS5 MATCH expression, or None for an empty query.

    A query that is exactly one double-quoted phrase (``"exact phrase"``) becomes
    one FTS5 phrase. In a longer query a double-quoted run that starts and ends a
    token (``foo "exact phrase" bar``) is still one exact phrase, and the other
    words are split on whitespace and prefix-matched. Every term is quoted so FTS
    operators and stray quotes cannot cause a syntax error. NUL is replaced by a space (FTS5 rejects it
    inside a string); the entry-point validators reject it earlier anyway.
    """
    text = (query or "").replace("\x00", " ").strip()
    if not text:
        return None
    phrase = phrase_of(text)
    if phrase is not None and '"' not in phrase:
        return _quote(phrase)
    parts: list[str] = []
    position = 0
    for match in _QUOTED_PHRASE.finditer(text):
        parts.extend(f"{_quote(t)}*" for t in text[position : match.start()].split())
        parts.append(_quote(match.group(1).strip()))
        position = match.end()
    parts.extend(f"{_quote(t)}*" for t in text[position:].split())
    return " ".join(parts)


# Index of the ``body`` column in the ``search_index`` FTS5 table, in the order
# of its column list (entity_type, entity_id, tool, project, title, body; see
# migration 4 in schema.py). ``snippet()`` takes a column number, not a name.
SEARCH_INDEX_BODY_COLUMN = 5

# FTS5 ``snippet()`` brackets each match with these sentinels at the storage
# layer. Payloads convert them with :func:`snippet_to_text`; nothing outside
# ``annuli.storage`` should see the raw form.
SNIPPET_OPEN = "\x02"
SNIPPET_CLOSE = "\x03"


def snippet_to_text(snippet: str) -> str:
    """Convert a sentinel-form FTS snippet into text with ``«`` / ``»`` marks.

    Literal ``«``/``»`` in the (untrusted) body become ``‹``/``›`` first, so
    body text cannot fake a highlight. Sentinels are then mapped to the marks,
    and any stray control character that survives is dropped.
    """
    text = snippet.replace("«", "‹").replace("»", "›")
    text = text.replace(SNIPPET_OPEN, "«").replace(SNIPPET_CLOSE, "»")
    return text.replace(SNIPPET_OPEN, "").replace(SNIPPET_CLOSE, "")
