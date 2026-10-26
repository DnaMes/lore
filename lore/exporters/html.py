"""Standalone single-file HTML exporter for a single session.

Renders a :class:`UnifiedSession` into one self-contained HTML document:
all CSS is inlined in a ``<style>`` block, no external assets, no CDN, and
no JavaScript is required to read the conversation. Every piece of session
content is escaped with :func:`html.escape` because session content is
untrusted.
"""

from __future__ import annotations

import html
import re

from ..core.models import Role, UnifiedSession
from .tool_calls import canonical_tool_calls, tool_call_html

_INLINE_CSS = """\
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 0 4rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.6;
  color: #1f2933;
  background: #f4f5f7;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 1.5rem; }
header.session {
  background: #1f2933;
  color: #f4f5f7;
  padding: 1.5rem;
  border-radius: 8px;
  margin-bottom: 1.5rem;
}
header.session h1 { margin: 0 0 0.5rem; font-size: 1.4rem; }
header.session .meta { font-size: 0.85rem; color: #9aa5b1; }
header.session .meta span { margin-right: 1rem; white-space: nowrap; }
.message {
  background: #ffffff;
  border: 1px solid #e4e7eb;
  border-radius: 8px;
  margin-bottom: 1rem;
  overflow: hidden;
}
.message .role {
  padding: 0.4rem 1rem;
  font-size: 0.78rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.message .body { padding: 1rem; }
.message .body p { margin: 0 0 0.75rem; white-space: pre-wrap; word-wrap: break-word; }
.message .body p:last-child { margin-bottom: 0; }
.role-user .role { background: #e6f0ff; color: #2358a8; }
.role-assistant .role { background: #e9f7ef; color: #1f7a4d; }
.role-tool .role { background: #fdf0e1; color: #a05a17; }
.role-system .role,
.role-info .role { background: #eef0f2; color: #52606d; }
.time { float: right; font-weight: 400; opacity: 0.7; }
pre {
  background: #f4f5f7;
  border: 1px solid #e4e7eb;
  border-radius: 6px;
  padding: 0.75rem 1rem;
  overflow-x: auto;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 13px;
  line-height: 1.45;
}
code {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 0.88em;
  background: #f4f5f7;
  border-radius: 3px;
  padding: 0.1em 0.3em;
}
pre code { background: none; padding: 0; }
details {
  margin: 0.5rem 0;
  border: 1px solid #e4e7eb;
  border-radius: 6px;
  background: #fafbfc;
}
details summary {
  cursor: pointer;
  padding: 0.4rem 0.75rem;
  font-size: 0.85rem;
  font-weight: 600;
  color: #52606d;
}
details .detail-body { padding: 0 0.75rem 0.5rem; }
footer.session {
  text-align: center;
  font-size: 0.78rem;
  color: #9aa5b1;
  margin-top: 1.5rem;
}
"""

_CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def _render_content(content: str) -> str:
    """Render escaped message text, turning ```fenced``` blocks into <pre>."""
    if not content:
        return ""

    parts: list[str] = []
    last = 0
    for match in _CODE_FENCE_RE.finditer(content):
        prefix = content[last : match.start()]
        if prefix.strip():
            parts.append(_render_paragraphs(prefix))
        code = match.group(2)
        parts.append(f"<pre><code>{html.escape(code)}</code></pre>")
        last = match.end()

    tail = content[last:]
    if tail.strip() or not parts:
        parts.append(_render_paragraphs(tail))

    return "\n".join(p for p in parts if p)


def _render_paragraphs(text: str) -> str:
    """Escape text and split blank-line-separated blocks into <p> elements."""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return ""
    return "\n".join(f"<p>{html.escape(block.strip())}</p>" for block in blocks)


def _render_message(role_class: str, role_label: str, time_str: str, body: str) -> str:
    return (
        f'<div class="message role-{role_class}">'
        f'<div class="role">{html.escape(role_label)}'
        f'<span class="time">{html.escape(time_str)}</span></div>'
        f'<div class="body">{body}</div>'
        f"</div>"
    )


def render_session_html(session: UnifiedSession) -> str:
    """Render a :class:`UnifiedSession` into a standalone HTML document string."""
    title = session.title or session.summary or f"Session {session.session_id[:8]}"

    meta_bits: list[str] = [f"<span>Tool: {html.escape(session.tool.value)}</span>"]
    if session.project_path:
        meta_bits.append(f"<span>Project: {html.escape(session.project_path)}</span>")
    meta_bits.append(
        f"<span>Created: {html.escape(session.created_at.strftime('%Y-%m-%d %H:%M'))}</span>"
    )
    meta_bits.append(f"<span>Messages: {html.escape(str(session.message_count))}</span>")
    title_source = getattr(session.title_source, "value", session.title_source)
    if title_source and title_source != "native":
        meta_bits.append(f"<span>Title source: {html.escape(title_source)} (derived)</span>")
    if session.git_branch:
        meta_bits.append(f"<span>Branch: {html.escape(session.git_branch)}</span>")

    rows: list[str] = []
    for msg in session.messages:
        role = msg.role if isinstance(msg.role, Role) else Role.INFO
        try:
            time_str = msg.timestamp.strftime("%H:%M:%S")
        except (AttributeError, ValueError):
            time_str = ""

        body_parts: list[str] = []
        rendered = _render_content(msg.content or "")
        if rendered:
            body_parts.append(rendered)

        # Canonical tool-call envelope (#166), same shape as the Markdown export.
        for call in canonical_tool_calls(msg):
            body_parts.append(tool_call_html(call))

        if msg.reasoning:
            body_parts.append(
                "<details><summary>Reasoning</summary>"
                f'<div class="detail-body">{_render_content(msg.reasoning)}</div>'
                "</details>"
            )

        body = "\n".join(body_parts) or "<p></p>"
        rows.append(_render_message(role.value, role.value.title(), time_str, body))

    # Derived metadata is labelled, never presented as captured content (#165).
    summary_block = (
        '<p class="summary"><strong>Summary (derived, not transcript content):</strong> '
        f"{html.escape(session.summary)}</p>"
        if session.summary
        else ""
    )
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{_INLINE_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="session">
<h1>{html.escape(title)}</h1>
<div class="meta">{"".join(meta_bits)}</div>
{summary_block}
</header>
<main>
{chr(10).join(rows) or '<p class="empty">No messages were captured for this session.</p>'}
</main>
<footer class="session">Exported by lore &middot; session {html.escape(session.session_id)}</footer>
</div>
</body>
</html>
"""
    return doc
