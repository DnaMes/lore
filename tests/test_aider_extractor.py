"""Tests for the Aider extractor (issue #51).

Aider writes one ``.aider.chat.history.md`` file per project in the
project's working directory. Each ``# aider chat started at ...`` block is
one session. These tests build synthetic history files under a temp HOME.
"""

from __future__ import annotations

from pathlib import Path

from annuli.core.models import Role, Tool
from annuli.extractors.aider import AiderExtractor

SAMPLE_HISTORY = """
# aider chat started at 2024-01-15 14:30:45

#### add a hello function
#### to main.py

Sure, I'll add a hello function.

```python
def hello():
    print("hi")
```

> Applied edit to main.py

# aider chat started at 2024-02-20 09:15:00

#### refactor the loop

Done, refactored the loop into a comprehension.
"""


def _write_history(home: Path, project: str, content: str) -> Path:
    """Create <home>/<project>/.aider.chat.history.md and return its path."""
    proj_dir = home / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    history = proj_dir / ".aider.chat.history.md"
    history.write_text(content, encoding="utf-8")
    return history


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_not_available_when_no_history_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AiderExtractor().is_available() is False


def test_available_when_history_file_exists(monkeypatch, tmp_path):
    _write_history(tmp_path, "myproject", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert AiderExtractor().is_available() is True


def test_extract_sessions_empty_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list(AiderExtractor().extract_sessions()) == []


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_tool_property_is_aider():
    assert AiderExtractor().tool is Tool.AIDER


def test_splits_file_into_one_session_per_header(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = list(AiderExtractor().extract_sessions())
    assert len(sessions) == 2


def test_session_timestamps_from_header(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = sorted(AiderExtractor().extract_sessions(), key=lambda s: s.created_at)
    assert sessions[0].created_at.year == 2024
    assert sessions[0].created_at.month == 1
    assert sessions[1].created_at.month == 2


def test_user_assistant_tool_roles_parsed(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = sorted(AiderExtractor().extract_sessions(), key=lambda s: s.created_at)
    first = sessions[0]
    roles = [m.role for m in first.messages]
    assert roles == [Role.USER, Role.ASSISTANT, Role.TOOL]


def test_multiline_user_prompt_joined(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = sorted(AiderExtractor().extract_sessions(), key=lambda s: s.created_at)
    user_msg = sessions[0].messages[0]
    assert user_msg.content == "add a hello function\nto main.py"


def test_assistant_message_keeps_code_block(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = sorted(AiderExtractor().extract_sessions(), key=lambda s: s.created_at)
    assistant_msg = sessions[0].messages[1]
    assert "```python" in assistant_msg.content
    assert "def hello():" in assistant_msg.content


def test_project_path_is_history_file_parent(monkeypatch, tmp_path):
    _write_history(tmp_path, "coolproject", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = list(AiderExtractor().extract_sessions())
    assert all(s.project_path == str(tmp_path / "coolproject") for s in sessions)


def test_session_id_is_stable_across_runs(monkeypatch, tmp_path):
    """Re-importing the same file must yield the same session IDs."""
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    ids_1 = sorted(s.session_id for s in AiderExtractor().extract_sessions())
    ids_2 = sorted(s.session_id for s in AiderExtractor().extract_sessions())
    assert ids_1 == ids_2
    assert all(sid.startswith("aider-") for sid in ids_1)


def test_session_ids_unique_per_block(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    ids = [s.session_id for s in AiderExtractor().extract_sessions()]
    assert len(ids) == len(set(ids))


def test_blank_user_line_handled(monkeypatch, tmp_path):
    """A bare '####' (Aider's blank-input marker) must not crash parsing."""
    content = (
        "# aider chat started at 2024-03-01 10:00:00\n\n"
        "####\n"
        "#### a real follow-up prompt\n\n"
        "An assistant reply.\n"
    )
    _write_history(tmp_path, "proj", content)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = list(AiderExtractor().extract_sessions())
    assert len(sessions) == 1
    user_msgs = [m for m in sessions[0].messages if m.role == Role.USER]
    # The blank line collapses; the real prompt survives.
    assert any("real follow-up prompt" in m.content for m in user_msgs)


def test_thread_id_assigned(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", SAMPLE_HISTORY)
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = list(AiderExtractor().extract_sessions())
    assert all(s.thread_id for s in sessions)


def test_malformed_file_does_not_crash(monkeypatch, tmp_path):
    _write_history(tmp_path, "proj", "not a real aider history\njust noise\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    # No session header -> the leading noise block has no user prompt and is
    # dropped by should_import_session; extraction must not raise.
    assert list(AiderExtractor().extract_sessions()) == []
