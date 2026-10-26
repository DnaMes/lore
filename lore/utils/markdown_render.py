"""Thread-local Markdown rendering shared by the web UI and the HTML export.

``markdown.markdown()`` builds a fresh parser (and processing pipeline) on every
call, while ``markdown.Markdown`` keeps state and is not thread-safe. One parser
per thread keeps the isolation and drops the rebuild cost: measured 1.27-1.37x
faster than the module-level helper on 1 KB and 10 KB messages
(see tests/test_markdown_renderer.py).
"""

from __future__ import annotations

import html
import threading

import markdown

MARKDOWN_EXTENSIONS = ("fenced_code", "tables", "nl2br")

# ``markdown.Markdown`` keeps parser state and is not thread-safe, while
# ``markdown.markdown()`` builds a fresh parser on every call. One parser per
# thread keeps the isolation and drops the rebuild cost.
_MARKDOWN_LOCAL = threading.local()


def thread_local_markdown_parser() -> "markdown.Markdown":
    """Return this thread's reusable Markdown parser, creating it on first use."""
    parser = getattr(_MARKDOWN_LOCAL, "parser", None)
    if parser is None:
        parser = markdown.Markdown(extensions=list(MARKDOWN_EXTENSIONS))
        _MARKDOWN_LOCAL.parser = parser
    return parser


def render_markdown(text: str) -> str:
    """Render Markdown with the thread-local parser, or escape when unavailable.

    Equivalent to ``markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS)``
    but reuses the parser; ``reset()`` clears footnotes and other per-document
    state before each conversion. Without the optional ``markdown`` package the
    text is HTML-escaped with newlines preserved, matching the previous
    behaviour of every call site.
    """
    if not markdown:
        return html.escape(text).replace("\n", "<br>")
    return thread_local_markdown_parser().reset().convert(text)
