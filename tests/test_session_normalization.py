from datetime import datetime
from pathlib import Path

from lore.core.models import Role, TitleSource, Tool, UnifiedMessage, UnifiedSession
from lore.extractors.base import BaseExtractor
from lore.extractors.opencode import OpenCodeExtractor


class DummyExtractor(BaseExtractor):
    @property
    def tool(self) -> Tool:
        return Tool.CODEX

    def extract_sessions(self):
        return iter(())


def _msg(role: Role, content: str) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=datetime(2026, 3, 4, 12, 0, 0))


def test_should_import_normalizes_title_source_and_thread_id():
    extractor = DummyExtractor()
    session = UnifiedSession(
        tool=Tool.CODEX,
        session_id="ses-normalize-1",
        created_at=datetime(2026, 3, 4, 12, 0, 0),
        last_updated=datetime(2026, 3, 4, 12, 5, 0),
        project_path="/repo/demo",
        messages=[
            _msg(Role.USER, "Please add API retries and improve logging."),
            _msg(Role.ASSISTANT, "Sure, I will do that."),
            _msg(Role.USER, "Also add tests."),
        ],
        title=None,
        thread_id=None,
    )

    assert extractor.should_import_session(session) is True
    assert session.title is not None
    assert "Please add API retries" in session.title
    assert session.title_source == TitleSource.FIRST_MESSAGE
    assert session.thread_id is not None


def test_should_import_normalizes_fallback_title_when_no_user_text():
    extractor = DummyExtractor()
    session = UnifiedSession(
        tool=Tool.CURSOR,
        session_id="ses-normalize-2",
        created_at=datetime(2026, 3, 4, 12, 0, 0),
        last_updated=datetime(2026, 3, 4, 12, 5, 0),
        project_path="/repo/demo",
        messages=[
            _msg(Role.ASSISTANT, "Done."),
            _msg(Role.ASSISTANT, "Anything else?"),
            _msg(Role.ASSISTANT, "No further actions."),
        ],
        title=None,
        thread_id=None,
    )

    assert extractor.should_import_session(session) is False
    assert session.title == "Cursor Session 2026-03-04"
    assert session.title_source == TitleSource.FALLBACK
    assert session.thread_id is not None


def test_opencode_parse_session_sets_thread_id(monkeypatch):
    extractor = OpenCodeExtractor()
    monkeypatch.setattr(
        extractor,
        "_load_messages",
        lambda _sid: [
            _msg(Role.USER, "hi"),
            _msg(Role.ASSISTANT, "hello"),
            _msg(Role.USER, "again"),
        ],
    )

    session = extractor._parse_session(
        Path("/tmp/session.json"),
        {
            "id": "ses-opencode-1",
            "time": {"created": 0, "updated": 0},
            "title": "OpenCode Session",
            "directory": "/repo/opencode",
            "projectID": "proj-1",
            "version": "1.0.0",
        },
    )

    assert session is not None
    assert session.thread_id is not None
