from datetime import datetime
from pathlib import Path

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.extractors.base import BaseExtractor
from lore_cli import _is_low_value_index_entry


class _DummyExtractor(BaseExtractor):
    @property
    def tool(self) -> Tool:
        return Tool.CODEX

    def extract_sessions(self):
        return iter(())


def _session(messages, title=None):
    now = datetime(2026, 3, 4, 12, 0, 0)
    return UnifiedSession(
        tool=Tool.GEMINI_CLI,
        session_id="ses_test_abcdef12",
        created_at=now,
        last_updated=now,
        messages=messages,
        title=title,
    )


def test_extract_prompt_outline_skips_kryptic_command_markup():
    builder = IndexBuilder(Path("/tmp"))
    messages = [
        UnifiedMessage(
            role=Role.USER,
            content="<command-name>login</command-name> <command-message>login</command-message>",
            timestamp=datetime(2026, 3, 4, 12, 0, 0),
        ),
        UnifiedMessage(
            role=Role.USER,
            content="please fix docker compose startup crash",
            timestamp=datetime(2026, 3, 4, 12, 1, 0),
        ),
    ]

    outline = builder._extract_prompt_outline(_session(messages))

    assert outline == "please fix docker compose startup crash"


def test_infer_title_rejects_low_quality_native_title():
    builder = IndexBuilder(Path("/tmp"))
    session = _session([], title="<command-name>login</command-name>")

    title = builder._infer_title(session, "clean install on fedora 43")

    assert "clean install on fedora 43" in title
    assert title.endswith("abcdef12")


def test_should_import_session_allows_single_prompt_session_after_relaxation():
    extractor = _DummyExtractor()
    session = _session(
        [
            UnifiedMessage(
                role=Role.USER,
                content="one prompt only",
                timestamp=datetime(2026, 3, 4, 12, 0, 0),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="long answer " * 400,
                timestamp=datetime(2026, 3, 4, 12, 1, 0),
            ),
        ]
    )

    assert extractor.should_import_session(session) is True


def _thin_session():
    """A session with a single user prompt and little content."""
    return _session(
        [
            UnifiedMessage(
                role=Role.USER,
                content="hi",
                timestamp=datetime(2026, 3, 4, 12, 0, 0),
            ),
        ]
    )


def test_skip_counts_tracks_dropped_sessions(monkeypatch):
    """Dropped sessions are tallied per reason, not silently discarded (#1e)."""
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    extractor = _DummyExtractor()

    assert extractor.should_import_session(_thin_session()) is False
    assert extractor.should_import_session(_thin_session()) is False
    assert extractor.skip_counts.get("too_few_user_prompts") == 2


def test_min_user_prompts_env_override(monkeypatch):
    """LORE_MIN_USER_PROMPTS lowers the threshold without a profile flip."""
    extractor = _DummyExtractor()
    session = _session(
        [
            UnifiedMessage(
                role=Role.USER,
                content="single but meaningful prompt",
                timestamp=datetime(2026, 3, 4, 12, 0, 0),
            ),
        ]
    )

    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    assert extractor.should_import_session(session) is False

    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "1")
    assert extractor.should_import_session(session) is True


def test_min_user_prompts_invalid_override_falls_back(monkeypatch):
    """A garbage override is ignored, falling back to the class/profile default."""
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "not-a-number")
    monkeypatch.setenv("LORE_IMPORT_PROFILE", "relaxed")
    # Pin the relaxed default to 3 so the single-prompt session must be dropped
    # (conftest pins it to 1 for the rest of the suite).
    monkeypatch.setattr(BaseExtractor, "MIN_USER_PROMPTS", 3, raising=True)
    extractor = _DummyExtractor()
    assert extractor.should_import_session(_thin_session()) is False


def test_is_low_value_index_entry_flags_thin_sessions():
    assert _is_low_value_index_entry({"id": "x", "prompts": 1, "messages": 2}) is True
    assert (
        _is_low_value_index_entry(
            {
                "id": "x",
                "prompts": 2,
                "messages": 3,
                "prompt_outline": "ok",
                "search_text": "small",
            }
        )
        is True
    )
    assert (
        _is_low_value_index_entry(
            {
                "id": "x",
                "prompts": 3,
                "messages": 6,
                "prompt_outline": "real debugging session",
                "search_text": "x" * 800,
            }
        )
        is False
    )


def test_infer_title_drops_copilot_mode_preamble_titles():
    builder = IndexBuilder(Path("/tmp"))
    session = _session([], title="/home/user on-request workspace-write restricted")

    title = builder._infer_title(session, "")

    assert title.startswith("Gemini Cli 2026-03-04")


def test_infer_title_drops_login_successful_titles():
    builder = IndexBuilder(Path("/tmp"))
    session = _session([], title="Login successful")

    title = builder._infer_title(session, "")

    assert title.startswith("Gemini Cli 2026-03-04")


def test_infer_title_drops_agents_template_prompt_titles():
    builder = IndexBuilder(Path("/tmp"))
    session = _session(
        [], title="Generate a file named AGENTS.md that serves as a contributor guide"
    )

    title = builder._infer_title(session, "")

    assert title.startswith("Gemini Cli 2026-03-04")


def test_is_low_quality_title_catches_wrapped_caveat():
    """#68 — the <local-command-caveat> wrapper must not slip past the filter."""
    from lore.exporters.index import is_low_quality_title

    assert is_low_quality_title(
        "<local-command-caveat>Caveat: The messages below were generated by the user"
    )
    assert is_low_quality_title("Caveat: The messages below were generated")
    assert is_low_quality_title("<local-command-stdout></local-command-stdout>")
    # Real titles survive.
    assert not is_low_quality_title("Refactor auth middleware")
    assert not is_low_quality_title("live title for a cursor session")
