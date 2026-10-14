"""MCP tools get_session_summary and summarize_session (B4.5, #197)."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import lore.llm
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import mcp
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.services.summarize import SUMMARY_PROMPT_VERSION
from lore.storage import initialise
from lore.storage.summaries import save_summary, summary_fingerprint
from lore.storage.writer import v2_db_path, write_sessions

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SID = "0b0d5f5e-7a46-4b0e-9a43-1c2d3e4f5a6b"
SENTINEL = "SENTINEL_TRANSCRIPT_BODY_DO_NOT_PRINT"
MODEL_LABEL = "ollama/llama3.2"


def _server(tmp_path: Path):
    server = mcp.create_server()
    server.output_dir = tmp_path
    return server


def _call(server, name: str, arguments: dict) -> str:
    handler = server.tools[name]["handler"]
    return asyncio.run(handler(arguments))


def _msg(role: Role, content: str, **kwargs: Any) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=T0, **kwargs)


def _session(sid: str, *contents: str, **kwargs: Any) -> UnifiedSession:
    msgs = [
        _msg(Role.USER if i % 2 == 0 else Role.ASSISTANT, text) for i, text in enumerate(contents)
    ]
    return UnifiedSession(
        tool=kwargs.get("tool", Tool.CLAUDE_CODE),
        session_id=sid,
        created_at=T0,
        last_updated=kwargs.get("last_updated", T0),
        project_path=kwargs.get("project_path", "/proj"),
        origin=kwargs.get("origin", "interactive"),
        messages=msgs,
    )


def _rich_session(sid: str = SID, *, sentinel: bool = False) -> UnifiedSession:
    chunks: list[str] = []
    for i in range(6):
        chunks.append(f"User goal paragraph {i} " + ("detail " * 80))
        tool_tail = SENTINEL if sentinel else "Error: something failed"
        chunks.append(f"[Tool: Read] /src/module.py\n[Tool Result]\n{tool_tail}\n" + ("log " * 40))
    return _session(sid, *chunks)


def _build_store(output_dir: Path, *sessions: UnifiedSession) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    initialise(v2_db_path(output_dir))
    write_sessions(v2_db_path(output_dir), sessions)


def _llm_summary(cite: int = 1) -> dict[str, Any]:
    return {
        "one_liner": "Shipped the feature",
        "goal": {"text": "build feature", "cites": [cite]},
        "outcome": {"status": "done", "text": "shipped", "cites": [cite]},
        "decisions": [],
        "open_questions": [],
        "next_steps": [],
        "topics": ["feature"],
        "confidence": 0.85,
    }


def _stored_summary_payload() -> dict[str, Any]:
    return {
        **_llm_summary(),
        "artifacts": {
            "files_read": [],
            "files_modified": [],
            "commands": [],
            "errors": [{"seq": 2, "text": "x" * 300}],
        },
        "truncated": False,
        "chunks": 0,
    }


class FakeProvider(LLMProvider):
    def __init__(
        self,
        responses: Optional[List[str]] = None,
        barrier: Optional[threading.Barrier] = None,
    ) -> None:
        super().__init__(LLMConfig.for_transcripts(provider="fake", model="test"))
        self.prompts: List[str] = []
        self.responses = list(responses or [])
        self.barrier = barrier
        self.generate_calls = 0

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        if self.barrier is not None:
            self.barrier.wait()
        self.generate_calls += 1
        self.prompts.append(prompt)
        content = self.responses.pop(0) if self.responses else json.dumps(_llm_summary())
        return LLMResponse(content=content, model="test", tokens_used=120)

    def generate_batch(
        self, prompts: List[str], system_prompt: Optional[str] = None
    ) -> List[LLMResponse]:
        return [self.generate(p, system_prompt=system_prompt) for p in prompts]

    def is_available(self) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"model": "test"}


@pytest.fixture
def use_provider(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, FakeProvider] = {}

    def install(provider: FakeProvider) -> FakeProvider:
        holder["provider"] = provider
        monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)
        return provider

    install.holder = holder  # type: ignore[attr-defined]
    return install


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_summary_tools_registered_with_schemas(tmp_path: Path):
    server = _server(tmp_path)
    names = {t["name"] for t in server.tools.values()}
    assert "get_session_summary" in names
    assert "summarize_session" in names

    get_tool = server.tools["get_session_summary"]
    assert get_tool["inputSchema"]["required"] == ["session_id"]
    assert get_tool.get("annotations", {}).get("readOnlyHint") is True

    sum_tool = server.tools["summarize_session"]
    assert "session_id" in sum_tool["inputSchema"]["properties"]
    assert sum_tool.get("annotations", {}).get("readOnlyHint") is False


# ---------------------------------------------------------------------------
# get_session_summary
# ---------------------------------------------------------------------------


def test_get_session_summary_invalid_id(tmp_path: Path):
    server = _server(tmp_path)
    payload = json.loads(_call(server, "get_session_summary", {"session_id": "not-valid!!!"}))
    assert payload["status"] == "invalid_id"


def test_get_session_summary_missing(tmp_path: Path):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    payload = json.loads(_call(server, "get_session_summary", {"session_id": SID}))
    assert payload["status"] == "missing"
    assert payload["stale"] is None
    assert payload["summary"] is None


def test_get_session_summary_available(tmp_path: Path):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    body = _stored_summary_payload()
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{SID}",
        session_id=SID,
        fingerprint=summary_fingerprint("x", MODEL_LABEL, 1, 1),
        model=MODEL_LABEL,
        prompt_version=SUMMARY_PROMPT_VERSION,
        compressor_version=1,
        payload=body,
    )
    payload = json.loads(_call(server, "get_session_summary", {"session_id": SID}))
    assert payload["status"] == "available"
    assert payload["model"] == MODEL_LABEL
    assert payload["summary"]["one_liner"] == "Shipped the feature"
    assert payload["truncated"] is False
    assert payload["chunks"] == 0
    artifacts = payload["summary"]["artifacts"]
    assert "errors" not in artifacts and "commands" not in artifacts
    assert artifacts["errors_count"] >= 1  # counted, but the transcript-derived text is not
    assert "x" * 300 not in json.dumps(payload)


def test_get_session_summary_never_returns_transcript_sentinel(tmp_path: Path):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session(sentinel=True))
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{SID}",
        session_id=SID,
        fingerprint="fp",
        model=MODEL_LABEL,
        prompt_version=1,
        compressor_version=1,
        payload=_stored_summary_payload(),
    )
    raw = _call(server, "get_session_summary", {"session_id": SID})
    assert SENTINEL not in raw


def test_get_session_summary_stale_after_new_messages(tmp_path: Path):
    server = _server(tmp_path)
    session = _rich_session()
    _build_store(tmp_path, session)
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{SID}",
        session_id=SID,
        fingerprint=summary_fingerprint("stale", MODEL_LABEL, 1, 1),
        model=MODEL_LABEL,
        prompt_version=SUMMARY_PROMPT_VERSION,
        compressor_version=1,
        payload=_stored_summary_payload(),
    )
    session.messages.append(_msg(Role.USER, "brand new user message " + ("z" * 200)))
    write_sessions(v2_db_path(tmp_path), [session])

    payload = json.loads(_call(server, "get_session_summary", {"session_id": SID}))
    assert payload["status"] == "available"
    assert payload["stale"] is True


def test_get_session_summary_deleted(tmp_path: Path):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    save_summary(
        tmp_path,
        level="session",
        scope_key=f"session:{SID}",
        session_id=SID,
        fingerprint="fp",
        model=MODEL_LABEL,
        prompt_version=1,
        compressor_version=1,
        payload=_stored_summary_payload(),
    )
    (tmp_path / "deleted_sessions.json").write_text(json.dumps({"session_ids": [SID]}))
    payload = json.loads(_call(server, "get_session_summary", {"session_id": SID}))
    assert payload["status"] == "deleted"
    assert payload["summary"] is None


# ---------------------------------------------------------------------------
# summarize_session
# ---------------------------------------------------------------------------


def test_summarize_session_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.delenv("LORE_MCP_ALLOW_SUMMARIZE", raising=False)
    monkeypatch.setattr(
        lore.llm,
        "get_provider",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )
    payload = json.loads(_call(server, "summarize_session", {"session_id": SID}))
    assert payload["error"] == "summarize_session is disabled"
    assert "LORE_MCP_ALLOW_SUMMARIZE" in payload["hint"]


def test_summarize_session_enabled_writes_row(tmp_path: Path, use_provider, monkeypatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    provider = use_provider(FakeProvider())

    payload = json.loads(_call(server, "summarize_session", {"session_id": SID}))

    assert payload["outcome"] == "written"
    assert payload["summary_available"] is True
    assert "summary" not in payload
    assert provider.prompts


def test_summarize_session_ignores_cloud_env_and_provider_arg(
    tmp_path: Path, use_provider, monkeypatch
):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    monkeypatch.setenv("LORE_SUMMARY_PROVIDER", "gemini")
    seen: list[str] = []

    def spy(**kwargs):
        seen.append(str(kwargs.get("provider")))
        return FakeProvider()

    monkeypatch.setattr(lore.llm, "get_provider", spy)
    _call(
        server,
        "summarize_session",
        {"session_id": SID, "provider": "gemini"},
    )
    assert seen == ["ollama"]


def test_summarize_session_refuses_off_host_base_url(tmp_path: Path, monkeypatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    monkeypatch.setenv("OLLAMA_HOST", "https://remote.example.com")
    monkeypatch.setattr(
        lore.llm,
        "get_provider",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )
    payload = json.loads(_call(server, "summarize_session", {"session_id": SID}))
    assert "error" in payload
    assert "off-host" in payload["error"].lower() or "local" in payload["error"].lower()


def test_summarize_session_tombstoned_without_provider(tmp_path: Path, use_provider, monkeypatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    (tmp_path / "deleted_sessions.json").write_text(json.dumps({"session_ids": [SID]}))
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    provider = use_provider(FakeProvider())

    payload = json.loads(_call(server, "summarize_session", {"session_id": SID}))

    assert "error" in payload
    assert provider.prompts == []


def test_summarize_session_force_resends(tmp_path: Path, use_provider, monkeypatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    provider = use_provider(FakeProvider())

    _call(server, "summarize_session", {"session_id": SID})
    _call(server, "summarize_session", {"session_id": SID, "force": True})

    assert len(provider.prompts) == 2


def test_summarize_session_unchanged_without_force(tmp_path: Path, use_provider, monkeypatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    provider = use_provider(FakeProvider())

    first = json.loads(_call(server, "summarize_session", {"session_id": SID}))
    second = json.loads(_call(server, "summarize_session", {"session_id": SID}))

    assert first["outcome"] == "written"
    assert second["outcome"] == "unchanged"
    assert len(provider.prompts) == 1


def test_summarize_session_concurrent_single_provider_call(
    tmp_path: Path, use_provider, monkeypatch
):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    provider = use_provider(FakeProvider())
    start = threading.Barrier(2)

    def run():
        start.wait()
        return _call(server, "summarize_session", {"session_id": SID})

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert provider.generate_calls == 1


def test_summarize_session_result_has_no_summary_text(tmp_path: Path, use_provider, monkeypatch):
    server = _server(tmp_path)
    _build_store(tmp_path, _rich_session())
    monkeypatch.setenv("LORE_MCP_ALLOW_SUMMARIZE", "1")
    use_provider(FakeProvider())
    raw = _call(server, "summarize_session", {"session_id": SID})
    assert "one_liner" not in raw
    assert "Shipped the feature" not in raw


def test_returned_payload_is_allow_listed_and_has_no_transcript_derived_text(tmp_path):
    from lore.interfaces.mcp_tools.summaries import _sanitize_summary_payload

    payload = {
        "one_liner": "ok",
        "goal": {"text": "g", "cites": [1]},
        "artifacts": {
            "files_read": ["/a.py"],
            "commands": ["curl SENTINEL_COMMAND"],
            "errors": [{"seq": 2, "text": "SENTINEL_ERROR_TEXT"}],
        },
        "unexpected": "SENTINEL_EXTRA",
        "redactions": 3,
    }

    cleaned = _sanitize_summary_payload(payload)

    dumped = json.dumps(cleaned)
    assert "SENTINEL" not in dumped
    assert cleaned["artifacts"] == {"files_read": ["/a.py"], "commands_count": 1, "errors_count": 1}


def test_localhost_is_only_trusted_when_it_resolves_to_loopback(monkeypatch):
    import socket

    from lore.services import summarize_cli as cli

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("203.0.113.9", 0))])
    assert cli.llm_base_url_is_local("http://localhost:11434") is False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    assert cli.llm_base_url_is_local("http://localhost:11434") is True
    assert cli.llm_base_url_is_local("http://127.0.0.1:11434") is True
    assert cli.llm_base_url_is_local("http://[::1]:11434") is True
    assert cli.llm_base_url_is_local("http://ollama.internal:11434") is False
    assert cli.llm_base_url_is_local("http://0.0.0.0:11434") is False
