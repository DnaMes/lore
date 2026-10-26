"""Cloud opt-in probes at the transport boundary (library callers, not only the CLI).

1. A remote ``OLLAMA_HOST`` makes the library title path a cloud send: it must
   refuse (0 ``chat()`` calls) without opt-in and proceed with opt-in. Loopback
   Ollama titles keep working without opt-in.
2. Directly constructed Gemini/OpenRouter/Jev providers must refuse at the
   transport boundary just like factory-built ones; ``generate_batch`` goes
   through the same gate.
3. The gemini-cli title path refuses before any subprocess runs.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.llm import CloudOptInRequired, LLMConfig
from lore.llm.factory import get_provider
from lore.llm.gemini import GeminiProvider
from lore.llm.jev import JevProvider
from lore.llm.openrouter import OpenRouterProvider
from lore.titles.generator import TitleGenerator, TitleStrategy


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LORE_JEV_LANES", raising=False)


def _session(project_path: str = "/work/repo") -> UnifiedSession:
    turns = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"turn {i}: ok",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(12)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="cloud-gate-probe",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project_path=project_path,
        title="",
        messages=turns,
    )


def _title_generator(tmp_path: Path, **kwargs: Any) -> TitleGenerator:
    import lore.titles.generator as gen_mod

    gen_mod.OLLAMA_AVAILABLE = True
    return TitleGenerator(cache_dir=tmp_path / "title_cache", **kwargs)


# --- probe 1: Ollama in the library title path ---------------------------------


class _RecordingOllama:
    """Stands in for the ``ollama`` module: counts Client() and chat() calls."""

    def __init__(self) -> None:
        self.clients = 0
        self.prompts: List[str] = []

    def module(self) -> SimpleNamespace:
        outer = self

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None:
                outer.clients += 1

            def chat(self, **kwargs: Any) -> Dict[str, Any]:
                outer.prompts.append(kwargs["messages"][0]["content"])
                return {"message": {"content": "Remote Title"}}

        return SimpleNamespace(Client=_Client)


def test_remote_ollama_title_refuses_without_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://192.0.2.1:11434")
    recorder = _RecordingOllama()
    monkeypatch.setattr("lore.titles.generator.ollama", recorder.module())
    gen = _title_generator(tmp_path, strategy=TitleStrategy.SMART, provider="local")

    with pytest.raises(CloudOptInRequired) as exc_info:
        gen.generate(_session(), force=True)

    assert "--allow-cloud" in str(exc_info.value)
    assert recorder.clients == 0  # no client, no chat
    assert recorder.prompts == []


@pytest.mark.parametrize("optin", ["env", "arg"])
def test_remote_ollama_title_with_optin_reaches_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, optin: str
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://192.0.2.1:11434")
    if optin == "env":
        monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
        kwargs: Dict[str, Any] = {}
    else:
        kwargs = {"allow_cloud": True}
    recorder = _RecordingOllama()
    monkeypatch.setattr("lore.titles.generator.ollama", recorder.module())
    gen = _title_generator(tmp_path, strategy=TitleStrategy.SMART, provider="local", **kwargs)

    title = gen.generate(_session(), force=True)

    assert title == "Remote Title"
    assert recorder.clients == 1 and len(recorder.prompts) == 1
    assert "Generate a concise title" in recorder.prompts[0]


def test_loopback_ollama_title_still_works_without_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingOllama()
    monkeypatch.setattr("lore.titles.generator.ollama", recorder.module())
    gen = _title_generator(tmp_path, strategy=TitleStrategy.SMART, provider="local")

    title = gen.generate(_session(), force=True)

    assert title == "Remote Title"
    assert recorder.clients == 1 and len(recorder.prompts) == 1


# --- probe 2: directly constructed providers ----------------------------------


class _FakeGenai:
    """Minimal ``google.generativeai`` stand-in recording generate_content calls."""

    def __init__(self, captured: Dict[str, Any]) -> None:
        self._captured = captured

    def configure(self, **kwargs: Any) -> None:  # noqa: ARG002
        pass

    class types:  # noqa: N801 - mirrors the SDK attribute
        @staticmethod
        def GenerationConfig(**kwargs: Any) -> None:  # noqa: ARG002
            return None

    def GenerativeModel(self, model: str, system_instruction: str = None):  # noqa: N802
        captured = self._captured

        class _Model:
            def generate_content(self, prompt: str, generation_config: Any = None):  # noqa: ANN001
                captured["prompt"] = prompt
                return SimpleNamespace(
                    text="ok", usage_metadata=SimpleNamespace(total_token_count=1)
                )

        return _Model()


def test_gemini_direct_without_optin_never_calls_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: Dict[str, Any] = {}
    provider = GeminiProvider(
        LLMConfig.for_transcripts(provider="gemini", cache_dir=str(tmp_path / "c"))
    )
    monkeypatch.setattr(provider, "_get_client", lambda: _FakeGenai(captured))

    with pytest.raises(CloudOptInRequired):
        provider.generate("synthetic transcript")

    assert "prompt" not in captured  # 0 transport calls


def test_gemini_direct_with_allow_cloud_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: Dict[str, Any] = {}
    provider = GeminiProvider(
        LLMConfig.for_transcripts(provider="gemini", cache_dir=str(tmp_path / "c")),
        allow_cloud=True,
    )
    monkeypatch.setattr(provider, "_get_client", lambda: _FakeGenai(captured))

    response = provider.generate("synthetic transcript")

    assert response.content == "ok"
    assert captured["prompt"] == "synthetic transcript"


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_openrouter_direct_without_optin_never_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []
    monkeypatch.setattr("lore.llm.openrouter.urlopen", lambda *a, **k: calls.append(a))
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False, api_key="k" * 16)
    )

    with pytest.raises(CloudOptInRequired):
        provider.generate("synthetic transcript")

    assert calls == []


def test_openrouter_direct_with_allow_cloud_works(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}}
    ).encode()
    calls: List[Any] = []

    def fake_urlopen(request: Any, timeout: Any = None) -> _FakeHTTPResponse:
        calls.append(request)
        return _FakeHTTPResponse(body)

    monkeypatch.setattr("lore.llm.openrouter.urlopen", fake_urlopen)
    provider = OpenRouterProvider(
        LLMConfig(provider="openrouter", model="m:free", cache_enabled=False, api_key="k" * 16),
        allow_cloud=True,
    )

    response = provider.generate("synthetic transcript")

    assert response.content == "ok"
    assert len(calls) == 1


def test_jev_direct_without_optin_never_calls_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_JEV_LANES", "local")
    runs: List[Any] = []
    monkeypatch.setattr(
        "lore.llm.jev.subprocess.run",
        lambda *a, **k: runs.append(a) or subprocess.CompletedProcess(a[0], 0, stdout="ok"),
    )
    provider = JevProvider(LLMConfig(provider="jev", model="local", cache_enabled=False))

    with pytest.raises(CloudOptInRequired):
        provider.generate("synthetic transcript")

    assert runs == []


def test_jev_direct_with_allow_cloud_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_JEV_LANES", "local")
    runs: List[Any] = []

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("lore.llm.jev.subprocess.run", fake_run)
    provider = JevProvider(
        LLMConfig(provider="jev", model="local", cache_enabled=False), allow_cloud=True
    )

    response = provider.generate("synthetic transcript")

    assert response.content == "ok"
    assert runs == [["delegate", "--type", "local"]]


@pytest.mark.parametrize("name", ["gemini", "openrouter", "jev"])
def test_generate_batch_is_gated_through_generate(name: str) -> None:
    provider = get_provider(name, allow_cloud=True)
    # Rebuild without opt-in: batch must hit the same transport gate as generate.
    gated = type(provider)(provider.config)

    with pytest.raises(CloudOptInRequired):
        gated.generate_batch(["a", "b"])


# --- probe 3: gemini-cli title path refuses before the subprocess -------------


def test_gemini_cli_title_without_optin_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess must not run")),
    )
    gen = _title_generator(tmp_path, strategy=TitleStrategy.SMART, provider="gemini-cli")

    with pytest.raises(CloudOptInRequired):
        gen.generate(_session(), force=True)
