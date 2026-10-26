"""Library-level cloud opt-in gate (factory chokepoint + gemini-cli title path).

The CLI handlers already exit 2 before reaching the provider layer; these tests pin
the library behaviour: a non-local provider is never constructed without opt-in, and
secret-shaped strings are redacted before an excerpt leaves for the gemini CLI.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lore.llm import CloudOptInRequired, LLMConfig
from lore.llm.factory import get_provider
from lore.llm.gemini import GeminiProvider
from lore.llm.openrouter import OpenRouterProvider
from lore.titles.generator import TitleGenerator, TitleStrategy
from lore.utils.redact import redact

TOKEN = "ghp_" + "z" * 36  # matches the redact() github_token pattern


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)


@pytest.mark.parametrize("provider", ["gemini", "openrouter", "jev"])
def test_get_provider_cloud_without_optin_raises(provider: str) -> None:
    with pytest.raises(CloudOptInRequired) as exc_info:
        get_provider(provider)
    assert "--allow-cloud" in str(exc_info.value)
    assert "LORE_ALLOW_CLOUD=1" in str(exc_info.value)


def test_get_provider_optin_via_arg_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(get_provider("gemini", allow_cloud=True), GeminiProvider)
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    assert isinstance(get_provider("openrouter"), OpenRouterProvider)


def test_get_provider_loopback_ollama_needs_no_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert get_provider("ollama").config.provider == "ollama"


def test_get_provider_off_host_ollama_is_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.5:11434")
    with pytest.raises(CloudOptInRequired):
        get_provider("ollama")


def test_get_provider_unknown_provider_still_valueerror() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("not-a-provider")


# --- run_improve --------------------------------------------------------------


def test_run_improve_gemini_without_optin_never_constructs_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)

    def _boom(self, config):  # noqa: ANN001
        raise AssertionError("provider must not be constructed without cloud opt-in")

    monkeypatch.setattr(GeminiProvider, "__init__", _boom)
    with pytest.raises(CloudOptInRequired):
        lore_cli_improve(tmp_path)
    assert (tmp_path / "knowledge").exists() is False


def lore_cli_improve(output_dir: Path):
    """Library entry exactly as the task pins it (no CLI gate in front)."""
    from lore.services.improve import run_improve

    return run_improve(
        output_dir,
        provider_name="gemini",
        model=None,
        limit=1,
        stages=["extract_knowledge"],
    )


def test_run_improve_optin_via_arg_reaches_get_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    seen: list[dict] = []

    def fake_get_provider(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            is_available=lambda: True,
            config=LLMConfig.for_transcripts(provider="gemini", model="gemini-2.0-flash"),
        )

    def fake_run_extraction(_out, _sessions, _extractor, _label):
        return SimpleNamespace(
            extracted=1,
            unchanged=0,
            skipped_no_messages=0,
            failed=0,
            redactions=0,
        )

    monkeypatch.setattr("lore.services.improve.get_provider", fake_get_provider)
    monkeypatch.setattr("lore.services.improve.run_extraction", fake_run_extraction)

    from lore.services.improve import run_improve

    results = run_improve(
        tmp_path,
        provider_name="gemini",
        model=None,
        limit=1,
        stages=["extract_knowledge"],
        allow_cloud=True,
    )

    assert len(seen) == 1
    assert seen[0]["allow_cloud"] is True
    assert results[0].status == "completed"


def test_run_improve_loopback_ollama_needs_no_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    seen: list[dict] = []

    def fake_get_provider(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            is_available=lambda: True,
            config=LLMConfig.for_transcripts(provider="ollama", model="llama3.2"),
        )

    monkeypatch.setattr("lore.services.improve.get_provider", fake_get_provider)
    monkeypatch.setattr(
        "lore.services.improve.run_extraction",
        lambda *_a: SimpleNamespace(
            extracted=0, unchanged=0, skipped_no_messages=0, failed=0, redactions=0
        ),
    )

    from lore.services.improve import run_improve

    results = run_improve(
        tmp_path,
        provider_name="ollama",
        model=None,
        limit=1,
        stages=["extract_knowledge"],
    )

    assert len(seen) == 1
    assert seen[0]["allow_cloud"] is False
    assert results[0].status == "completed"


# --- gemini-cli title path ----------------------------------------------------


def _session():
    from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession

    turns = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"turn {i}: please rotate the key {TOKEN}" if i == 0 else f"turn {i}: ok",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(12)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="cloud-optin-0002",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project_path="/p",
        title="",
        messages=turns,
    )


@pytest.fixture()
def _title_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lore.titles.generator.OLLAMA_AVAILABLE", True)
    monkeypatch.setattr("lore.titles.generator.lore_home", lambda: tmp_path / "lore_home")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


def test_gemini_cli_title_without_optin_never_calls_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _title_env
) -> None:
    def _no_subprocess(*_a, **_k):
        raise AssertionError("subprocess must not run without cloud opt-in")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    generator = TitleGenerator(strategy=TitleStrategy.SMART, provider="gemini-cli")

    with pytest.raises(CloudOptInRequired):
        generator.generate(_session(), force=True)


def _install_subprocess_capture(monkeypatch: pytest.MonkeyPatch) -> list:
    seen: list = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="A Title")

    monkeypatch.setattr("subprocess.run", fake_run)
    return seen


def test_gemini_cli_title_env_optin_sends_redacted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _title_env
) -> None:
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    calls = _install_subprocess_capture(monkeypatch)
    generator = TitleGenerator(strategy=TitleStrategy.SMART, provider="gemini-cli")

    title = generator.generate(_session(), force=True)

    assert title == "A Title"
    assert len(calls) == 1
    assert calls[0][0][0] == "gemini"
    prompt = calls[0][1]["input"]
    assert TOKEN not in prompt
    assert redact(TOKEN).text in prompt or "[REDACTED:" in prompt


def test_gemini_cli_title_arg_optin_sends_redacted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _title_env
) -> None:
    calls = _install_subprocess_capture(monkeypatch)
    generator = TitleGenerator(
        strategy=TitleStrategy.SMART, provider="gemini-cli", allow_cloud=True
    )

    generator.generate(_session(), force=True)

    assert len(calls) == 1
    prompt = calls[0][1]["input"]
    assert TOKEN not in prompt


def test_local_title_provider_still_works_without_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _title_env
) -> None:
    import lore.titles.generator as gen_mod

    captured: list = []

    class _Module:
        def __init__(self, *a, **k):
            captured.append(1)

        def chat(self, **kwargs):
            return {"message": {"content": "Local Title"}}

    monkeypatch.setattr(gen_mod, "OLLAMA_AVAILABLE", True)
    monkeypatch.setattr(gen_mod, "ollama", SimpleNamespace(Client=_Module))
    generator = TitleGenerator(strategy=TitleStrategy.SMART, provider="local")

    assert generator.generate(_session(), force=True) == "Local Title"
    assert captured == [1]


# --- summarize service entry point -------------------------------------------


def test_run_summarize_command_gemini_without_optin_never_constructs_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from lore.services.summarize_cli import run_summarize_command

    def _boom(*_a, **_k):
        raise AssertionError("provider must not be constructed without cloud opt-in")

    monkeypatch.setattr("lore.llm.get_provider", _boom)
    # Candidate collection needs a store; the gate must fire before any provider work.
    monkeypatch.setattr(
        "lore.services.summarize_cli._collect_candidates",
        lambda _args, _out: (None, [SimpleNamespace(session_id="s1")]),
    )
    args = Namespace(
        output_dir=str(tmp_path),
        level="session",
        session=None,
        project=None,
        since=None,
        tool=None,
        include_automated=False,
        max_sessions=1,
        provider="gemini",
        model=None,
        dry_run=False,
        json=False,
        force=False,
        max_input_tokens=10**9,
        max_session_tokens=10**9,
        allow_cloud=False,
    )

    rc = run_summarize_command(args)

    assert rc == 2
    err = capsys.readouterr().err
    assert "--allow-cloud" in err
    assert "LORE_ALLOW_CLOUD" in err
