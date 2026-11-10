"""Tests for the "Recently in this project" block of ``lore memory context`` (B4)."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from datetime import datetime, timezone

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import mcp
from lore.services.memory_context import (
    build_memory_context,
    select_recent_sessions,
)
from lore.storage import add_memory, initialise
from lore.storage.summaries import save_summary
from lore.storage.writer import v2_db_path, write_sessions

PROJECT = "/home/dnames/projects/demo-app"
_T_BASE = datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)
DEFAULT_URL = "https://lore.local.erdlabs.com"


@pytest.fixture
def recent_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("LORE_WEB_URL", raising=False)


def _context_args(tmp_path, **overrides) -> Namespace:
    base = {
        "output_dir": str(tmp_path),
        "memory_action": "context",
        "project": PROJECT,
        "budget": 2000,
        "tool": None,
        "format": "text",
        "limit": 40,
        "no_recent": False,
    }
    base.update(overrides)
    return Namespace(**base)


def _store_session(
    tmp_path,
    sid: str,
    *,
    day: int,
    project: str = PROJECT,
    origin: str = "interactive",
    title: str | None = None,
    prompt: str = "Please fix the login redirect bug",
    tool: Tool = Tool.CLAUDE_CODE,
) -> None:
    stamp = _T_BASE.replace(day=day)
    session = UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=stamp,
        last_updated=stamp,
        project_path=project,
        origin=origin,
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=prompt, timestamp=stamp)],
    )
    db = v2_db_path(tmp_path)
    initialise(db)
    write_sessions(db, [session], extras={sid: {"prompt_outline": prompt}})


def _store_summary(tmp_path, sid: str, one_liner: str) -> None:
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{sid}",
        session_id=sid,
        fingerprint="fp",
        model="test/model",
        prompt_version=1,
        compressor_version=1,
        payload={"one_liner": one_liner},
    )


def _add_memory(tmp_path, title: str, body: str) -> None:
    add_memory(tmp_path, "fact", title, body, status="confirmed")


def test_recent_selects_last_three_of_this_project_newest_first(tmp_path, recent_env):
    for day, sid in enumerate(["s1", "s2", "s3", "s4", "s5"], start=1):
        _store_session(tmp_path, sid, day=day, title=f"title {sid}")
    _store_session(tmp_path, "other", day=9, project="/home/dnames/projects/other")

    rows = select_recent_sessions(tmp_path, scope_project=PROJECT)
    assert [r["id"] for r in rows] == ["s5", "s4", "s3"]
    assert rows[0]["title"] == "title s5"
    assert rows[0]["tool"] == "claude-code"
    assert rows[0]["date"] == "2026-11-05"


def test_recent_excludes_automated_origins_and_current_session(tmp_path, recent_env, monkeypatch):
    _store_session(tmp_path, "human", day=1)
    _store_session(tmp_path, "cron-run", day=2, origin="cron")
    _store_session(tmp_path, "exec-run", day=3, origin="exec")
    _store_session(tmp_path, "sub-run", day=4, origin="subagent")
    _store_session(tmp_path, "running", day=5)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "running")

    rows = select_recent_sessions(tmp_path, scope_project=PROJECT)
    assert [r["id"] for r in rows] == ["human"]


def test_recent_stored_summary_wins_over_first_prompt(tmp_path, recent_env):
    _store_session(tmp_path, "with", day=2, prompt="raw prompt text")
    _store_session(tmp_path, "without", day=1, prompt="only the prompt")
    _store_summary(tmp_path, "with", "Fixed the redirect loop")

    rows = {r["id"]: r for r in select_recent_sessions(tmp_path, scope_project=PROJECT)}
    assert rows["with"]["summary"] == "Fixed the redirect loop"
    assert rows["without"]["summary"] is None
    assert rows["without"]["first_prompt"] == "only the prompt"


def test_recent_first_prompt_is_one_line_and_truncated(tmp_path, recent_env):
    _store_session(tmp_path, "long", day=1, prompt="line one of the prompt " + "word " * 80)
    row = select_recent_sessions(tmp_path, scope_project=PROJECT)[0]
    assert "\n" not in row["first_prompt"]
    assert len(row["first_prompt"]) <= 120


def test_recent_empty_without_store_or_project(tmp_path, recent_env):
    assert select_recent_sessions(tmp_path, scope_project=PROJECT) == []
    _store_session(tmp_path, "s1", day=1)
    assert select_recent_sessions(tmp_path, scope_project=None) == []


def test_recent_link_uses_lore_web_url_env(tmp_path, recent_env, monkeypatch):
    _store_session(tmp_path, "s1", day=1)
    row = select_recent_sessions(tmp_path, scope_project=PROJECT)[0]
    assert row["url"] == f"{DEFAULT_URL}/session/s1"

    monkeypatch.setenv("LORE_WEB_URL", "http://lore.test:5000/")
    row = select_recent_sessions(tmp_path, scope_project=PROJECT)[0]
    assert row["url"] == "http://lore.test:5000/session/s1"

    monkeypatch.setenv("LORE_WEB_URL", "")
    row = select_recent_sessions(tmp_path, scope_project=PROJECT)[0]
    assert row["url"] is None


def test_build_text_shows_recent_block_after_memories(tmp_path, recent_env):
    _add_memory(tmp_path, "a memory", "Remember this.")
    _store_session(tmp_path, "s1", day=1, title="Login work", prompt="fix login")
    _store_summary(tmp_path, "s1", "Fixed login redirect")

    text = build_memory_context(tmp_path, scope_project=PROJECT, budget=2000)
    assert "a memory" in text
    assert "Recently in this project:" in text
    assert text.index("a memory") < text.index("Recently in this project:")
    assert '- 2026-11-01 claude-code "Login work": Fixed login redirect' in text
    assert f"{DEFAULT_URL}/session/s1" in text


def test_build_text_without_memories_still_shows_recent(tmp_path, recent_env):
    _store_session(tmp_path, "s1", day=1, prompt="fix login")
    text = build_memory_context(tmp_path, scope_project=PROJECT, budget=2000)
    assert text.startswith("Recently in this project:")
    assert "fix login" in text


def test_build_no_recent_flag_disables_block(tmp_path, recent_env):
    _add_memory(tmp_path, "a memory", "Remember this.")
    _store_session(tmp_path, "s1", day=1)
    text = build_memory_context(tmp_path, scope_project=PROJECT, include_recent=False)
    assert "a memory" in text
    assert "Recently" not in text


@pytest.mark.parametrize("output_format", ["text", "json"])
@pytest.mark.parametrize("budget", [4, 8, 40, 120, 400, 2000])
def test_build_stays_inside_budget_and_recent_share(tmp_path, recent_env, budget, output_format):
    for i in range(12):
        _add_memory(tmp_path, f"memory {i}", "A reasonably long sentence for sizing.")
    for day in range(1, 4):
        _store_session(tmp_path, f"s{day}", day=day, prompt="x " * 200)

    text = build_memory_context(
        tmp_path, scope_project=PROJECT, budget=budget, output_format=output_format
    )
    assert len(text) <= budget * 4
    if output_format == "json":
        return
    if "Recently in this project:" in text:
        recent_block = text[text.index("Recently in this project:") :]
        # The recent block is capped at a quarter of the budget (token estimate, ceil(len/4)).
        assert -(-len(recent_block) // 4) <= budget // 4
    if budget >= 120:
        assert "- [fact #" in text  # memory items keep priority over the recent block


def test_build_json_carries_recent_sessions_field(tmp_path, recent_env):
    _store_session(tmp_path, "s1", day=1, title="Login work", prompt="fix login")
    _store_summary(tmp_path, "s1", "Fixed login redirect")

    raw = build_memory_context(tmp_path, scope_project=PROJECT, budget=2000, output_format="json")
    payload = json.loads(raw)
    assert payload["entries"] == []
    assert payload["cut"] == 0
    assert payload["recent_sessions"] == [
        {
            "id": "s1",
            "tool": "claude-code",
            "date": "2026-11-01",
            "title": "Login work",
            "summary": "Fixed login redirect",
            "first_prompt": "fix login",
            "url": f"{DEFAULT_URL}/session/s1",
        }
    ]


def test_json_recent_field_is_empty_list_when_disabled(tmp_path, recent_env):
    _add_memory(tmp_path, "a memory", "Remember this.")
    _store_session(tmp_path, "s1", day=1)
    raw = build_memory_context(
        tmp_path, scope_project=PROJECT, output_format="json", include_recent=False
    )
    payload = json.loads(raw)
    assert payload["recent_sessions"] == []
    assert len(payload["entries"]) == 1


def test_cli_context_no_recent_flag(tmp_path, recent_env, capsys):
    _store_session(tmp_path, "s1", day=1, prompt="fix login")
    lore_cli.cmd_memory(_context_args(tmp_path, no_recent=True))
    assert capsys.readouterr().out == ""
    lore_cli.cmd_memory(_context_args(tmp_path))
    assert "Recently in this project:" in capsys.readouterr().out


def test_mcp_list_recent_sessions_includes_summary_when_present(tmp_path, recent_env, monkeypatch):
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "1")
    server = mcp.create_server()
    _store_session(tmp_path, "with", day=2)
    _store_session(tmp_path, "without", day=1)
    _store_summary(tmp_path, "with", "Fixed the redirect loop")

    handler = server.tools["list_recent_sessions"]["handler"]
    payload = json.loads(asyncio.run(handler({"limit": 10})))
    by_id = {s["id"]: s for s in payload["sessions"]}
    assert by_id["with"]["summary"] == "Fixed the redirect loop"
    assert "summary" not in by_id["without"]


def test_text_line_uses_title_alone_when_no_summary():
    """Without a stored summary the title already names the session; the raw first prompt
    is often injected boilerplate (skill bodies, slash-command text), so it stays out."""
    from lore.services.memory_context import _recent_line

    rec = {
        "date": "2026-11-04",
        "tool": "claude-code",
        "title": "Download-Manager für Chromium",
        "summary": None,
        "first_prompt": "Base directory for this skill: /home/x/.claude/skills/systematic-debugging",
        "url": "",
    }
    assert _recent_line(rec) == '- 2026-11-04 claude-code "Download-Manager für Chromium"'
    rec["title"] = ""
    assert _recent_line(rec).endswith(
        ": Base directory for this skill: /home/x/.claude/skills/systematic-debugging"
    )
    rec["summary"] = "Added a download manager"
    assert _recent_line(rec).endswith(": Added a download manager")
