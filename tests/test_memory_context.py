"""Tests for context memory selection and ``lore memory show``."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

import lore_cli
from lore.services.memory_context import (
    build_memory_context,
    render_context_memories,
    select_context_memories,
)
from lore.storage import add_memory, record_feedback, supersede_memory
from lore.storage.memory import set_memory_status
from lore.storage.schema import open_v2_connection
from lore.storage.writer import v2_db_path


def _show_args(tmp_path, memory_id: int) -> Namespace:
    return Namespace(output_dir=str(tmp_path), memory_action="show", memory_id=memory_id)


def _touch_updated(tmp_path, memory_id: int, stamp: str) -> None:
    conn = open_v2_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE memory SET updated = ? WHERE id = ?", (stamp, memory_id))
        conn.commit()
    finally:
        conn.close()


def test_select_excludes_non_confirmed_and_superseded(tmp_path):
    keep = add_memory(tmp_path, "fact", "keep", "active body")
    candidate = add_memory(tmp_path, "fact", "candidate", "active body", status="candidate")
    rejected = add_memory(tmp_path, "fact", "reject me", "active body", status="candidate")
    assert set_memory_status(tmp_path, rejected, "rejected") is True
    old = add_memory(tmp_path, "fact", "old", "active body", scope_project="/proj")
    new_id = supersede_memory(tmp_path, old, "new", "active body")

    selected = {e.id for e in select_context_memories(tmp_path, scope_project="/proj")}
    assert old not in selected
    assert new_id in selected
    assert candidate not in selected
    assert rejected not in selected

    global_ids = {e.id for e in select_context_memories(tmp_path)}
    assert keep in global_ids
    assert new_id not in global_ids


def test_select_skips_retired_markers(tmp_path):
    fine = add_memory(tmp_path, "fact", "fine", "still valid")
    add_memory(tmp_path, "fact", "gone OBSOLETE", "body")
    add_memory(tmp_path, "fact", "also gone", "marked RETIRED in text")
    ok = add_memory(tmp_path, "fact", "prefix ok", "notSUPERSEDEDword")
    _touch_updated(tmp_path, fine, "2026-02-01T00:00:00+00:00")
    _touch_updated(tmp_path, ok, "2026-01-01T00:00:00+00:00")

    titles = [e.title for e in select_context_memories(tmp_path)]
    assert titles == ["fine", "prefix ok"]


def test_select_ranking_project_scope_weight_kind_recency(tmp_path):
    project = str(tmp_path / "app")
    child = str(tmp_path / "app" / "pkg")
    other = str(tmp_path / "elsewhere")

    global_low = add_memory(tmp_path, "link", "global link", "body")
    project_lesson = add_memory(tmp_path, "lesson", "project lesson", "body", scope_project=project)
    child_fact = add_memory(tmp_path, "fact", "child fact", "body", scope_project=child)
    add_memory(tmp_path, "decision", "other project", "body", scope_project=other)

    rated = add_memory(tmp_path, "link", "rated global", "body")
    record_feedback(tmp_path, rated, "alice", 5)

    _touch_updated(tmp_path, global_low, "2020-01-01T00:00:00+00:00")
    _touch_updated(tmp_path, rated, "2026-01-02T00:00:00+00:00")
    _touch_updated(tmp_path, project_lesson, "2026-01-01T00:00:00+00:00")
    _touch_updated(tmp_path, child_fact, "2025-12-01T00:00:00+00:00")

    ordered = select_context_memories(tmp_path, scope_project=project)
    assert [e.title for e in ordered] == [
        "project lesson",
        "child fact",
        "rated global",
        "global link",
    ]


def test_select_respects_limit(tmp_path):
    for i in range(5):
        add_memory(tmp_path, "fact", f"m{i}", "body")
    assert len(select_context_memories(tmp_path, limit=2)) == 2


def test_select_scope_tool_filter(tmp_path):
    any_tool = add_memory(tmp_path, "fact", "any tool", "body", scope_tool=None)
    cursor_only = add_memory(tmp_path, "fact", "cursor only", "body", scope_tool="cursor")
    add_memory(tmp_path, "fact", "claude only", "body", scope_tool="claude")
    _touch_updated(tmp_path, any_tool, "2026-02-01T00:00:00+00:00")
    _touch_updated(tmp_path, cursor_only, "2026-01-01T00:00:00+00:00")

    titles = [e.title for e in select_context_memories(tmp_path, scope_tool="cursor")]
    assert titles == ["any tool", "cursor only"]


def test_cli_show_known_memory(tmp_path, capsys):
    mid = add_memory(
        tmp_path,
        "lesson",
        "Show title",
        "Show body",
        tags=["alpha", "beta"],
        source_session="sess-abc",
    )
    lore_cli.cmd_memory(_show_args(tmp_path, mid))
    out = capsys.readouterr().out
    assert "Show title" in out
    assert "Show body" in out
    assert "Tags: alpha, beta" in out
    assert "sess-abc" in out


def test_cli_show_unknown_memory_exits_one(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(_show_args(tmp_path, 4242))
    assert exc.value.code == 1
    assert "does not exist" in capsys.readouterr().err


def _context_args(tmp_path, **overrides) -> Namespace:
    base = {
        "output_dir": str(tmp_path),
        "memory_action": "context",
        "project": None,
        "budget": 2000,
        "tool": None,
        "format": "text",
        "limit": 40,
    }
    base.update(overrides)
    return Namespace(**base)


def _add_ranked(tmp_path, title: str, body: str = "body.", **kwargs) -> int:
    mid = add_memory(tmp_path, "fact", title, body, **kwargs)
    _touch_updated(tmp_path, mid, "2026-03-01T00:00:00+00:00")
    return mid


def test_render_respects_budget(tmp_path):
    for i in range(3):
        add_memory(tmp_path, "fact", f"t{i}", "one.", status="confirmed")
    ranked = select_context_memories(tmp_path, limit=3)
    text = render_context_memories(ranked, budget=12)
    memory_lines = [ln for ln in text.splitlines() if ln.startswith("- [")]
    assert len(memory_lines) == 2
    assert "(1 more" in text


def test_render_more_line_absent_when_not_cut(tmp_path):
    mid = add_memory(tmp_path, "fact", "solo", "only line.")
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    assert f"# {mid}" in text or f"#{mid}" in text
    assert " more;" not in text


def test_render_first_sentence_truncated_to_160_chars(tmp_path):
    long = "x" * 200 + ". never show this tail."
    mid = add_memory(tmp_path, "fact", "long", long)
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    line = [ln for ln in text.splitlines() if f"#{mid}" in ln][0]
    snippet = line.split(": ", 1)[1]
    assert len(snippet) == 160
    assert "never show" not in text


def test_render_replaces_bare_uuid(tmp_path):
    uid = "550e8400-e29b-41d4-a716-446655440000"
    add_memory(tmp_path, "fact", "uuid case", f"remember {uid} here.")
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    assert uid not in text
    assert "<uuid>" in text


def test_render_applies_redaction(tmp_path):
    secret = "sk-" + "a" * 24
    add_memory(tmp_path, "fact", "secret", f"token {secret} end.")
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    assert secret not in text
    assert "[REDACTED:openai_key]" in text


def test_build_memory_context_empty_store_prints_nothing(tmp_path, capsys):
    assert build_memory_context(tmp_path) == ""
    lore_cli.cmd_memory(_context_args(tmp_path))
    assert capsys.readouterr().out == ""


def test_render_json_shape(tmp_path):
    mid = add_memory(tmp_path, "lesson", "json title", "First. Second.")
    ranked = select_context_memories(tmp_path, limit=1)
    raw = render_context_memories(ranked, budget=2000, output_format="json")
    payload = json.loads(raw)
    assert payload["cut"] == 0
    assert len(payload["entries"]) == 1
    row = payload["entries"][0]
    assert row == {
        "id": mid,
        "kind": "lesson",
        "title": "json title",
        "first_sentence": "First.",
        "project": None,
    }


def test_cli_context_end_to_end(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _add_ranked(tmp_path, "hook memory", "visible sentence.")
    lore_cli.cmd_memory(_context_args(tmp_path, budget=2000))
    out = capsys.readouterr().out
    assert "hook memory" in out
    assert "visible sentence." in out


def test_render_bold_leadin_newline_is_one_physical_line(tmp_path):
    body = "**Lead-in:**\nrest of first sentence here."
    add_memory(tmp_path, "fact", "bold", body)
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    memory_lines = [ln for ln in text.splitlines() if ln.startswith("- [")]
    assert len(memory_lines) == 1
    assert "Lead-in:" in memory_lines[0]
    assert "rest of first sentence here." in memory_lines[0]


def test_render_unescapes_backslash_quotes(tmp_path):
    add_memory(tmp_path, "fact", "quotes", r"She said \"hello\" today.")
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    assert '\\"' not in text
    assert '"hello"' in text


def test_render_decodes_html_entities(tmp_path):
    add_memory(tmp_path, "fact", "entities", "Use &lt;tag&gt; and &amp; more.")
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=2000)
    assert "&lt;" not in text
    assert "<tag>" in text
    assert "&amp;" not in text


def test_render_text_lines_start_with_dash_bracket_or_paren(tmp_path):
    for i in range(3):
        add_memory(tmp_path, "fact", f"line{i}", f"body {i}.")
    ranked = select_context_memories(tmp_path, limit=3)
    text = render_context_memories(ranked, budget=12)
    for line in text.splitlines():
        assert line.startswith("- [") or line.startswith("(")


def test_render_first_entry_over_budget_still_present(tmp_path):
    title = "huge"
    body = "word " * 80 + "end."
    mid = add_memory(tmp_path, "fact", title, body)
    ranked = select_context_memories(tmp_path, limit=1)
    text = render_context_memories(ranked, budget=8)
    assert text.strip()
    assert f"#{mid}" in text
    assert text.splitlines()[0].startswith("- [")


def test_context_does_not_call_embed_text(tmp_path, monkeypatch):
    import lore.storage.embeddings as emb

    _add_ranked(tmp_path, "no embed", "plain.")

    def _fail(*_args, **_kwargs):
        raise AssertionError("embed_text must not run for memory context")

    monkeypatch.setattr(emb, "embed_text", _fail)
    lore_cli.cmd_memory(_context_args(tmp_path))


def test_first_sentence_stops_at_paragraph_break() -> None:
    from lore.services.memory_context import _first_sentence

    body = "User chose medium risk for routing\n\nUser explicitly chose **medium** when asked."
    assert _first_sentence(body) == "User chose medium risk for routing"
