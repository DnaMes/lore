"""AI tasks receive the archived transcript, or fail with a reason (#177).

The summarizer, formatter and knowledge extractor used to be handed a session's
*metadata* — an index entry with no messages, or a first-few-messages excerpt —
and would analyse that reconstruction. These tests use a recording fake provider
so the assertion is on the exact prompt the provider would receive.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import lore.llm
import lore_cli
from lore.core.models import Role, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.llm.tasks.format import SUMMARY_OK, SessionFormatter
from lore.services.hydration import (
    REASON_EMPTY,
    REASON_NO_STORE,
    REASON_NOT_SYNCED,
    REASON_SESSION_MISSING,
    HydrationError,
    hydrate_body,
    hydrate_session,
)
from lore.storage.reader import load_session_messages_v2
from lore.storage.schema import open_connection
from lore.storage.writer import v2_db_path
from tests.test_summarize_pipeline import T0, _build_store, _msg, _session

LATE_DECISION = "FINAL DECISION: ship the SQLite reader, not the JSON cache"


class RecordingProvider(LLMProvider):
    """Records every prompt; answers the formatter's summary JSON."""

    def __init__(self, *, available: bool = True) -> None:
        super().__init__(LLMConfig(provider="fake", model="test"))
        self.prompts: List[str] = []
        self.available = available

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(
            content=json.dumps({"summary": "Done: reader shipped.", "tags": ["sqlite"]}),
            model="test",
            tokens_used=10,
        )

    def generate_batch(
        self, prompts: List[str], system_prompt: Optional[str] = None
    ) -> List[LLMResponse]:
        return [self.generate(p, system_prompt=system_prompt) for p in prompts]

    def is_available(self) -> bool:
        return self.available

    def get_model_info(self) -> Dict[str, Any]:
        return {"model": "test"}


def _metadata_only(session: UnifiedSession) -> UnifiedSession:
    """How ``lore format`` / ``lore knowledge`` receive a session from the index."""
    from dataclasses import replace

    return replace(session, messages=[])


def _store(tmp_path: Path, session: UnifiedSession) -> Path:
    out = tmp_path / "out"
    _build_store(out, session)
    return out


def test_hydration_returns_the_archived_body_in_store_order(tmp_path):
    session = _session("archived", "first prompt", "first answer", "second prompt", LATE_DECISION)
    out = _store(tmp_path, session)

    body = hydrate_body(out, "archived")

    assert [m.content for m in body.messages] == [
        "first prompt",
        "first answer",
        "second prompt",
        LATE_DECISION,
    ]
    assert [m.role for m in body.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.ASSISTANT,
    ]
    assert body.message_count == 4
    assert body.seq_range == (0, 3)
    assert body.source == "v2-store"
    assert len(body.revision) == 64 and int(body.revision, 16) >= 0


def test_hydration_replaces_a_lossy_excerpt_with_the_whole_body(tmp_path):
    session = _session("stub", "first prompt", "first answer", LATE_DECISION)
    out = _store(tmp_path, session)
    stale = UnifiedSession(
        tool=session.tool,
        session_id="stub",
        created_at=session.created_at,
        last_updated=session.last_updated,
        messages=[_msg(Role.USER, "first prompt")],  # a lossy live excerpt
    )

    hydrated, body = hydrate_session(out, stale)

    assert [m.content for m in hydrated.messages] == [
        "first prompt",
        "first answer",
        LATE_DECISION,
    ]
    assert body.revision == hydrate_body(out, "stub").revision
    assert body.coverage() == {
        "source": "v2-store",
        "messages": 3,
        "seq_range": [0, 2],
        "complete": True,
        "revision": body.revision,
    }


def test_revision_changes_when_the_archived_body_changes(tmp_path):
    first = _store(tmp_path, _session("rev", "one", "two"))
    before = hydrate_body(first, "rev").revision

    other = tmp_path / "other"
    _build_store(other, _session("rev", "one", "two changed"))
    after = hydrate_body(other, "rev").revision

    assert before != after


def test_no_store_is_a_typed_reason(tmp_path):
    missing = tmp_path / "never-indexed"
    missing.mkdir()
    with pytest.raises(HydrationError) as caught:
        hydrate_body(missing, "whatever")
    assert caught.value.reason == REASON_NO_STORE
    assert caught.value.session_id == "whatever"


def test_unknown_session_is_a_typed_reason(tmp_path):
    out = _store(tmp_path, _session("known", "hello"))
    with pytest.raises(HydrationError) as caught:
        hydrate_body(out, "unknown")
    assert caught.value.reason == REASON_SESSION_MISSING


def test_unsynced_messages_are_a_typed_reason(tmp_path):
    out = _store(tmp_path, _session("unsynced", "hello"))
    conn = open_connection(v2_db_path(out))
    try:
        conn.execute("UPDATE sessions SET messages_synced = 0 WHERE id = 'unsynced'")
    finally:
        conn.close()

    with pytest.raises(HydrationError) as caught:
        hydrate_body(out, "unsynced")
    assert caught.value.reason == REASON_NOT_SYNCED


def test_a_synced_session_without_rows_is_a_typed_reason(tmp_path):
    out = _store(tmp_path, _session("blank", "hello"))
    conn = open_connection(v2_db_path(out))
    try:
        conn.execute("DELETE FROM messages WHERE session_id = 'blank'")
    finally:
        conn.close()

    with pytest.raises(HydrationError) as caught:
        hydrate_body(out, "blank")
    assert caught.value.reason == REASON_EMPTY


def test_formatter_summarises_the_whole_archived_body(tmp_path):
    session = _session("format", "first prompt", "first answer", LATE_DECISION)
    out = _store(tmp_path, session)
    provider = RecordingProvider()
    formatter = SessionFormatter(provider, tmp_path / "formatted")
    hydrated, body = hydrate_session(out, _metadata_only(session))

    formatted = formatter.format_session(hydrated, source_revision=body.revision)

    assert len(provider.prompts) == 1
    assert LATE_DECISION in provider.prompts[0]
    assert formatted.summary_status == SUMMARY_OK
    assert formatted.source_revision == body.revision
    assert formatted.summary.startswith("Done")

    path = formatter.save_formatted_session(formatted)
    saved = path.read_text(encoding="utf-8")
    assert LATE_DECISION in saved
    assert f"_Source revision: {body.revision[:12]}_" in saved


def test_formatter_never_prompts_with_an_empty_body(tmp_path):
    provider = RecordingProvider()
    formatter = SessionFormatter(provider, tmp_path / "formatted")
    session = _session("empty", "something")
    session.messages = []

    formatted = formatter.format_session(session)

    assert provider.prompts == []
    assert formatted.summary_status == REASON_EMPTY
    saved = formatter.save_formatted_session(formatted).read_text(encoding="utf-8")
    assert "AI summary unavailable" in saved


def test_formatting_still_renders_when_ai_is_unavailable(tmp_path):
    session = _session("offline", "first prompt", "first answer", LATE_DECISION)
    out = _store(tmp_path, session)
    provider = RecordingProvider(available=False)
    formatter = SessionFormatter(provider, tmp_path / "formatted")
    hydrated, body = hydrate_session(out, _metadata_only(session))

    formatted = formatter.format_session(hydrated, source_revision=body.revision)

    assert provider.prompts == []
    assert formatted.summary == "" and formatted.tags == []
    assert LATE_DECISION in formatted.formatted_content
    assert "AI summary unavailable" not in formatter.save_formatted_session(formatted).read_text()


def test_cli_format_hydrates_before_prompting(tmp_path, monkeypatch, capsys):
    session = _session("cli", "first prompt", "first answer", LATE_DECISION)
    out = _store(tmp_path, session)
    IndexBuilder(out).build_index([session], {})
    provider = RecordingProvider()
    monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)
    args = Namespace(
        output_dir=str(out),
        session_id="cli",
        tool=None,
        limit=5,
        provider="gemini",
        model=None,
        allow_cloud=True,
        include_automated=False,
    )

    assert lore_cli.cmd_format(args) in (None, 0)

    assert len(provider.prompts) == 1
    assert LATE_DECISION in provider.prompts[0]
    assert "Skipped" not in capsys.readouterr().err


def test_cli_format_skips_an_unhydratable_session_with_its_reason(tmp_path, monkeypatch, capsys):
    session = _session("cli", "first prompt", LATE_DECISION)
    out = _store(tmp_path, session)
    IndexBuilder(out).build_index([session], {})
    conn = open_connection(v2_db_path(out))
    try:
        conn.execute("UPDATE sessions SET messages_synced = 0 WHERE id = 'cli'")
    finally:
        conn.close()
    provider = RecordingProvider()
    monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)
    args = Namespace(
        output_dir=str(out),
        session_id="cli",
        tool=None,
        limit=5,
        provider="gemini",
        model=None,
        allow_cloud=True,
        include_automated=False,
    )

    assert lore_cli.cmd_format(args) == 1

    assert provider.prompts == []
    err = capsys.readouterr().err
    assert "Skipped cli: messages-not-synced" in err
    assert f"1 sessions could not be hydrated (1 {REASON_NOT_SYNCED})" in err
    assert not (out / "cli.md").exists()


def test_archived_late_message_is_visible_to_the_formatter(tmp_path):
    """The decisive late body is exactly what the old excerpt dropped (#177)."""
    session = _session("late", "opening", *["filler " * 40] * 12, LATE_DECISION)
    out = _store(tmp_path, session)
    assert load_session_messages_v2(out, "late") is not None
    provider = RecordingProvider()
    formatter = SessionFormatter(provider, tmp_path / "formatted")

    hydrated, body = hydrate_session(out, _metadata_only(session))
    formatter.format_session(hydrated, source_revision=body.revision)

    assert LATE_DECISION in provider.prompts[0]


def test_hydration_timestamp_is_not_rewritten(tmp_path):
    session = _session("stamp", "prompt", "answer")
    out = _store(tmp_path, session)
    body = hydrate_body(out, "stamp")
    assert all(message.timestamp == T0 for message in body.messages)
