"""Persisted, idempotent `lore knowledge` extraction (#197, task 2, migration 14)."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import lore.llm
import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.llm.tasks.knowledge import KnowledgeExtractor
from lore.services.knowledge import (
    KNOWLEDGE_PROMPT_VERSION,
    ExtractionReport,
    run_extraction,
)
from lore.storage import current_version, get_memory, initialise, open_connection
from lore.storage.knowledge import (
    get_session_knowledge,
    list_session_knowledge,
    prompt_fingerprint,
    save_session_knowledge,
)
from lore.storage.schema import MIGRATIONS
from lore.storage.writer import v2_db_path
from tests.test_summarize_pipeline import _replace_messages


def _payload(topic: str) -> str:
    return json.dumps(
        {
            "topic": topic,
            "key_points": [f"{topic} point"],
            "code_snippets": [],
            "decisions": [f"{topic} decision"],
            "tools_used": ["sqlite"],
        }
    )


class FakeProvider(LLMProvider):
    """Records prompts; replays scripted responses, then a valid payload."""

    def __init__(self, responses: Optional[List[str]] = None) -> None:
        super().__init__(LLMConfig(provider="fake", model="fake-model"))
        self.prompts: List[str] = []
        self.responses = list(responses or [])

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        content = self.responses.pop(0) if self.responses else _payload("Topic")
        return LLMResponse(content=content, model="fake-model")

    def generate_batch(
        self, prompts: List[str], system_prompt: Optional[str] = None
    ) -> List[LLMResponse]:
        return [self.generate(p) for p in prompts]

    def is_available(self) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"model": "fake-model"}


def _session(sid: str, *contents: str) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        messages=[
            UnifiedMessage(
                role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
                content=text,
                timestamp=datetime(2026, 1, 1),
            )
            for i, text in enumerate(contents)
        ],
    )


def _build(output_dir: Path, *sessions: UnifiedSession) -> None:
    IndexBuilder(output_dir).build_index(list(sessions), {})


def _extractor(output_dir: Path, provider: FakeProvider) -> KnowledgeExtractor:
    return KnowledgeExtractor(provider, output_dir / "knowledge")


def _stubs(*sessions: UnifiedSession) -> List[UnifiedSession]:
    """Sessions as `cmd_knowledge` builds them from index.json: no messages."""
    return [
        UnifiedSession(
            tool=s.tool,
            session_id=s.session_id,
            created_at=s.created_at,
            last_updated=s.last_updated,
            messages=[],
            project_path=s.project_path,
        )
        for s in sessions
    ]


def _run(output_dir: Path, provider: FakeProvider, *sessions: UnifiedSession) -> ExtractionReport:
    return run_extraction(
        output_dir, _stubs(*sessions), _extractor(output_dir, provider), "fake-model"
    )


def _args(output_dir: Path) -> Namespace:
    return Namespace(
        output_dir=str(output_dir),
        tool=None,
        limit=50,
        provider="gemini",
        model=None,
        output=None,
        allow_cloud=True,
        include_automated=False,
    )


@pytest.fixture
def use_provider(monkeypatch):
    """Make `lore knowledge` use the given fake instead of a real provider."""

    def install(provider: FakeProvider) -> None:
        monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)

    return install


def test_migration_14_from_v13(tmp_path):
    full = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in full if m[0] <= 13]
    try:
        conn = initialise(v2_db_path(tmp_path))
    finally:
        MIGRATIONS[:] = full
    try:
        # Raw insert: add_memory writes the status column of migration 16.
        cur = conn.execute(
            "INSERT INTO memory (kind, title, body, author, created, updated) "
            "VALUES ('note', 'keep me', 'body text', 'human', 't', 't')"
        )
        memory_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert current_version(conn) == 13
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'session_knowledge'"
        ).fetchone()
    finally:
        conn.close()

    assert get_session_knowledge(tmp_path, "nope") is None  # opens and migrates

    conn = open_connection(v2_db_path(tmp_path))
    try:
        assert current_version(conn) == max(v for v, _, _ in MIGRATIONS) >= 14
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'session_knowledge'"
        ).fetchone()
    finally:
        conn.close()
    entry = get_memory(tmp_path, memory_id)
    assert entry is not None and entry.title == "keep me"


def test_storage_roundtrip_and_upsert(tmp_path):
    _build(tmp_path, _session("s1", "hello"))
    save_session_knowledge(tmp_path, "s1", "fp1", "m", 1, {"topic": "A"})
    save_session_knowledge(tmp_path, "s1", "fp2", "m", 1, {"topic": "B"})

    row = get_session_knowledge(tmp_path, "s1")
    assert row is not None
    assert row["fingerprint"] == "fp2"
    assert row["payload"] == {"topic": "B"}
    assert [r["session_id"] for r in list_session_knowledge(tmp_path)] == ["s1"]


def test_fingerprint_depends_on_every_input():
    base = prompt_fingerprint("conv", "m", 1)
    assert base == prompt_fingerprint("conv", "m", 1)
    assert len(base) == 64
    assert base != prompt_fingerprint("conv2", "m", 1)
    assert base != prompt_fingerprint("conv", "m2", 1)
    assert base != prompt_fingerprint("conv", "m", 2)


def test_read_paths_do_not_create_a_db(tmp_path):
    assert get_session_knowledge(tmp_path, "s1") is None
    assert list_session_knowledge(tmp_path) == []
    assert not v2_db_path(tmp_path).exists()


def test_run_extraction_skips_unchanged(tmp_path):
    session = _session("s1", "use sqlite-vec for vectors", "ok")
    _build(tmp_path, session)
    provider = FakeProvider()

    first = _run(tmp_path, provider, session)
    second = _run(tmp_path, provider, session)

    assert len(provider.prompts) == 1
    assert "use sqlite-vec for vectors" in provider.prompts[0]
    assert (first.extracted, first.unchanged) == (1, 0)
    assert (second.extracted, second.unchanged) == (0, 1)
    row = get_session_knowledge(tmp_path, "s1")
    assert row is not None
    assert row["model"] == "fake-model"
    assert row["prompt_version"] == KNOWLEDGE_PROMPT_VERSION
    # The entry names the archived revision it was extracted from (#177).
    assert len(row["payload"]["source_revision"]) == 64


def test_changed_session_is_reextracted(tmp_path):
    _build(tmp_path, _session("s1", "first version"))
    provider = FakeProvider()
    _run(tmp_path, provider, _session("s1"))

    _replace_messages(tmp_path, _session("s1", "second version"))
    report = _run(tmp_path, provider, _session("s1"))

    assert len(provider.prompts) == 2
    assert "second version" in provider.prompts[1]
    assert report.extracted == 1


def test_failed_call_stores_nothing_and_retries(tmp_path):
    _build(tmp_path, _session("s1", "hello there"))
    provider = FakeProvider(["not json"])

    first = _run(tmp_path, provider, _session("s1"))

    assert first.failed == 1
    assert get_session_knowledge(tmp_path, "s1") is None

    second = _run(tmp_path, provider, _session("s1"))

    assert len(provider.prompts) == 2
    assert (second.extracted, second.failed) == (1, 0)
    assert get_session_knowledge(tmp_path, "s1") is not None


def test_provider_exception_stores_nothing(tmp_path):
    class Boom(FakeProvider):
        def generate(self, prompt, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("quota")

    _build(tmp_path, _session("s1", "hello there"))

    report = _run(tmp_path, Boom(), _session("s1"))

    assert report.failed == 1
    assert list_session_knowledge(tmp_path) == []


def test_session_without_messages_is_skipped(tmp_path):
    _build(tmp_path, _session("s1", "hello"))
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE sessions SET messages_synced = 0 WHERE id = 's1'")
    finally:
        conn.close()
    provider = FakeProvider()

    report = _run(tmp_path, provider, _session("s1"), _session("unknown"))

    assert report.skipped_no_messages == 2
    assert report.hydration_reasons == {
        "messages-not-synced": 1,
        "session-not-archived": 1,
    }
    assert report.hydration_breakdown() == "1 messages-not-synced, 1 session-not-archived"
    assert provider.prompts == []


def test_knowledge_base_json_is_complete_after_partial_run(tmp_path, use_provider):
    a, b = _session("a", "alpha talk"), _session("b", "beta talk")
    _build(tmp_path, a, b)
    provider = FakeProvider([_payload("Alpha"), _payload("Beta")])
    args = _args(tmp_path)
    use_provider(provider)

    assert lore_cli.cmd_knowledge(args) == 0
    _replace_messages(tmp_path, _session("b", "beta talk changed"))
    provider.responses = [_payload("Beta2")]
    assert lore_cli.cmd_knowledge(args) == 0

    kb = json.loads((tmp_path / "knowledge" / "knowledge_base.json").read_text())
    assert kb["total_entries"] == 2
    assert {e["topic"] for e in kb["entries"]} == {"Alpha", "Beta2"}
    assert len(provider.prompts) == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"topic": None, "key_points": [], "code_snippets": [], "decisions": [], "tools_used": []},
        {"topic": "T", "key_points": 5, "code_snippets": [], "decisions": [], "tools_used": []},
        {"topic": "T", "key_points": [], "code_snippets": [], "decisions": [], "tools_used": [{}]},
        ["not", "a", "dict"],
    ],
)
def test_malformed_payload_counts_as_failed_and_is_not_stored(tmp_path, bad):
    s = _session("s1", "hello there")
    _build(tmp_path, s)

    report = _run(tmp_path, FakeProvider([json.dumps(bad)]), s)

    assert (report.extracted, report.failed) == (0, 1)
    assert get_session_knowledge(tmp_path, "s1") is None


def test_changed_project_reextracts(tmp_path):
    s = _session("s1", "hello there")
    _build(tmp_path, s)
    provider = FakeProvider()
    _run(tmp_path, provider, s)
    moved = _stubs(s)
    moved[0].project_path = "/elsewhere"

    report = run_extraction(tmp_path, moved, _extractor(tmp_path, provider), "fake-model")

    assert report.extracted == 1
    assert len(provider.prompts) == 2


def test_corrupt_row_is_skipped_and_reextracted(tmp_path):
    s = _session("s1", "hello there")
    _build(tmp_path, s)
    provider = FakeProvider()
    _run(tmp_path, provider, s)
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE session_knowledge SET payload_json = '{broken'")
    finally:
        conn.close()

    assert get_session_knowledge(tmp_path, "s1") is None
    assert list_session_knowledge(tmp_path) == []
    assert _run(tmp_path, provider, s).extracted == 1


def test_cmd_knowledge_exits_one_when_every_call_fails(tmp_path, use_provider, capsys):
    _build(tmp_path, _session("s1", "hello there"))
    use_provider(FakeProvider(["not json"]))

    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 1

    assert "all 1 extraction calls failed" in capsys.readouterr().err


def test_cmd_knowledge_tool_flag_scopes_the_output(tmp_path, use_provider):
    a, b = _session("claude-one", "alpha talk"), _session("cursor-one", "beta talk")
    b.tool = Tool.CURSOR
    _build(tmp_path, a, b)
    use_provider(FakeProvider([_payload("Alpha"), _payload("Beta")]))
    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0

    args = _args(tmp_path)
    args.tool = "cursor"
    assert lore_cli.cmd_knowledge(args) == 0

    kb = json.loads((tmp_path / "knowledge" / "knowledge_base.json").read_text())
    assert [e["session_id"] for e in kb["entries"]] == ["cursor-one"]


def test_cmd_knowledge_excludes_deleted_sessions(tmp_path, use_provider):
    """A tombstoned session is neither sent to the provider nor kept in the output."""
    a, b = _session("keep", "alpha talk"), _session("gone", "beta talk")
    _build(tmp_path, a, b)
    provider = FakeProvider([_payload("Keep"), _payload("Gone")])
    use_provider(provider)
    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0
    assert len(provider.prompts) == 2

    (tmp_path / "deleted_sessions.json").write_text(json.dumps({"session_ids": ["gone"]}))
    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0

    kb = json.loads((tmp_path / "knowledge" / "knowledge_base.json").read_text())
    assert [e["session_id"] for e in kb["entries"]] == ["keep"]
    assert len(provider.prompts) == 2


def test_cmd_knowledge_clears_stale_output_when_every_session_is_deleted(tmp_path, use_provider):
    """The old file must not keep the text of sessions deleted since the last run."""
    _build(tmp_path, _session("gone", "beta talk"))
    use_provider(FakeProvider([_payload("Secret topic")]))
    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0
    kb_path = tmp_path / "knowledge" / "knowledge_base.json"
    assert "Secret topic" in kb_path.read_text()

    (tmp_path / "deleted_sessions.json").write_text(json.dumps({"session_ids": ["gone"]}))
    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0

    kb = json.loads(kb_path.read_text())
    assert kb["total_entries"] == 0
    assert "Secret topic" not in kb_path.read_text()


def test_cmd_knowledge_does_not_extract_deleted_session(tmp_path, use_provider):
    _build(tmp_path, _session("gone", "beta talk"))
    (tmp_path / "deleted_sessions.json").write_text(json.dumps({"session_ids": ["gone"]}))
    provider = FakeProvider()
    use_provider(provider)

    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0

    assert provider.prompts == []


def test_cmd_knowledge_exit_zero_when_nothing_new(tmp_path, use_provider, capsys):
    _build(tmp_path, _session("s1", "hello there"))
    provider = FakeProvider()
    args = _args(tmp_path)
    use_provider(provider)
    assert lore_cli.cmd_knowledge(args) == 0
    capsys.readouterr()

    assert lore_cli.cmd_knowledge(args) == 0

    out = capsys.readouterr().out
    assert "nothing new" in out.lower()
    assert len(provider.prompts) == 1
    assert (tmp_path / "knowledge" / "knowledge_base.json").exists()


def test_cmd_knowledge_names_provider_before_sending(tmp_path, use_provider, capsys):
    _build(tmp_path, _session("s1", "hello there"))
    seen: List[str] = []
    provider = FakeProvider()
    original = provider.generate

    def spy(prompt, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(capsys.readouterr().out)
        return original(prompt, *args, **kwargs)

    provider.generate = spy  # type: ignore[method-assign]
    use_provider(provider)

    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0

    assert "gemini" in seen[0] and "gemini-2.0-flash" in seen[0]


def test_cmd_knowledge_session_without_messages_exits_zero(tmp_path, use_provider, capsys):
    _build(tmp_path, _session("s1", "hello"))
    conn = open_connection(v2_db_path(tmp_path))
    try:
        conn.execute("UPDATE sessions SET messages_synced = 0")
    finally:
        conn.close()
    provider = FakeProvider()
    use_provider(provider)

    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0
    assert provider.prompts == []


def test_cmd_knowledge_tool_scope_ignores_the_limit(tmp_path, use_provider):
    # --limit bounds what is sent to the provider, not which stored rows are reported.
    old = _session("claude-old", "alpha talk")
    new = _session("claude-new", "beta talk")
    new.created_at = datetime(2026, 2, 1)
    _build(tmp_path, old, new)
    use_provider(FakeProvider([_payload("Alpha"), _payload("Beta")]))
    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 0

    args = _args(tmp_path)
    args.tool = "claude-code"
    args.limit = 1
    assert lore_cli.cmd_knowledge(args) == 0

    kb = json.loads((tmp_path / "knowledge" / "knowledge_base.json").read_text())
    assert {e["session_id"] for e in kb["entries"]} == {"claude-old", "claude-new"}


# --- #199: failures explain themselves, and the write commits explicitly -----


def test_failure_buckets_distinguish_provider_problems_from_bad_answers(tmp_path):
    """`failed` is the total, but the cause is counted separately (#199)."""
    _build(tmp_path, _session("s1", "hello there"))

    unparsable = _run(tmp_path, FakeProvider(["not json"]), _session("s1"))
    assert (unparsable.failed, unparsable.unparsable) == (1, 1)
    assert (unparsable.provider_unavailable, unparsable.provider_error) == (0, 0)

    class Boom(FakeProvider):
        def generate(self, prompt, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("quota")

    errored = _run(tmp_path, Boom(), _session("s1"))
    assert (errored.failed, errored.provider_error, errored.unparsable) == (1, 1, 0)

    class Unavailable(FakeProvider):
        def is_available(self) -> bool:
            return False

    absent = _run(tmp_path, Unavailable(), _session("s1"))
    assert (absent.failed, absent.provider_unavailable) == (1, 1)
    assert (absent.provider_error, absent.unparsable) == (0, 0)
    assert absent.provider_unavailable + absent.provider_error + absent.unparsable == absent.failed


def test_cmd_knowledge_error_names_the_failure_cause(tmp_path, use_provider, capsys):
    """Exit code 1 says which kind of failure it was (#199)."""
    _build(tmp_path, _session("s1", "hello there"))
    use_provider(FakeProvider(["not json"]))

    assert lore_cli.cmd_knowledge(_args(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "all 1 extraction calls failed" in err
    assert "1 unparsable" in err


def test_save_session_knowledge_brackets_an_explicit_transaction(tmp_path, monkeypatch):
    """The upsert must not lean on autocommit: it brackets itself (#199)."""
    from lore.storage import knowledge as knowledge_mod
    from lore.storage.writer import v2_db_path

    initialise(v2_db_path(tmp_path))  # schema is current, so nothing else emits BEGIN
    statements: List[str] = []
    real_open = knowledge_mod.open_v2_connection

    def tracing_open(path: Path):
        conn = real_open(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(knowledge_mod, "open_v2_connection", tracing_open)
    save_session_knowledge(tmp_path, "s1", "fp", "fake-model", 1, {"topic": "T"})

    sql = [s.strip().split()[0].upper() for s in statements if s.strip()]
    assert sql.count("BEGIN") == 1 and sql.count("COMMIT") == 1
    assert sql.index("BEGIN") < sql.index("INSERT") < sql.index("COMMIT")
    assert get_session_knowledge(tmp_path, "s1")["fingerprint"] == "fp"
