"""Shared FTS5 query builder for session search and memory recall (#197)."""

from __future__ import annotations

from typing import Optional


def _quote(text: str) -> str:
    """Wrap ``text`` as one FTS5 string, doubling inner quotes."""
    return '"' + text.replace('"', '""') + '"'


def build_fts_query(query: str) -> Optional[str]:
    """Turn user text into an FTS5 MATCH expression, or None for an empty query.

    A query that is exactly one double-quoted phrase (``"exact phrase"``) becomes
    one FTS5 phrase. Anything else is split on whitespace and each term is
    prefix-matched; every term is quoted so FTS operators and stray quotes
    cannot cause a syntax error.
    """
    text = (query or "").strip()
    if not text:
        return None
    if len(text) > 2 and text.startswith('"') and text.endswith('"') and text[1:-1].strip():
        return _quote(text[1:-1])
    return " ".join(f"{_quote(t)}*" for t in text.split())
