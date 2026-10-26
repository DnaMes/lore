"""Tests for lore/extractors/antigravity.py.

Antigravity stores each session as a directory under
``~/.gemini/antigravity/brain/<session-id>/`` containing:
- ``task.md`` (the user goal / checklist) - REQUIRED
- ``task.md.metadata.json`` (summary + timestamps)
- ``walkthrough.md`` (the assistant solution) - optional
- ``walkthrough.md.metadata.json`` (timestamps) - optional
"""

from __future__ import annotations

import json
from pathlib import Path

from lore.core.models import Role, Tool
from lore.extractors.antigravity import AntigravityExtractor


def _brain_dir(home: Path) -> Path:
    path = home / ".gemini" / "antigravity" / "brain"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_session(
    home: Path,
    session_id: str,
    *,
    task: str | None = "# Build the thing\n\nDo step one and step two.",
    task_meta: dict | None = None,
    walkthrough: str | None = None,
    walkthrough_meta: dict | None = None,
) -> Path:
    session_dir = _brain_dir(home) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    if task is not None:
        (session_dir / "task.md").write_text(task, encoding="utf-8")
    if task_meta is not None:
        (session_dir / "task.md.metadata.json").write_text(json.dumps(task_meta), encoding="utf-8")
    if walkthrough is not None:
        (session_dir / "walkthrough.md").write_text(walkthrough, encoding="utf-8")
    if walkthrough_meta is not None:
        (session_dir / "walkthrough.md.metadata.json").write_text(
            json.dumps(walkthrough_meta), encoding="utf-8"
        )
    return session_dir


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_not_available_when_no_brain_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AntigravityExtractor().is_available() is False


def test_not_available_when_antigravity_dir_but_no_brain(monkeypatch, tmp_path):
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AntigravityExtractor().is_available() is False


def test_a_bare_brain_dir_is_not_available(monkeypatch, tmp_path):
    """#158 — an install directory without readable data is not availability."""
    _brain_dir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AntigravityExtractor().is_available() is False


def test_available_when_a_task_artifact_exists(monkeypatch, tmp_path):
    _make_session(tmp_path, "real-session")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AntigravityExtractor().is_available() is True


def test_undecodable_tree_is_counted_as_unsupported(monkeypatch, tmp_path):
    """#158 — undecoded artifacts are counted, never reported as parsed."""
    brain = _brain_dir(tmp_path)
    session_dir = brain / "opaque-session"
    session_dir.mkdir(parents=True)
    (session_dir / "conversations").mkdir()
    (session_dir / "conversations" / "abc.pb").write_bytes(b"\x00\x01binary")
    monkeypatch.setenv("HOME", str(tmp_path))

    extractor = AntigravityExtractor()
    assert list(extractor.extract_sessions()) == []
    assert extractor.discovery["unsupported_artifacts"] == 1
    assert extractor.discovery["files_parsed"] == 0
    assert extractor.is_available() is False


def test_extract_sessions_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list(AntigravityExtractor().extract_sessions()) == []


def test_tool_property_is_antigravity(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AntigravityExtractor().tool is Tool.ANTIGRAVITY


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_task_only_session(monkeypatch, tmp_path):
    _make_session(tmp_path, "sess-task-only")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool is Tool.ANTIGRAVITY
    assert s.session_id == "sess-task-only"
    assert s.message_count == 1
    assert s.messages[0].role is Role.USER
    assert "step one and step two" in s.messages[0].content


def test_title_extracted_from_markdown_heading(monkeypatch, tmp_path):
    _make_session(
        tmp_path,
        "sess-titled",
        task="# Refactor the parser\n\nSplit it into modules.",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].title == "Refactor the parser"


def test_parse_task_and_walkthrough(monkeypatch, tmp_path):
    _make_session(
        tmp_path,
        "sess-full",
        task="# Add a feature\n\nImplement the new endpoint.",
        walkthrough="## Solution\n\nI added the endpoint and tests.",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.message_count == 2
    assert s.messages[0].role is Role.USER
    assert s.messages[1].role is Role.ASSISTANT
    assert "added the endpoint and tests" in s.messages[1].content


def test_metadata_summary_and_timestamps(monkeypatch, tmp_path):
    _make_session(
        tmp_path,
        "sess-meta",
        task="# Meta session\n\nGoal text.",
        task_meta={"summary": "A short summary line", "updatedAt": "2025-06-15T12:00:00"},
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.summary == "A short summary line"
    assert s.created_at.year == 2025
    assert s.created_at.month == 6
    assert s.created_at.day == 15


def test_walkthrough_metadata_advances_last_updated(monkeypatch, tmp_path):
    _make_session(
        tmp_path,
        "sess-wt-meta",
        task="# WT meta\n\nGoal.",
        task_meta={"updatedAt": "2025-06-15T10:00:00"},
        walkthrough="## Done\n\nFinished it.",
        walkthrough_meta={"updatedAt": "2025-06-15T14:30:00"},
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    # last_updated should track the later walkthrough metadata timestamp.
    assert s.last_updated.hour == 14
    assert s.last_updated >= s.created_at


def test_project_path_from_file_link(monkeypatch, tmp_path):
    """A file:// link in the walkthrough yields a project path when it exists."""
    project = tmp_path / "home" / "user" / "projects" / "myrepo"
    project.mkdir(parents=True)
    walkthrough = f"## Solution\n\nSee file://{project}/src/main.py for the change."
    _make_session(
        tmp_path,
        "sess-proj",
        task="# Project session\n\nGoal.",
        walkthrough=walkthrough,
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].project_path == str(project)


def test_session_without_task_md_skipped(monkeypatch, tmp_path):
    """A brain subdir lacking task.md yields no session."""
    session_dir = _brain_dir(tmp_path) / "no-task"
    session_dir.mkdir()
    (session_dir / "walkthrough.md").write_text("orphan walkthrough", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert list(AntigravityExtractor().extract_sessions()) == []


def test_corrupt_metadata_json_does_not_crash(monkeypatch, tmp_path):
    session_dir = _make_session(
        tmp_path,
        "sess-badmeta",
        task="# Bad meta\n\nGoal text here.",
    )
    (session_dir / "task.md.metadata.json").write_text("not valid json {{", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    # Corrupt metadata is swallowed; the session still parses from task.md.
    assert len(sessions) == 1
    assert sessions[0].session_id == "sess-badmeta"


def test_non_directory_entries_in_brain_ignored(monkeypatch, tmp_path):
    brain = _brain_dir(tmp_path)
    (brain / "stray-file.txt").write_text("not a session", encoding="utf-8")
    _make_session(tmp_path, "real-session", task="# Real\n\nGoal.")
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "real-session"


def test_extract_multiple_sessions(monkeypatch, tmp_path):
    for i in range(3):
        _make_session(
            tmp_path,
            f"sess-{i}",
            task=f"# Session {i}\n\nGoal number {i}.",
        )
    monkeypatch.setenv("HOME", str(tmp_path))

    sessions = list(AntigravityExtractor().extract_sessions())
    assert len(sessions) == 3
    assert {s.session_id for s in sessions} == {"sess-0", "sess-1", "sess-2"}


def test_no_task_md_dir_is_recorded_as_skip_not_silent(monkeypatch, tmp_path):
    """#69 — a brain dir without task.md must surface as a skip, not vanish."""
    brain = _brain_dir(tmp_path)
    (brain / "empty-session").mkdir(parents=True)  # no task.md
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")

    extractor = AntigravityExtractor()
    imported = list(extractor.extract_sessions())

    assert imported == []
    assert extractor.skip_counts.get("no_task_md") == 1


def test_short_session_recorded_as_too_few_prompts(monkeypatch, tmp_path):
    """#69 — a real but too-short session is filtered and counted, not a silent 0."""
    _make_session(tmp_path, "short-session")  # single user prompt
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")

    extractor = AntigravityExtractor()
    imported = list(extractor.extract_sessions())

    assert imported == []
    assert extractor.skip_counts.get("too_few_user_prompts", 0) >= 1


def test_source_data_is_reported_even_without_readable_input(monkeypatch, tmp_path):
    """#158 — a bare brain tree has source data, just nothing decodable."""
    brain = _brain_dir(tmp_path)
    (brain / "opaque").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    extractor = AntigravityExtractor()
    assert extractor.has_source_data() is True
    assert extractor.is_available() is False


def test_check_deep_reports_coverage_for_an_undecodable_tree(monkeypatch, tmp_path, capsys):
    """#158 — the diagnostic says which artifacts it could not use."""
    import argparse

    import lore_cli

    brain = _brain_dir(tmp_path)
    session_dir = brain / "opaque"
    (session_dir / "conversations").mkdir(parents=True)
    (session_dir / "conversations" / "x.pb").write_bytes(b"\x00bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [AntigravityExtractor()])
    lore_cli.cmd_check(argparse.Namespace(deep=True, output_dir=str(tmp_path / ".lore")))
    out = capsys.readouterr().out
    assert "data directory present, no readable input" in out
    assert "Coverage: unsupported_artifacts=1" in out
    assert "Sessions parsed: 0" in out
