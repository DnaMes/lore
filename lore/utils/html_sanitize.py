"""The HTML allowlist shared by the web UI and the standalone HTML export (#169).

One allowlist, one sanitiser: the export renders full Markdown now, so it needs
the same protection the web view has, and two copies of a security list drift.
"""

from __future__ import annotations

import nh3

SANITIZE_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "u",
    "code",
    "pre",
    "a",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "div",
    "span",
    # Used by tool-call / thinking collapsible blocks generated internally
    "details",
    "summary",
}

SANITIZE_ATTRS = {
    "a": {"href", "title"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
    "details": {"class"},
    "summary": {"class"},
}

SANITIZE_URL_SCHEMES = {"http", "https", "mailto"}


def sanitize_html(rendered_html: str) -> str:
    """Strip everything outside the allowlist (scripts, handlers, odd schemes)."""
    return nh3.clean(
        rendered_html or "",
        tags=SANITIZE_TAGS,
        attributes=SANITIZE_ATTRS,
        url_schemes=SANITIZE_URL_SCHEMES,
        strip_comments=True,
        link_rel=None,
    )
