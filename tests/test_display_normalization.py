"""Display normalisation is a view, not a second lossy parser (#167).

`normalize_session_messages_for_display` rewrote ``message.content`` in place, so
the canonical text was gone for that session object and running it twice folded
an already folded message. The summaries it produces (file operations, unset
environment variables) also dropped the lines they summarised with no way back.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces.web_services import (
    normalize_session_messages_for_display,
    preview_normalize_message,
)

TS = datetime(2026, 7, 1, 9, 0, 0)


def _session(tool: Tool, content: str, role: Role = Role.ASSISTANT, rules=None) -> UnifiedSession:
    return UnifiedSession(
        tool=tool,
        session_id="ses_norm",
        created_at=TS,
        last_updated=TS,
        project_path="/repo/lore",
        messages=[UnifiedMessage(role=role, content=content, timestamp=TS)],
    )


def _normalize(session, rules=None) -> UnifiedMessage:
    normalize_session_messages_for_display(session, noise_rules=rules)
    return session.messages[0]


# The summariser fires on its documented line shapes, three or more of them.
FILE_OPS = "\n".join(f"The file /repo/lore/f{index}.py has been updated." for index in range(6))


def _file_op_rules() -> dict:
    return {
        "default": {"strip_ansi_sequences": True},
        "claude-code": {"claude_summarize_file_operations": True},
    }


# ---------------------------------------------------------------------------
# the canonical content survives
# ---------------------------------------------------------------------------


def test_original_content_is_kept_on_the_message():
    original = "The file /repo/lore/a.py has been updated.\n" * 5
    message = _normalize(_session(Tool.CLAUDE_CODE, original, rules=_file_op_rules()))
    assert message.original_content == original
    assert message.content != original  # the view is folded


def test_repeated_normalisation_is_idempotent():
    session = _session(Tool.CLAUDE_CODE, FILE_OPS, rules=_file_op_rules())
    first = _normalize(session).content
    second = _normalize(session).content
    third = _normalize(session).content
    assert first == second == third


def test_repeated_normalisation_does_not_refold_the_folded_text():
    """The folded text contains its own summary line; a second run must not wrap it again."""
    session = _session(Tool.CLAUDE_CODE, FILE_OPS, rules=_file_op_rules())
    _normalize(session)
    once = session.messages[0].content
    _normalize(session)
    twice = session.messages[0].content
    assert once.count("<details>") == twice.count("<details>") == 1


def test_folded_file_operations_keep_every_line():
    session = _session(Tool.CLAUDE_CODE, FILE_OPS, rules=_file_op_rules())
    message = _normalize(session)
    assert "File operations summary (collapsed)" in message.content
    for index in range(6):
        assert f"/repo/lore/f{index}.py" in message.content


def test_unset_env_vars_keep_every_key():
    keys = [f"LORE_VARIABLE_{index}" for index in range(6)]
    text = "\n".join(f"{key}: not set" for key in keys)
    message = _normalize(
        _session(
            Tool.OPENCODE, text, rules={"default": {"opencode_summarize_unset_env_vars": True}}
        )
    )
    assert "Unset environment variables (6)" in message.content
    for key in keys:
        assert key in message.content


# ---------------------------------------------------------------------------
# content that must never be lost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "## Heading\n\nA paragraph with **bold** and `code`.",
        "```python\ndef f():\n    return indented\n```",
        "line one\n\nline three after a blank line",
        "tool output\n\tindented with a tab\n        indented with spaces",
        "unicode: 👩‍💻 José 日本語 e\u0301 combining",
    ],
)
def test_content_survives_normalisation(content):
    message = _normalize(_session(Tool.CLAUDE_CODE, content))
    assert message.original_content == content
    for line in content.splitlines():
        if line.strip():
            assert line.strip() in message.content


def test_code_indentation_survives_in_the_view():
    content = "```python\ndef f():\n    return 42\n```"
    message = _normalize(_session(Tool.CLAUDE_CODE, content))
    assert "    return 42" in message.content


def test_preview_path_does_not_mutate_anything():
    content = "The file /repo/lore/a.py has been updated.\n" * 5
    preview = preview_normalize_message(
        "claude-code", "assistant", content, noise_rules=_file_op_rules()
    )
    assert "File operations summary (collapsed)" in preview
    assert "/repo/lore/a.py" in preview


def test_message_without_noise_is_left_alone():
    content = "just a clean answer"
    message = _normalize(_session(Tool.CLAUDE_CODE, content))
    assert message.content == content
    assert message.original_content == content


def test_the_export_uses_the_canonical_content():
    """The Markdown export must not see the folded view."""
    from pathlib import Path

    from lore.exporters.markdown import MarkdownExporter

    session = _session(Tool.CLAUDE_CODE, FILE_OPS, rules=_file_op_rules())
    _normalize(session)
    markdown = MarkdownExporter(Path("."))._generate_markdown(session)
    # The exporter renders whatever the session carries, so the folded lines are
    # present in full inside the collapsed block rather than being dropped.
    for index in range(6):
        assert f"/repo/lore/f{index}.py" in markdown
