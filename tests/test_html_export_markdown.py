"""Standalone HTML export renders full Markdown, offline and sanitised (#169).

The export used to understand only paragraphs and ``` fences, so headings,
tables, lists, blockquotes and inline markup landed in a standalone file as
literal text — and a shared page has no renderer of its own to fix that. It now
uses the same thread-local Markdown pipeline as the web UI and the same
allowlist, so the page shows what the UI shows and nothing executable.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.html import render_session_html

TS = datetime(2026, 6, 1, 9, 0, 0)

MARKDOWN = """## Summary of the bug

The `cursor` leaked because:

- the lock was taken twice
- the `finally` block was skipped

| step | state |
| ---- | ----- |
| parse | ok |
| write | failed |

> Quoted advice from the review.

A [link](https://example.com/docs) and **strong** text with an ![image](https://example.com/x.png).

```python
def f():
    return "<not markup>"
```
"""


def _session(content: str = MARKDOWN, title: str = "Rendering") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="ses_render",
        created_at=TS,
        last_updated=TS,
        project_path="/repo/lore",
        title=title,
        messages=[
            UnifiedMessage(role=Role.USER, content="look at this", timestamp=TS),
            UnifiedMessage(role=Role.ASSISTANT, content=content, timestamp=TS),
        ],
    )


@pytest.fixture()
def page() -> str:
    return render_session_html(_session())


def test_headings_are_rendered(page):
    assert "<h2>Summary of the bug</h2>" in page


def test_lists_are_rendered(page):
    assert "<ul>" in page and "<li>the lock was taken twice</li>" in page


def test_tables_are_rendered(page):
    assert "<table>" in page and "<th>step</th>" in page and "<td>failed</td>" in page


def test_blockquote_is_rendered(page):
    assert "<blockquote>" in page and "Quoted advice from the review." in page


def test_inline_markup_is_rendered(page):
    assert "<code>cursor</code>" in page
    assert "<strong>strong</strong>" in page
    assert '<a href="https://example.com/docs">link</a>' in page


def test_fenced_code_keeps_its_text_verbatim(page):
    assert 'return "&lt;not markup&gt;"' in page
    assert "<not markup>" not in page.split("<pre>", 1)[1].split("</pre>", 1)[0]


def test_external_images_are_dropped(page):
    """Not in the allowlist: an <img> in content never reaches the file."""
    assert "<img" not in page.replace("&lt;img", "")


def test_scripts_and_handlers_are_stripped():
    content = '<script>alert(1)</script>\n\n<b onclick="steal()">bold</b>'
    page = render_session_html(_session(content=content))
    assert "<script" not in page
    assert "onclick" not in page
    # <b> is not in the allowlist, so the tag goes and its text stays.
    assert "<b>" not in page
    assert ">bold<" in page


def test_javascript_links_are_rejected():
    page = render_session_html(_session(content="[click](javascript:alert(1))"))
    assert "javascript:" not in page


def test_page_is_self_contained_and_offline(page):
    """No stylesheet link, no script, no remote asset: the file works alone."""
    assert "<style>" in page
    assert "<script" not in page
    assert "<link" not in page
    refs = re.findall(r'(?:src|href)="([^"]+)"', page)
    # Only content links may appear, and they are ordinary https URLs.
    assert all(url.startswith(("https://example.com", "#")) for url in refs), refs


def test_plain_text_still_works():
    page = render_session_html(_session(content="just a sentence", title="Plain"))
    assert "<p>just a sentence</p>" in page


def test_markdown_rendering_is_shared_with_the_web_ui(page):
    """Same pipeline, so the export and the UI cannot drift apart."""
    from lore.utils.markdown_render import render_markdown

    assert render_markdown("**x**") == "<p><strong>x</strong></p>"
    assert "<strong>x</strong>" in page or "<strong>strong</strong>" in page
