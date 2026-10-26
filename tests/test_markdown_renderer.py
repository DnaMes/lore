"""Contract for the thread-local Markdown renderer (cluster of PRs #202-#215).

``render_markdown`` must be output-equivalent to
``markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS)``, reuse one parser per
thread, clear per-document state between conversions, and degrade exactly like
the call sites it replaced when the optional dependency is missing.
"""

from __future__ import annotations

import threading

import markdown
import pytest

from lore.interfaces.web_formatting import (
    MARKDOWN_EXTENSIONS,
    render_markdown,
    thread_local_markdown_parser,
)

CORPUS = [
    "plain paragraph",
    "## Heading\n\nSome *emphasis* and `inline code`.",
    "```python\nprint('fenced')\n```",
    "| a | b |\n| - | - |\n| 1 | 2 |",
    "line one\nline two",
    "a [link](https://example.com) and <b>raw html</b>",
    "text with a footnote[^1]\n\n[^1]: the note",
    "",
]


@pytest.mark.parametrize("text", CORPUS)
def test_output_matches_the_module_level_helper(text):
    expected = markdown.markdown(text, extensions=list(MARKDOWN_EXTENSIONS))
    assert render_markdown(text) == expected


def test_parser_is_reused_within_one_thread():
    first = thread_local_markdown_parser()
    render_markdown("first document")
    assert thread_local_markdown_parser() is first


def test_each_thread_gets_its_own_parser():
    main_parser = thread_local_markdown_parser()
    seen: list[object] = []

    def worker() -> None:
        seen.append(thread_local_markdown_parser())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen and seen[0] is not main_parser


def test_state_does_not_leak_between_documents():
    """Footnotes are per-document state; reset() must clear them."""
    with_footnote = render_markdown("text[^1]\n\n[^1]: the note")
    assert "the note" in with_footnote
    second = render_markdown("plain paragraph")
    assert "the note" not in second and "footnote" not in second


def test_missing_markdown_package_falls_back_to_escaped_text(monkeypatch):
    from lore.utils import markdown_render

    monkeypatch.setattr(markdown_render, "markdown", None)
    assert render_markdown("<b>x</b>\ny") == "&lt;b&gt;x&lt;/b&gt;<br>y"


def test_parser_uses_the_documented_extensions():
    parser = thread_local_markdown_parser()
    assert parser.convert("| a |\n| - |\n| 1 |").startswith("<table>")
    assert "nl2br" in " ".join(MARKDOWN_EXTENSIONS)
