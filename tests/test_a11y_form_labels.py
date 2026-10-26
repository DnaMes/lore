"""Accessible names for the form controls rendered by the templates (#210).

The filter form on ``/projects`` had four controls with no visible label, the
noise-rules preview textarea had only a placeholder, and the resume copy button
was created from a JavaScript string in ``session.html``. A control without an
accessible name is unusable with a screen reader, so this test renders the real
pages and checks every form control:

* it has an ``aria-label``, a ``<label for=...>`` or an enclosing ``<label>``,
* ``aria-label`` values are unique within a page,
* a control that already sits inside a ``<label>`` does not repeat the name in
  ``aria-label`` (that would double-announce it).

The static template text is also checked, because the resume button only exists
inside a JavaScript string and never appears in the rendered session HTML.
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "lore" / "templates"

FORM_TAGS = {"input", "select", "textarea"}
NON_LABELLED_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


class _ControlCollector(HTMLParser):
    """Collect form controls with their accessible name and label context."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[dict] = []
        self.label_targets: set[str] = set()
        self._label_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "label":
            self._label_depth += 1
            target = attributes.get("for")
            if target:
                self.label_targets.add(target)
        if tag in FORM_TAGS:
            if attributes.get("type", "").lower() in NON_LABELLED_INPUT_TYPES:
                return
            self.controls.append(
                {
                    "tag": tag,
                    "id": attributes.get("id", ""),
                    "name": attributes.get("name", ""),
                    "aria_label": attributes.get("aria-label", ""),
                    "inside_label": self._label_depth > 0,
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_depth:
            self._label_depth -= 1


def _collect(html: str) -> _ControlCollector:
    collector = _ControlCollector()
    collector.feed(html)
    return collector


def _accessible_name(control: dict, label_targets: set[str]) -> str:
    if control["aria_label"]:
        return control["aria_label"]
    if control["inside_label"]:
        return "<enclosing label>"
    if control["id"] and control["id"] in label_targets:
        return f"<label for={control['id']}>"
    return ""


def _assert_labelled(html: str, page: str) -> None:
    collector = _collect(html)
    assert collector.controls, f"no form controls found on {page}"
    unnamed = [c for c in collector.controls if not _accessible_name(c, collector.label_targets)]
    assert not unnamed, f"controls without an accessible name on {page}: {unnamed}"
    labels = [c["aria_label"] for c in collector.controls if c["aria_label"]]
    assert len(labels) == len(set(labels)), f"duplicate aria-label on {page}: {labels}"
    doubled = [c for c in collector.controls if c["aria_label"] and c["inside_label"]]
    assert not doubled, f"aria-label duplicates an enclosing <label> on {page}: {doubled}"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    fake_output = tmp_path / ".lore"
    fake_output.mkdir()
    fake_index = fake_output / "index.json"
    fake_index.write_text("{}", encoding="utf-8")
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="session-1",
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo/demo",
        thread_id="thread-1",
        title="Claude debugging session",
        messages=[
            UnifiedMessage(Role.USER, "Find the sqlite issue", datetime(2026, 4, 1, 10, 0, 0)),
            UnifiedMessage(
                Role.ASSISTANT, "I found the locking bug", datetime(2026, 4, 1, 10, 1, 0)
            ),
        ],
    )
    payload = {
        "sessions": [
            {
                "id": "session-1",
                "tool": "claude-code",
                "title": "Claude debugging session",
                "display_title": "Claude debugging session",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo/demo",
                "thread_id": "thread-1",
                "messages": 2,
                "prompts": 1,
                "tokens": 1234,
                "export_path": None,
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 2,
            "by_tool": {"claude-code": 1},
            "by_project": {"/repo/demo": 1},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }
    monkeypatch.setattr(web, "OUTPUT_DIR", fake_output)
    monkeypatch.setattr(web, "INDEX_PATH", fake_index)
    monkeypatch.setattr(web, "load_index", lambda: payload)
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [session])
    monkeypatch.setattr(
        web, "load_noise_rules", lambda: {"default": {"strip_ansi_sequences": True}}
    )
    monkeypatch.setattr(web, "sessions_list_now", lambda: datetime(2026, 10, 14, 12, 0, 0))
    with web.app.test_client() as test_client:
        yield test_client


@pytest.mark.parametrize("route", ["/projects", "/noise-rules", "/sessions"])
def test_every_form_control_has_a_unique_accessible_name(client, route):
    response = client.get(route)
    assert response.status_code == 200
    _assert_labelled(response.get_data(as_text=True), route)


def test_project_filter_controls_are_named_for_what_they_filter():
    """The four filter controls carry a name that says what they filter by."""
    html = (TEMPLATE_DIR / "projects.html").read_text(encoding="utf-8")
    names = [control["aria_label"] for control in _collect(html).controls if control["aria_label"]]
    assert names == ["Tool filter", "Tag filter", "Start date", "End date"]


def test_noise_rule_checkboxes_keep_their_enclosing_label():
    """Checkboxes stay inside their visible <label>; no aria-label is added."""
    html = (TEMPLATE_DIR / "noise_rules.html").read_text(encoding="utf-8")
    collector = _collect(html)
    checkboxes = [c for c in collector.controls if c["tag"] == "input"]
    assert checkboxes, "no checkbox rendered"
    assert all(c["inside_label"] for c in checkboxes)
    assert not any(c["aria_label"] for c in checkboxes)


def test_resume_copy_button_string_is_labelled():
    """The button is built in JavaScript, so check the template source."""
    source = (TEMPLATE_DIR / "session.html").read_text(encoding="utf-8")
    assert 'id="resume-copy-btn"' in source
    assert 'aria-label=\\"Copy resume command\\"' in source or (
        'id="resume-copy-btn" aria-label="Copy resume command"' in source
    )
