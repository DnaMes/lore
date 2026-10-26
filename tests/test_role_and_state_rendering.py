"""Roles and execution states stay distinguishable in the transcript (#168).

Presentation used to collapse both axes: every non-user message was rendered as
an assistant turn, and a tool's state was reduced to ok/failed/None — so a system
message looked like the model talking and a cancelled or unmatched call was
indistinguishable from a finished one. Errors could disappear in a folded burst.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.interfaces.web_formatting import format_tool_calls
from lore.utils.text_processing import (
    TOOL_STATES,
    canonical_tool_state,
    tool_state_label,
)

START = datetime(2026, 8, 1, 9, 0, 0)


# ---------------------------------------------------------------------------
# one vocabulary for execution state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ok", "ok"),
        ("success", "ok"),
        ("completed", "ok"),
        ("failed", "failed"),
        ("error", "failed"),
        ("pending", "pending"),
        ("queued", "pending"),
        ("running", "running"),
        ("in_progress", "running"),
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("aborted", "cancelled"),
        ("unmatched", "unmatched"),
        ("orphaned", "unmatched"),
        (None, "unknown"),
        ("", "unknown"),
        ("weird-state", "unknown"),
    ],
)
def test_every_state_word_maps_to_the_vocabulary(raw, expected):
    assert canonical_tool_state(raw) == expected
    assert expected in TOOL_STATES


def test_is_error_wins_over_a_successful_status():
    assert canonical_tool_state("completed", is_error=True) == "failed"


def test_states_are_not_silently_equivalent():
    """A pending run must not read like a finished one."""
    assert canonical_tool_state("pending") != canonical_tool_state("ok")
    assert canonical_tool_state("cancelled") != canonical_tool_state("failed")
    assert canonical_tool_state("unmatched") != canonical_tool_state("failed")


def test_labels_are_distinct_per_state():
    labels = {tool_state_label(state) for state in TOOL_STATES}
    assert len(labels) == len(TOOL_STATES)


# ---------------------------------------------------------------------------
# the rendered chip carries the state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["ok", "failed", "pending", "running", "cancelled", "unknown"])
def test_tool_chip_exposes_its_state(state):
    markup = format_tool_calls([{"name": "Bash", "status": state}])
    assert f'data-status="{state}"' in markup


def test_folding_a_burst_keeps_the_failure_visible():
    """A long burst is folded, but a failed call inside it stays marked."""
    calls = [
        {"name": "Bash", "status": "ok", "input": {"command": "ls"}},
        {"name": "Bash", "status": "ok", "input": {"command": "pwd"}},
        {"name": "Bash", "status": "ok", "input": {"command": "whoami"}},
        {
            "name": "Bash",
            "status": "failed",
            "is_error": True,
            "input": {"command": "rm -rf /nope"},
        },
    ]
    markup = format_tool_calls(calls)
    assert 'data-status="failed"' in markup
    assert "✗" in markup


# ---------------------------------------------------------------------------
# the rendered transcript says who spoke
# ---------------------------------------------------------------------------


def _session() -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="ses_roles",
        created_at=START,
        last_updated=START + timedelta(minutes=5),
        project_path="/repo/lore",
        title="Roles",
        messages=[
            UnifiedMessage(role=Role.USER, content="run the tests", timestamp=START),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="running them now",
                timestamp=START + timedelta(minutes=1),
            ),
            UnifiedMessage(
                role=Role.TOOL,
                content="pytest output: 3 failed",
                timestamp=START + timedelta(minutes=2),
            ),
            UnifiedMessage(
                role=Role.SYSTEM,
                content="context compacted by the harness",
                timestamp=START + timedelta(minutes=3),
            ),
        ],
    )


@pytest.fixture()
def client(monkeypatch, tmp_path):
    session = _session()
    index_payload = {
        "sessions": [
            {
                "id": session.session_id,
                "tool": session.tool.value,
                "title": session.title,
                "created": session.created_at.isoformat(),
                "updated": session.last_updated.isoformat(),
                "project": session.project_path,
                "messages": len(session.messages),
                "prompts": 1,
            }
        ],
        "stats": {},
    }
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_index", lambda: index_payload)
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [session])
    with web.app.test_client() as test_client:
        yield test_client


def test_system_message_is_labelled_and_marked(client):
    page = client.get("/session/ses_roles?live=1").get_data(as_text=True)
    assert 'data-role="system"' in page
    assert ">System<" in page
    assert "context compacted by the harness" in page


def test_tool_message_is_labelled_and_marked(client):
    page = client.get("/session/ses_roles?live=1").get_data(as_text=True)
    assert 'data-role="tool"' in page
    assert "Tool result" in page
    assert "pytest output: 3 failed" in page


def test_user_and_assistant_keep_their_roles(client):
    page = client.get("/session/ses_roles?live=1").get_data(as_text=True)
    assert 'data-role="user"' in page
    assert 'data-role="assistant"' in page
    assert "Claude" in page


def test_exports_carry_the_original_role_names():
    from pathlib import Path

    from lore.exporters.markdown import MarkdownExporter

    markdown = MarkdownExporter(Path("."))._generate_markdown(_session())
    assert "### User" in markdown
    assert "### Assistant" in markdown
    assert "### Tool" in markdown
    assert "### System" in markdown
