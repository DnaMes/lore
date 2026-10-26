"""Finding 3 probes: the FINISHED outgoing prompt is redacted before every send.

A synthetic GitHub token placed in the session's project path or title must never
reach the mocked transport of any send path that can leave the machine:

1. gemini-cli title path (subprocess transport),
2. remote Ollama title path (ollama client.chat transport),
3. stats insights (Gemini SDK generate_content transport).

With opt-in the transport is called exactly once with a redacted prompt and the
prompt is still the complete one; without opt-in the cloud gate refuses first and
0 transport calls happen.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.llm import CloudOptInRequired, LLMConfig
from lore.llm.gemini import GeminiProvider
from lore.llm.tasks.stats import StatsGenerator
from lore.titles.generator import TitleGenerator, TitleStrategy

TOKEN = "ghp_" + "z" * 36  # matches the redact() github_token pattern

INSIGHTS_JSON = json.dumps(
    {
        "patterns": "ok",
        "productivity": "ok",
        "recommendations": [],
        "trends": [],
    }
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


def _session(project_path: str = "/work/repo", title: str = "") -> UnifiedSession:
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
        session_id="redaction-probe",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project_path=project_path,
        title=title,
        messages=turns,
    )


# --- 1. gemini-cli title path --------------------------------------------------


def _title_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> TitleGenerator:
    monkeypatch.setattr("lore.titles.generator.OLLAMA_AVAILABLE", True)
    return TitleGenerator(cache_dir=tmp_path / "title_cache", **kwargs)


def _install_subprocess_capture(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    seen: List[Any] = []

    def fake_run(cmd: Any, **kwargs: Any) -> SimpleNamespace:
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="A Title")

    monkeypatch.setattr("subprocess.run", fake_run)
    return seen


def test_gemini_cli_title_optin_redacts_project_path_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_subprocess_capture(monkeypatch)
    gen = _title_generator(
        tmp_path, monkeypatch, strategy=TitleStrategy.SMART, provider="gemini-cli", allow_cloud=True
    )

    title = gen.generate(_session(project_path=f"/work/{TOKEN}/repo"), force=True)

    assert title == "A Title"
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    prompt = kwargs["input"]
    # The excerpt travels through stdin; argv holds no session text.
    assert all(prompt not in part for part in cmd)
    assert TOKEN not in prompt
    assert "[REDACTED:github_token]" in prompt
    # Redaction runs over the finished prompt: its structure survives intact.
    assert "Generate a concise title" in prompt
    assert "Project:" in prompt
    assert "Conversation:" in prompt


def test_gemini_cli_title_optin_redacts_session_title_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_subprocess_capture(monkeypatch)
    gen = _title_generator(
        tmp_path, monkeypatch, strategy=TitleStrategy.SMART, provider="gemini-cli", allow_cloud=True
    )

    gen.generate(_session(title=f"rotate {TOKEN}"), force=True)

    assert len(calls) == 1
    assert TOKEN not in calls[0][1]["input"]


def test_gemini_cli_title_without_optin_zero_subprocess_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_subprocess_capture(monkeypatch)
    gen = _title_generator(
        tmp_path, monkeypatch, strategy=TitleStrategy.SMART, provider="gemini-cli"
    )

    with pytest.raises(CloudOptInRequired):
        gen.generate(_session(project_path=f"/work/{TOKEN}/repo"), force=True)

    assert calls == []


# --- 2. remote Ollama title path ----------------------------------------------


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


def test_remote_ollama_title_optin_redacts_project_path_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://192.0.2.1:11434")
    recorder = _RecordingOllama()
    monkeypatch.setattr("lore.titles.generator.ollama", recorder.module())
    gen = _title_generator(
        tmp_path, monkeypatch, strategy=TitleStrategy.SMART, provider="local", allow_cloud=True
    )

    title = gen.generate(_session(project_path=f"/work/{TOKEN}/repo"), force=True)

    assert title == "Remote Title"
    assert recorder.clients == 1 and len(recorder.prompts) == 1
    prompt = recorder.prompts[0]
    assert TOKEN not in prompt
    assert "[REDACTED:github_token]" in prompt
    assert "Generate a concise title" in prompt


def test_remote_ollama_title_without_optin_zero_chat_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://192.0.2.1:11434")
    recorder = _RecordingOllama()
    monkeypatch.setattr("lore.titles.generator.ollama", recorder.module())
    gen = _title_generator(tmp_path, monkeypatch, strategy=TitleStrategy.SMART, provider="local")

    with pytest.raises(CloudOptInRequired):
        gen.generate(_session(title=f"rotate {TOKEN}"), force=True)

    assert recorder.clients == 0
    assert recorder.prompts == []


# --- 3. stats insights --------------------------------------------------------


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
                    text=INSIGHTS_JSON, usage_metadata=SimpleNamespace(total_token_count=1)
                )

        return _Model()


def _gemini_provider(tmp_path: Path, captured: Dict[str, Any], **kwargs: Any) -> GeminiProvider:
    provider = GeminiProvider(
        LLMConfig.for_transcripts(
            provider="gemini", model="gemini-2.0-flash", cache_dir=str(tmp_path / "cache")
        ),
        **kwargs,
    )
    provider._get_client = lambda: _FakeGenai(captured)  # type: ignore[method-assign]
    return provider


def test_stats_insights_optin_redacts_title_and_project_tokens(
    tmp_path: Path,
) -> None:
    captured: Dict[str, Any] = {}
    provider = _gemini_provider(tmp_path, captured, allow_cloud=True)
    sessions = [
        _session(project_path=f"/work/{TOKEN}/repo", title="clean"),
        _session(project_path="/p2", title=f"rotate {TOKEN}"),
    ]
    generator = StatsGenerator(provider, tmp_path / "stats")

    stats = generator.generate_session_stats(sessions, use_llm=True)

    prompt = captured["prompt"]
    assert TOKEN not in prompt
    assert "[REDACTED:github_token]" in prompt
    assert generator.redactions >= 2  # one in the project metadata, one in the title
    assert "Analyze these AI coding session statistics" in prompt  # finished prompt intact
    assert stats["insights"]["patterns"] == "ok"


def test_stats_insights_without_optin_zero_transport_calls(
    tmp_path: Path,
) -> None:
    captured: Dict[str, Any] = {}
    provider = _gemini_provider(tmp_path, captured)
    generator = StatsGenerator(provider, tmp_path / "stats")

    stats = generator.generate_session_stats(
        [_session(project_path=f"/work/{TOKEN}/repo", title=f"rotate {TOKEN}")],
        use_llm=True,
    )

    assert "prompt" not in captured  # 0 transport calls
    assert stats["insights"]["patterns"] == "Unable to generate insights"
