"""Tests for utils/git.py, utils/paths.py, utils/rules.py, utils/datetime.py."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.utils.datetime import make_naive, parse_duration, parse_timestamp
from annuli.utils.git import get_git_info
from annuli.utils.paths import (
    get_current_project,
    make_thread_id,
    project_to_dirname,
    safe_copy_db,
    sanitize_filename,
)
from annuli.utils.rules import extract_rules

# ---------------------------------------------------------------------------
# utils/datetime.py
# ---------------------------------------------------------------------------


def test_parse_duration_hours():
    assert parse_duration("2h") == timedelta(hours=2)


def test_parse_duration_days():
    assert parse_duration("7d") == timedelta(days=7)


def test_parse_duration_weeks():
    assert parse_duration("2w") == timedelta(weeks=2)


def test_parse_duration_months():
    assert parse_duration("1m") == timedelta(days=30)


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        parse_duration("bad")


def test_parse_duration_uppercase():
    # The function calls .lower() so "5D" normalises to "5d" = 5 days
    result = parse_duration("5D")
    assert result == timedelta(days=5)


def test_make_naive_with_timezone():
    from datetime import timezone

    dt_aware = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = make_naive(dt_aware)
    assert result.tzinfo is None
    assert result.year == 2025


def test_make_naive_already_naive():
    dt = datetime(2025, 6, 15, 10, 30)
    result = make_naive(dt)
    assert result is dt


def test_parse_timestamp_int_seconds():
    dt = parse_timestamp(1700000000)
    assert dt.year >= 2023


def test_parse_timestamp_int_milliseconds():
    dt = parse_timestamp(1700000000000)
    assert dt.year >= 2023


def test_parse_timestamp_float():
    dt = parse_timestamp(1700000000.5)
    assert dt.year >= 2023


def test_parse_timestamp_iso_string():
    dt = parse_timestamp("2025-06-15T10:30:00")
    assert dt.year == 2025
    assert dt.month == 6


def test_parse_timestamp_with_space_separator():
    dt = parse_timestamp("2025-06-15 10:30:00")
    assert dt.year == 2025


def test_parse_timestamp_with_z_suffix():
    dt = parse_timestamp("2025-06-15T10:30:00Z")
    assert dt.year == 2025


def test_parse_timestamp_nanoseconds_truncated():
    dt = parse_timestamp("2025-11-24T21:38:42.296187599")
    assert dt.year == 2025


def test_parse_timestamp_nanoseconds_with_tz():
    dt = parse_timestamp("2025-11-24T21:38:42.296187599+00:00")
    assert dt.year == 2025


def test_parse_timestamp_nanoseconds_with_negative_tz():
    """Negative timezone offset in nanosecond-precision timestamp."""
    dt = parse_timestamp("2025-11-24T21:38:42.296187599-05:00")
    assert dt.year == 2025


# ---------------------------------------------------------------------------
# utils/git.py
# ---------------------------------------------------------------------------


def test_get_git_info_none_path():
    result = get_git_info(None)
    assert result == {"branch": None, "sha": None}


def test_get_git_info_nonexistent_path():
    result = get_git_info("/nonexistent/path/that/does/not/exist")
    assert result == {"branch": None, "sha": None}


def test_get_git_info_non_git_dir(tmp_path):
    result = get_git_info(str(tmp_path))
    assert result == {"branch": None, "sha": None}


def test_get_git_info_success(tmp_path):
    with patch("subprocess.check_output") as mock_co:
        mock_co.side_effect = [
            b"main\n",  # first call: branch
            b"abc1234\n",  # second call: sha
        ]
        result = get_git_info(str(tmp_path))

    assert result["branch"] == "main"
    assert result["sha"] == "abc1234"


def test_get_git_info_subprocess_error(tmp_path):
    with patch("subprocess.check_output", side_effect=subprocess.CalledProcessError(128, "git")):
        result = get_git_info(str(tmp_path))
    assert result == {"branch": None, "sha": None}


def test_get_git_info_timeout(tmp_path):
    with patch("subprocess.check_output", side_effect=subprocess.TimeoutExpired("git", 2)):
        result = get_git_info(str(tmp_path))
    assert result == {"branch": None, "sha": None}


def test_get_git_info_file_not_found(tmp_path):
    with patch("subprocess.check_output", side_effect=FileNotFoundError):
        result = get_git_info(str(tmp_path))
    assert result == {"branch": None, "sha": None}


# ---------------------------------------------------------------------------
# utils/paths.py
# ---------------------------------------------------------------------------


def test_sanitize_filename_basic():
    assert sanitize_filename("hello world") == "hello-world"


def test_sanitize_filename_removes_invalid_chars():
    result = sanitize_filename('file<name>:with"invalid|chars?')
    assert "/" not in result
    assert "<" not in result


def test_sanitize_filename_empty_becomes_untitled():
    result = sanitize_filename("")
    assert result == "untitled"


def test_sanitize_filename_whitespace_becomes_dashes():
    result = sanitize_filename("hello   world")
    assert "-" in result


def test_project_to_dirname_none():
    assert project_to_dirname(None) == "_unknown"


def test_project_to_dirname_empty():
    assert project_to_dirname("") == "_unknown"


def test_project_to_dirname_path():
    result = project_to_dirname("/home/user/projects/foo")
    assert "home" in result
    assert "foo" in result
    assert "/" not in result


def test_make_thread_id_from_project_path():
    tid = make_thread_id(project_path="/home/user/projects/foo")
    assert tid is not None
    assert tid.startswith("project:")
    assert len(tid) > 10


def test_make_thread_id_from_hash():
    tid = make_thread_id(project_hash="abc123")
    assert tid == "project:abc123"


def test_make_thread_id_none():
    assert make_thread_id() is None


def test_safe_copy_db_nonexistent(tmp_path):
    src = tmp_path / "missing.db"
    result = safe_copy_db(src)
    assert result == src  # returns original if missing


def test_safe_copy_db_copies_file(tmp_path):
    src = tmp_path / "test.db"
    src.write_bytes(b"SQLITE data")
    result = safe_copy_db(src)
    assert result.exists()
    assert result != src
    assert result.read_bytes() == b"SQLITE data"


def test_safe_copy_db_copies_wal_files(tmp_path):
    src = tmp_path / "test.db"
    src.write_bytes(b"SQLITE data")
    wal = tmp_path / "test.db-wal"
    wal.write_bytes(b"WAL data")
    shm = tmp_path / "test.db-shm"
    shm.write_bytes(b"SHM data")

    result = safe_copy_db(src)
    assert result.exists()
    wal_copy = Path(str(result) + "-wal")
    assert wal_copy.exists()


def test_safe_copy_db_exception_returns_original(tmp_path):
    """When copy fails due to permission error, original path is returned."""
    from unittest.mock import patch

    src = tmp_path / "test.db"
    src.write_bytes(b"SQLITE data")

    with patch("shutil.copy2", side_effect=PermissionError("no access")):
        result = safe_copy_db(src)
    # Falls back to original
    assert result == src


def test_get_current_project_uses_git(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = str(tmp_path) + "\n"
        result = get_current_project(cwd=tmp_path)
    assert result == str(tmp_path)


def test_get_current_project_falls_back_to_cwd(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        result = get_current_project(cwd=tmp_path)
    assert result == str(tmp_path)


def test_get_current_project_exception_fallback(tmp_path):
    with patch("subprocess.run", side_effect=Exception("no git")):
        result = get_current_project(cwd=tmp_path)
    assert result == str(tmp_path)


def test_get_current_project_no_cwd_arg():
    """When cwd=None, defaults to Path.cwd()."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        result = get_current_project(cwd=None)
    # Should return something (cwd)
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# utils/rules.py
# ---------------------------------------------------------------------------


def _make_session(messages: list[tuple[str, str]]) -> UnifiedSession:
    """Helper to build a minimal UnifiedSession for rules tests."""
    msgs = [
        UnifiedMessage(
            role=Role.USER if r == "user" else Role.ASSISTANT, content=c, timestamp=datetime.now()
        )
        for r, c in messages
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="test-rules-session",
        created_at=datetime.now(),
        last_updated=datetime.now(),
        messages=msgs,
    )


def test_extract_rules_empty():
    result = extract_rules([])
    assert result == []


def test_extract_rules_no_assistant_messages():
    session = _make_session([("user", "Hello, how are you?")])
    result = extract_rules([session])
    assert result == []


def test_extract_rules_extracts_sentences_with_keywords():
    session = _make_session(
        [
            (
                "assistant",
                "You should always use type hints. You must never use bare except. "
                "Best practice is to write tests. Avoid hardcoding secrets.",
            )
        ]
    )
    result = extract_rules([session])
    assert len(result) > 0
    # All results should be from assistant messages
    assert any("always" in r.lower() or "must" in r.lower() or "avoid" in r.lower() for r in result)


def test_extract_rules_skips_short_sentences():
    session = _make_session([("assistant", "Must. Always. Never do that.")])
    result = extract_rules([session])
    # Very short sentences (<20 chars) should be filtered
    assert all(len(r) >= 20 for r in result)


def test_extract_rules_respects_max_rules():
    long_content = " ".join(
        [
            f"You must always do step {i} carefully and thoroughly every time you work."
            for i in range(50)
        ]
    )
    session = _make_session([("assistant", long_content)])
    result = extract_rules([session], max_rules=5)
    assert len(result) <= 5


def test_extract_rules_deduplicates():
    repeated = "You should always write tests for your code."
    session = _make_session([("assistant", f"{repeated} {repeated} {repeated}")])
    result = extract_rules([session])
    assert result.count(repeated) <= 1
