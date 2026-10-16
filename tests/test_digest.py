"""Tests for the activity digest (issue #43).

`build_digest` is pure (index records + cutoff -> Digest), so these tests
feed synthetic records directly with no disk access.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lore.digest import Digest, build_digest, format_digest

NOW = datetime(2026, 5, 17, 12, 0, 0)


def _rec(
    updated: str,
    tool: str = "claude-code",
    project: str = "/home/u/proj",
    messages: int = 10,
    prompts: int = 4,
) -> dict:
    """Build one synthetic index session record."""
    return {
        "id": f"s-{updated}-{tool}",
        "tool": tool,
        "project": project,
        "messages": messages,
        "prompts": prompts,
        "created": updated,
        "updated": updated,
    }


# ---------------------------------------------------------------------------
# build_digest
# ---------------------------------------------------------------------------


def test_empty_when_no_sessions():
    digest = build_digest([], since=NOW - timedelta(days=7), until=NOW)
    assert digest.is_empty()
    assert digest.total_sessions == 0


def test_sessions_outside_window_excluded():
    records = [
        _rec("2026-05-15T10:00:00"),  # inside
        _rec("2026-04-01T10:00:00"),  # too old
        _rec("2026-06-01T10:00:00"),  # in the future, past `until`
    ]
    digest = build_digest(records, since=NOW - timedelta(days=7), until=NOW)
    assert digest.total_sessions == 1


def test_totals_summed():
    records = [
        _rec("2026-05-15T10:00:00", messages=10, prompts=4),
        _rec("2026-05-16T10:00:00", messages=5, prompts=2),
    ]
    digest = build_digest(records, since=NOW - timedelta(days=7), until=NOW)
    assert digest.total_sessions == 2
    assert digest.total_messages == 15
    assert digest.total_prompts == 6


def test_by_day_grouped_and_sorted():
    records = [
        _rec("2026-05-16T09:00:00"),
        _rec("2026-05-15T09:00:00"),
        _rec("2026-05-16T18:00:00"),
    ]
    digest = build_digest(records, since=NOW - timedelta(days=7), until=NOW)
    assert list(digest.by_day.keys()) == ["2026-05-15", "2026-05-16"]
    assert digest.by_day["2026-05-16"] == 2


def test_by_tool_counts():
    records = [
        _rec("2026-05-15T10:00:00", tool="claude-code"),
        _rec("2026-05-15T11:00:00", tool="codex"),
        _rec("2026-05-16T10:00:00", tool="claude-code"),
    ]
    digest = build_digest(records, since=NOW - timedelta(days=7), until=NOW)
    assert digest.by_tool == {"claude-code": 2, "codex": 1}


def test_top_projects_ranked_and_capped():
    records = []
    for i in range(6):
        # project p0 gets 6 sessions, p1 5, ... p5 1
        for _ in range(6 - i):
            records.append(_rec("2026-05-15T10:00:00", project=f"/home/u/p{i}"))
    digest = build_digest(records, since=NOW - timedelta(days=7), until=NOW, top_n_projects=3)
    assert len(digest.top_projects) == 3
    assert digest.top_projects[0] == ("/home/u/p0", 6)


def test_busiest_day_identified():
    records = [
        _rec("2026-05-15T10:00:00"),
        _rec("2026-05-16T10:00:00"),
        _rec("2026-05-16T11:00:00"),
    ]
    digest = build_digest(records, since=NOW - timedelta(days=7), until=NOW)
    assert digest.busiest_day == ("2026-05-16", 2)


def test_unparseable_timestamp_skipped():
    records = [
        _rec("2026-05-15T10:00:00"),
        {"id": "bad", "tool": "codex", "updated": "not-a-date", "messages": 1},
        {"id": "none", "tool": "codex", "messages": 1},  # no timestamp at all
    ]
    digest = build_digest(records, since=NOW - timedelta(days=30), until=NOW)
    assert digest.total_sessions == 1


def test_created_used_when_updated_missing():
    rec = {
        "id": "x",
        "tool": "cursor",
        "project": "/p",
        "messages": 2,
        "prompts": 1,
        "created": "2026-05-15T10:00:00",
    }
    digest = build_digest([rec], since=NOW - timedelta(days=7), until=NOW)
    assert digest.total_sessions == 1


def test_default_until_is_now():
    """Omitting `until` must not raise and should accept recent sessions."""
    recent = datetime.now() - timedelta(hours=1)
    rec = _rec(recent.strftime("%Y-%m-%dT%H:%M:%S"))
    digest = build_digest([rec], since=datetime.now() - timedelta(days=1))
    assert digest.total_sessions == 1


# ---------------------------------------------------------------------------
# format_digest
# ---------------------------------------------------------------------------


def test_format_text_contains_headline_and_counts():
    digest = build_digest([_rec("2026-05-15T10:00:00")], since=NOW - timedelta(days=7), until=NOW)
    out = format_digest(digest, fmt="text")
    assert "AI History Digest" in out
    assert "Sessions : 1" in out


def test_format_markdown_uses_headings():
    digest = build_digest([_rec("2026-05-15T10:00:00")], since=NOW - timedelta(days=7), until=NOW)
    out = format_digest(digest, fmt="markdown")
    assert out.startswith("# AI History Digest")
    assert "## Sessions by day" in out
    assert "## Top projects" in out


def test_format_empty_digest_text():
    digest = build_digest([], since=NOW - timedelta(days=7), until=NOW)
    out = format_digest(digest, fmt="text")
    assert "No sessions" in out


def test_format_empty_digest_markdown():
    digest = build_digest([], since=NOW - timedelta(days=7), until=NOW)
    out = format_digest(digest, fmt="markdown")
    assert out.startswith("# AI History Digest")
    assert "No sessions" in out


def test_format_rejects_unknown_format():
    digest = Digest(since=NOW, until=NOW)
    with pytest.raises(ValueError):
        format_digest(digest, fmt="xml")


def test_format_markdown_shortens_project_paths():
    digest = build_digest(
        [_rec("2026-05-15T10:00:00", project="/home/u/deep/nested/myproj")],
        since=NOW - timedelta(days=7),
        until=NOW,
    )
    out = format_digest(digest, fmt="markdown")
    assert "myproj" in out
    assert "/home/u/deep/nested/myproj" not in out
