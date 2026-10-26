"""The `lore search` CLI must route through the shared search service (#87).

Before PR 4 it instantiated ``SearchEngine(index.json)`` directly, bypassing
the v2 FTS store and hybrid semantic fusion — so CLI results could disagree
with the web UI and MCP. These tests pin the CLI to ``services.search_index``.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import v2_db_path, write_sessions


def _argns(**kw) -> argparse.Namespace:
    kw.setdefault("tool", None)
    kw.setdefault("project", None)
    return argparse.Namespace(**kw)


def _session(sid, title="T", body="hello world"):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1))],
    )


def test_cmd_search_uses_service_router(tmp_path, monkeypatch, capsys):
    """cmd_search calls services.index.search_index, not SearchEngine directly."""
    import lore_cli

    captured = {}

    def fake_search_index(index_path, query, deleted, **kwargs):
        captured["called"] = True
        captured["query"] = query
        captured["kwargs"] = kwargs
        return [
            {
                "session": {
                    "tool": "claude-code",
                    "title": "Hit",
                    "id": "abc123",
                    "created": "2026-01-01",
                    "messages": 3,
                },
                "score": 1.0,
            }
        ]

    monkeypatch.setattr("lore.services.index.search_index", fake_search_index)
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")  # an index must exist
    rc = lore_cli.cmd_search(_argns(query="anything", output_dir=str(tmp_path)))
    out = capsys.readouterr().out

    assert captured.get("called") is True
    assert captured["query"] == "anything"
    assert "Hit" in out
    assert rc is None  # cmd_search returns None on success


def test_cmd_search_finds_real_session_via_v2(tmp_path, capsys):
    """End-to-end: a session written to the v2 store is found by the CLI."""
    import lore_cli

    write_sessions(
        v2_db_path(tmp_path), [_session("s1", title="Kubernetes notes", body="deploying pods")]
    )
    lore_cli.cmd_search(_argns(query="kubernetes", output_dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert "Kubernetes notes" in out


def test_cmd_search_no_results_message(tmp_path, capsys, monkeypatch):
    import lore_cli

    # Keyword-only: semantic KNN has no distance cutoff, so it always returns
    # its nearest neighbours even for a nonsense query. Disable hybrid to assert
    # the genuine no-match message. (A relevance threshold is Phase-2 work.)
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    write_sessions(v2_db_path(tmp_path), [_session("s1", body="unrelated content")])
    lore_cli.cmd_search(_argns(query="zzznomatchzzz", output_dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert "No results found" in out
