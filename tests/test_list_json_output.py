"""``lore list --format json`` writes only the JSON array to stdout."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import BaseExtractor


class _OneSessionExtractor(BaseExtractor):
    @property
    def tool(self) -> Tool:
        return Tool.CLAUDE_CODE

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        messages = [
            UnifiedMessage(role=Role.USER, content="ask", timestamp=datetime(2026, 1, 1)),
            UnifiedMessage(role=Role.ASSISTANT, content="answer", timestamp=datetime(2026, 1, 1)),
        ]
        yield UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id="json-sess",
            created_at=datetime(2026, 1, 1),
            last_updated=datetime(2026, 1, 2),
            project_path="/proj",
            messages=messages,
        )


def _args(**extra) -> Namespace:
    base = {
        "tool": None,
        "project": None,
        "since": None,
        "limit": None,
        "offset": None,
        "format": "json",
        "include_automated": False,
        "tag": None,
    }
    base.update(extra)
    return Namespace(**base)


def test_list_json_stdout_is_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_OneSessionExtractor()])

    lore_cli.cmd_list(_args())
    captured = capsys.readouterr()

    rows = json.loads(captured.out)
    assert [row["session_id"] for row in rows] == ["json-sess"]
    assert "Total:" not in captured.out
    assert "Total: 1 sessions" in captured.err


def test_list_json_with_limit_stdout_is_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_OneSessionExtractor()])

    lore_cli.cmd_list(_args(limit=1, offset=0))
    captured = capsys.readouterr()

    assert len(json.loads(captured.out)) == 1
    assert "Showing 1 of 1 sessions" in captured.err


def test_list_table_still_reports_total_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_OneSessionExtractor()])

    lore_cli.cmd_list(_args(format="table"))

    assert "Total: 1 sessions" in capsys.readouterr().out
