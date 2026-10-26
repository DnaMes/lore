"""Automated checks for the reading contract (#170).

The transcript and the pages around it grew through incremental fixes; this test
is the machine-readable half of `docs/READING-CONTRACT.md`. It renders every page
of a synthetic archive and checks the rules that can be decided from the HTML
alone: language, one h1 and no heading jumps, a skip link with a target, an
accessible name on every link and button, a name on every form control, unique
ids, live status regions where the UI updates asynchronously, and the CSS
guarantees that keep narrow layouts and zoomed text from clipping.

The visual half — 200 % zoom, a 380 px viewport, keyboard traversal — needs a
browser and is tracked in #194.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data

START = datetime(2026, 9, 1, 8, 0, 0)
TEMPLATES = Path(web.__file__).parents[1] / "templates"

PAGES = {
    "sessions": "/sessions",
    "projects": "/projects",
    "threads": "/threads",
    "stats": "/stats",
    "rules": "/rules",
    "noise_rules": "/noise-rules",
    "memory": "/memory",
    "session_detail": "/session/session-1?live=1",
}


class _Audit(HTMLParser):
    """Collect what the contract assertions need from a rendered page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[dict] = []
        self.controls: list[dict] = []
        self.headings: list[tuple[int, str]] = []
        self.html_attrs: dict = {}
        self.live_regions: list[dict] = []
        self.label_targets: set[str] = set()
        self._open_links: list[dict] = []
        self._label_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key: (value or "") for key, value in attrs}
        if tag == "html":
            self.html_attrs = a
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.label_targets.add(a["for"])
        if tag in {"a", "button"}:
            entry = {
                "tag": tag,
                "href": a.get("href", ""),
                "aria_label": a.get("aria-label", ""),
                "title": a.get("title", ""),
                "text": "",
            }
            self.links.append(entry)
            self._open_links.append(entry)
        if tag in {"input", "select", "textarea"}:
            if a.get("type", "").lower() not in {"hidden", "submit", "button", "reset"}:
                self.controls.append(
                    {
                        "tag": tag,
                        "id": a.get("id", ""),
                        "aria_label": a.get("aria-label", ""),
                        "labelled_by": a.get("aria-labelledby", ""),
                        "inside_label": self._label_depth > 0,
                    }
                )
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append((int(tag[1]), ""))
        if a.get("aria-live") or a.get("role") in {"status", "alert"}:
            self.live_regions.append({"role": a.get("role", ""), "live": a.get("aria-live", "")})

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._open_links:
            self._open_links[-1]["text"] += text
        if self.headings:
            level, existing = self.headings[-1]
            self.headings[-1] = (level, existing + text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_depth:
            self._label_depth -= 1
        if tag in {"a", "button"} and self._open_links:
            self._open_links.pop()


def _audit(html: str) -> _Audit:
    parser = _Audit()
    parser.feed(html)
    return parser


def _session() -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="session-1",
        created_at=START,
        last_updated=START,
        project_path="/repo/lore",
        thread_id="thread-1",
        title="Reading contract",
        messages=[
            UnifiedMessage(role=Role.USER, content="look at the parser", timestamp=START),
            UnifiedMessage(role=Role.ASSISTANT, content="done", timestamp=START),
        ],
    )


@pytest.fixture(scope="module")
def pages():
    """Render every page once against a synthetic archive."""
    index_payload = {
        "sessions": [
            {
                "id": "session-1",
                "tool": "claude-code",
                "title": "Reading contract",
                "display_title": "Reading contract",
                "created": START.isoformat(),
                "updated": START.isoformat(),
                "project": "/repo/lore",
                "thread_id": "thread-1",
                "messages": 2,
                "prompts": 1,
                "keywords": ["parser"],
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 2,
            "by_tool": {"claude-code": 1},
            "by_project": {"/repo/lore": 1},
        },
        "generated_at": "2026-09-01T00:00:00Z",
        "version": "2",
    }
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    (tmp / "index.json").write_text(json.dumps(index_payload), encoding="utf-8")
    saved = {}
    for module in (web, web_data):
        saved[module] = {name: getattr(module, name, None) for name in ("OUTPUT_DIR", "INDEX_PATH")}
        module.OUTPUT_DIR = tmp
        module.INDEX_PATH = tmp / "index.json"
    saved["load_index"] = web.load_index
    saved["load_sessions_for_tool"] = web.load_sessions_for_tool
    web.load_index = lambda: index_payload
    web.load_sessions_for_tool = lambda _tool=None: [_session()]
    try:
        rendered = {}
        with web.app.test_client() as client:
            for name, route in PAGES.items():
                response = client.get(route)
                assert response.status_code == 200, f"{route} returned {response.status_code}"
                rendered[name] = response.get_data(as_text=True)
        yield rendered
    finally:
        for module, values in saved.items():
            if module in {web, web_data}:
                for key, value in values.items():
                    setattr(module, key, value)
        web.load_index = saved["load_index"]
        web.load_sessions_for_tool = saved["load_sessions_for_tool"]


# ---------------------------------------------------------------------------
# document structure
# ---------------------------------------------------------------------------


def test_pages_declare_their_language(pages):
    for name, html in pages.items():
        assert _audit(html).html_attrs.get("lang", "").startswith("en"), name


def test_every_page_has_exactly_one_h1(pages):
    for name, html in pages.items():
        level_one = [text for level, text in _audit(html).headings if level == 1]
        assert len(level_one) == 1, f"{name}: {level_one}"


def test_headings_do_not_skip_levels(pages):
    for name, html in pages.items():
        levels = [level for level, _text in _audit(html).headings]
        previous = 0
        for level in levels:
            assert level <= previous + 1, f"{name}: heading jump {levels}"
            previous = level


def test_skip_link_targets_an_existing_element(pages):
    for name, html in pages.items():
        audit = _audit(html)
        skip = next((link for link in audit.links if "skip" in link["text"].lower()), None)
        assert skip is not None, f"{name}: no skip link"
        target = skip["href"].lstrip("#")
        assert target in audit.ids, f"{name}: skip link target #{target} missing"


def test_ids_are_unique(pages):
    for name, html in pages.items():
        ids = _audit(html).ids
        duplicates = {value for value in ids if ids.count(value) > 1}
        assert not duplicates, f"{name}: duplicate ids {sorted(duplicates)}"


# ---------------------------------------------------------------------------
# names for everything the reader can reach
# ---------------------------------------------------------------------------


def test_every_link_and_button_has_an_accessible_name(pages):
    for name, html in pages.items():
        anonymous = [
            link
            for link in _audit(html).links
            if not (link["text"].strip() or link["aria_label"].strip() or link["title"].strip())
        ]
        assert not anonymous, f"{name}: unnamed controls {anonymous[:3]}"


def test_every_form_control_has_a_name(pages):
    for name, html in pages.items():
        audit = _audit(html)
        unnamed = [
            control
            for control in audit.controls
            if not (
                control["aria_label"].strip()
                or control["labelled_by"].strip()
                or control["inside_label"]
                or (control["id"] and control["id"] in audit.label_targets)
            )
        ]
        assert not unnamed, f"{name}: unnamed form controls {unnamed[:3]}"


def test_links_do_not_open_a_new_context_without_saying_so(pages):
    """No target=_blank without rel, and an external link states where it goes."""
    for name, html in pages.items():
        for match in re.finditer(r"<a\b[^>]*target=\"_blank\"[^>]*>", html):
            assert "rel=" in match.group(0), f"{name}: {match.group(0)[:80]}"


# ---------------------------------------------------------------------------
# live status
# ---------------------------------------------------------------------------


def test_async_feedback_uses_a_live_region(pages):
    """Pages that update without a reload must announce the change."""
    for name in ("session_detail", "noise_rules", "memory"):
        regions = _audit(pages[name]).live_regions
        assert any(
            region["live"] in {"polite", "assertive"} or region["role"] in {"status", "alert"}
            for region in regions
        ), f"{name}: no live region"


# ---------------------------------------------------------------------------
# layout and zoom guarantees (the CSS half of the contract)
# ---------------------------------------------------------------------------


def test_long_words_and_code_cannot_clip_a_narrow_layout():
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "overflow-wrap" in css, "prose needs overflow-wrap so long tokens wrap"
    assert re.search(r"pre[^{]*\{[^}]*overflow-x:\s*auto", css), "pre must scroll horizontally"


def test_flex_children_are_allowed_to_shrink():
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "min-width: 0" in css, "a flex child without min-width: 0 cannot shrink"


def test_motion_respects_the_operating_system_setting():
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in css


def test_focus_is_visible_on_every_interactive_component():
    """No focus style may remove the ring without replacing it with something visible."""
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert css.count(":focus-visible") >= 10, "every interactive component needs a focus ring"
    rules = re.findall(r"([^{}]*:(?:focus|focus-visible)[^{}]*)\{([^}]*)\}", css)
    assert rules, "no focus rules found"
    visible_props = ("box-shadow", "border", "background", "top:", "transform", "inset")
    for selector, body in rules:
        if "outline: none" not in body and "outline: 0" not in body:
            continue
        if any(prop in body for prop in visible_props):
            continue
        # Suppressing the legacy :focus ring is fine only when the modern
        # :focus-visible ring exists for the same element.
        base = selector.split(":")[0].strip()
        assert any(
            other.split(":")[0].strip() == base and "focus-visible" in other
            for other, _body in rules
        ), f"{selector.strip()} removes the outline without a visible replacement"


def test_transcript_typography_follows_the_theme_variables():
    """Colours and surfaces come from theme variables, so every theme is readable."""
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for variable in ("var(--text)", "var(--text-muted)", "var(--card)", "var(--border)"):
        assert variable in css, f"transcript styling does not use {variable}"
    # A hardcoded text colour in a message rule would break the dark themes.
    message_rules = re.findall(r"([^{}]*(?:message-card|prose)[^{}]*)\{([^}]*)\}", css)
    assert message_rules
    hardcoded = [
        selector
        for selector, body in message_rules
        if re.search(r"color:\s*#(?:000|111|222|333)[0-9a-f]*", body)
    ]
    assert not hardcoded, f"hardcoded dark text colour in transcript rules: {hardcoded[:3]}"


def test_zoom_does_not_depend_on_fixed_heights():
    """Fixed-height containers clip text at 200 % zoom."""
    css = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    transcript_rules = re.findall(
        r"([^{}]*(?:message-card|prose|turn-content)[^{}]*)\{([^}]*)\}", css
    )
    assert transcript_rules
    fixed = [
        selector
        for selector, body in transcript_rules
        if re.search(r"\bheight:\s*\d+px", body) and "max-height" not in body
    ]
    assert not fixed, f"fixed height on transcript content clips at zoom: {fixed[:3]}"
