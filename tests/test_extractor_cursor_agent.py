"""Cursor agent-transcript JSONL / legacy plain-text coverage (B2.7a)."""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

from lore.core.models import Tool
from lore.extractors.cursor import CursorExtractor


def _projects(home: Path) -> Path:
    root = home / ".cursor" / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _agent_jsonl(
    home: Path,
    slug: str,
    session_id: str,
    lines: list[str | dict],
    *,
    flat: bool = False,
) -> Path:
    """Write agent transcript under slug; nested dir unless ``flat``."""
    if flat:
        base = _projects(home) / slug / "agent-transcripts"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{session_id}.jsonl"
    else:
        base = _projects(home) / slug / "agent-transcripts" / session_id
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{session_id}.jsonl"
    text = "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _agent_plain(home: Path, slug: str, session_id: str, body: str) -> Path:
    base = _projects(home) / slug / "agent-transcripts" / session_id
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{session_id}.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def _extract(home: Path) -> tuple[CursorExtractor, list]:
    extractor = CursorExtractor()
    sessions = list(extractor.extract_sessions())
    return extractor, sessions


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_parser_version_bumped():
    assert CursorExtractor.PARSER_VERSION == 2


def test_source_description_mentions_both_sources(home):
    desc = CursorExtractor().source_description()
    assert "state.vscdb" in desc
    assert "agent-transcripts" in desc


def test_available_when_only_agent_transcripts(home):
    _agent_jsonl(home, "slug-a", "sess-1", [{"role": "user", "message": {"content": "hi"}}])
    assert CursorExtractor().is_available() is True


def test_jsonl_text_tool_use_and_tool_event(home):
    _agent_jsonl(
        home,
        "proj-slug",
        "agent-001",
        [
            {"role": "user", "message": {"content": [{"type": "text", "text": "run grep"}]}},
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Calling tool."},
                        {"type": "tool_use", "name": "grep", "input": {"pattern": "foo"}},
                    ]
                },
            },
        ],
    )
    _, sessions = _extract(home)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool is Tool.CURSOR
    assert s.session_id == "agent-001"
    assert s.origin == "unknown"
    assert s.project_path is None
    assistant = s.messages[1]
    assert "[Tool: grep]" in assistant.content
    assert assistant.tool_events == [
        {"name": "grep", "status": "completed", "duration_ms": None, "is_error": False}
    ]


def test_user_query_wrapper_stripped(home):
    wrapped = "<user_query>inner prompt text</user_query>"
    _agent_jsonl(
        home,
        "slug-b",
        "wrap-1",
        [{"role": "user", "message": {"content": wrapped}}],
    )
    _, sessions = _extract(home)
    assert sessions[0].messages[0].content == "inner prompt text"


def test_content_plain_string(home):
    _agent_jsonl(
        home,
        "slug-c",
        "plain-str",
        [{"role": "user", "message": {"content": "hello as string"}}],
    )
    _, sessions = _extract(home)
    assert sessions[0].messages[0].content == "hello as string"


def test_status_records_ignored(home, monkeypatch):
    _agent_jsonl(
        home,
        "slug-d",
        "status-1",
        [
            {"type": "status", "status": "running"},
            {"role": "user", "message": {"content": "only user"}},
        ],
    )
    extractor, sessions = _extract(home)
    assert len(sessions) == 1
    assert len(sessions[0].messages) == 1
    assert extractor.discovery["records_unknown"] == 0


def test_legacy_plain_text_format(home):
    body = "user: first line\ncontinued\nassistant: reply line\n"
    _agent_plain(home, "slug-e", "legacy-1", body)
    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert sessions[0].messages[0].content == "first line\ncontinued"
    assert sessions[0].messages[1].content == "reply line"


def test_below_prompt_floor_skipped_and_counted(home, monkeypatch):
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    _agent_jsonl(
        home,
        "slug-f",
        "sparse-1",
        [{"role": "user", "message": {"content": "only one"}}],
    )
    extractor, sessions = _extract(home)
    assert sessions == []
    assert extractor.skip_counts.get("too_few_user_prompts") == 1


def test_duplicate_id_qualified_both_slugs_order_independent(monkeypatch, tmp_path):
    lines = [{"role": "user", "message": {"content": "shared id prompt"}}]

    def build(reverse: bool) -> set[str]:
        alt = tmp_path / ("rev" if reverse else "fwd")
        monkeypatch.setenv("HOME", str(alt))
        order = (
            [("slug-two", "dup-id"), ("slug-one", "dup-id")]
            if reverse
            else [
                ("slug-one", "dup-id"),
                ("slug-two", "dup-id"),
            ]
        )
        for slug, sid in order:
            _agent_jsonl(alt, slug, sid, lines)
        _, sessions = _extract(alt)
        return {s.session_id for s in sessions}

    assert build(False) == {"slug-one:dup-id", "slug-two:dup-id"}
    assert build(True) == {"slug-one:dup-id", "slug-two:dup-id"}


def test_timestamps_match_file_mtime_naive(home):
    path = _agent_jsonl(
        home,
        "slug-g",
        "mtime-1",
        [{"role": "user", "message": {"content": "ts check"}}],
    )
    fixed = 1_700_000_000.0
    os.utime(path, (fixed, fixed))
    _, sessions = _extract(home)
    s = sessions[0]
    expected = s.created_at
    assert expected.tzinfo is None
    assert s.last_updated == expected
    for msg in s.messages:
        assert msg.timestamp == expected


def test_flat_jsonl_path_tolerated(home):
    _agent_jsonl(
        home,
        "slug-h",
        "flat-1",
        [{"role": "user", "message": {"content": "flat file"}}],
        flat=True,
    )
    _, sessions = _extract(home)
    assert len(sessions) == 1
    assert sessions[0].session_id == "flat-1"


def test_hostile_input_does_not_abort(home, monkeypatch):
    import lore.extractors.cursor_agent as ca

    monkeypatch.setattr(ca, "_MAX_LINE_CHARS", 1024)
    long_line = "x" * 9000
    bad_lines = [
        json.dumps({"role": 99, "message": {"content": "skip"}}),
        "1e999",
        long_line,
        json.dumps({"role": "user", "message": {"content": "survived"}}),
    ]
    _agent_jsonl(home, "slug-i", "hostile-1", bad_lines)

    sym_path = _projects(home) / "slug-i" / "agent-transcripts" / "sym-1" / "sym-1.jsonl"
    sym_path.parent.mkdir(parents=True, exist_ok=True)
    sym_target = home / "outside.jsonl"
    sym_target.write_text('{"role":"user","message":{"content":"shadow"}}\n', encoding="utf-8")
    try:
        sym_path.symlink_to(sym_target)
    except OSError:
        pytest.skip("file symlink not supported")

    # Symlinked agent-transcripts directory must not be followed.
    real_at = _projects(home) / "slug-sym" / "agent-transcripts"
    real_at.mkdir(parents=True)
    (real_at / "ignored").mkdir()
    payload = real_at / "ignored" / "ignored.jsonl"
    payload.write_text('{"role":"user","message":{"content":"hidden"}}\n', encoding="utf-8")
    link_dir = home / "linked-at"
    link_dir.mkdir(exist_ok=True)
    moved = link_dir / "real-at"
    try:
        real_at.rename(moved)
        real_at.symlink_to(moved)
    except OSError:
        pytest.skip("directory symlink not supported")

    extractor, sessions = _extract(home)
    ids = {s.session_id for s in sessions}
    assert "hostile-1" in ids
    assert "sym-1" not in ids
    assert extractor.discovery["records_unknown"] >= 2


def test_never_opens_cursor_secrets(home, monkeypatch):
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text("{}", encoding="utf-8")
    (home / ".cursor" / "cli-config.json").write_text("{}", encoding="utf-8")
    _agent_jsonl(home, "slug-j", "safe-1", [{"role": "user", "message": {"content": "ok"}}])

    real_open = builtins.open
    opened: list[str] = []

    def tracking_open(file, *args, **kwargs):
        path = str(file)
        opened.append(path)
        if "mcp.json" in path or "cli-config.json" in path:
            raise AssertionError(f"opened forbidden path: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    _extract(home)
    assert not any("mcp.json" in p or "cli-config.json" in p for p in opened)


def test_oversized_first_record_is_skipped_not_misread_as_legacy(home, monkeypatch):
    import lore.extractors.cursor_agent as ca

    monkeypatch.setattr(ca, "_MAX_LINE_CHARS", 1024)
    big = json.dumps({"role": "user", "message": {"content": "x" * 5000}})
    ok = json.dumps({"role": "user", "message": {"content": "after the big record"}})
    _agent_jsonl(home, "slug-big", "big-1", [big, ok])

    extractor, sessions = _extract(home)

    assert [m.content for s in sessions for m in s.messages] == ["after the big record"]
    assert extractor.discovery["records_unknown"] >= 1


def test_message_cap_bounds_parsed_messages(home, monkeypatch):
    import lore.extractors.cursor_agent as ca

    monkeypatch.setattr(ca, "_MAX_MESSAGES", 3)
    lines = [{"role": "user", "message": {"content": f"prompt {i}"}} for i in range(10)]
    _agent_jsonl(home, "slug-cap", "cap-1", lines)

    extractor, sessions = _extract(home)

    assert len(sessions[0].messages) == 3
    assert extractor.discovery["records_unknown"] >= 1


def test_same_slug_nested_and_flat_copy_keeps_one_session(home):
    nested = [{"role": "user", "message": {"content": "nested copy"}}]
    flat = [{"role": "user", "message": {"content": "flat copy"}}]
    _agent_jsonl(home, "slug-d", "dup-1", nested)
    _agent_jsonl(home, "slug-d", "dup-1", flat, flat=True)

    _, sessions = _extract(home)

    assert [s.session_id for s in sessions] == ["dup-1"]
    assert sessions[0].messages[0].content == "nested copy"


def test_symlink_swapped_in_after_check_is_not_read(home, monkeypatch):
    import lore.extractors.cursor_agent as ca

    secret = home / ".cursor" / "mcp.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"role":"user","message":{"content":"SECRET"}}\n', encoding="utf-8")
    path = _agent_jsonl(home, "slug-r", "race-1", [{"role": "user", "message": {"content": "x"}}])
    path.unlink()
    path.symlink_to(secret)
    monkeypatch.setattr(ca, "_is_plain_file", lambda _p: True)  # simulate the lost race

    _, sessions = _extract(home)

    assert all("SECRET" not in m.content for s in sessions for m in s.messages)
