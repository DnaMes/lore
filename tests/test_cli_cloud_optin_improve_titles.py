"""Cloud opt-in gate for `lore improve` and `lore generate-titles`.

Both send session text to a provider, so every non-local provider path must
refuse (exit 2, naming --allow-cloud and LORE_ALLOW_CLOUD) until the user
opts in — the same shared helper as analyze/knowledge/format/summarize.
The default local title provider keeps working without opt-in (#185).
"""

from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.titles.generator import TitleGenerator


def _improve_args(tmp_path: Path, **overrides) -> Namespace:
    base = {
        "output_dir": str(tmp_path),
        "provider": "gemini",
        "model": None,
        "limit": 5,
        "stage": None,
        "dry_run": False,
        "include_automated": False,
        "json": False,
        "allow_cloud": False,
    }
    base.update(overrides)
    return Namespace(**base)


def _session() -> UnifiedSession:
    turns = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"turn {i}: fix the login button alignment please"
            if i == 0
            else f"turn {i}: ok",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(12)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="cloud-optin-0001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project_path="/p",
        title="",
        messages=turns,
    )


def _fake_extractors(monkeypatch, sessions) -> None:
    extractor = SimpleNamespace(
        tool=SimpleNamespace(value="claude-code"),
        is_available=lambda: True,
        extract_sessions=lambda: iter(sessions),
    )
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda *a, **k: [extractor])


def _titles_args(**overrides) -> Namespace:
    base = {
        "strategy": "auto",
        "tool": None,
        "since": None,
        "force": True,
        "provider": "gemini-cli",
        "model": None,
        "include_automated": False,
        "allow_cloud": False,
    }
    base.update(overrides)
    return Namespace(**base)


@pytest.fixture()
def title_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Count provider calls made through cmd_generate_titles; keep cache in tmp."""
    seen: dict[str, int] = {"gemini-cli": 0, "local": 0}
    monkeypatch.setattr(
        TitleGenerator,
        "_generate_llm_gemini",
        lambda self, session: (
            seen.__setitem__("gemini-cli", seen["gemini-cli"] + 1) or "Gemini Title"
        ),
    )
    monkeypatch.setattr(
        TitleGenerator,
        "_generate_llm_ollama",
        lambda self, session: seen.__setitem__("local", seen["local"] + 1) or "Ollama Title",
    )
    monkeypatch.setattr("lore.titles.generator.OLLAMA_AVAILABLE", True)
    monkeypatch.setattr("lore.titles.generator.lore_home", lambda: tmp_path / "lore_home")
    _fake_extractors(monkeypatch, [_session()])
    return seen


# --- improve ------------------------------------------------------------------


def test_improve_cloud_without_optin_exits_2_and_never_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    calls: list = []
    monkeypatch.setattr("lore.services.improve.run_improve", lambda *a, **k: calls.append(a) or [])

    rc = lore_cli.cmd_improve(_improve_args(tmp_path, provider="gemini"))

    assert rc == 2
    assert calls == []  # the pipeline (and provider) was never reached
    err = capsys.readouterr().err
    assert "--allow-cloud" in err
    assert "LORE_ALLOW_CLOUD" in err
    assert "gemini" in err


def test_improve_cloud_env_provider_also_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_LLM_PROVIDER", "openrouter")
    calls: list = []
    monkeypatch.setattr("lore.services.improve.run_improve", lambda *a, **k: calls.append(a) or [])

    assert lore_cli.cmd_improve(_improve_args(tmp_path, provider=None)) == 2
    assert calls == []


def test_improve_cloud_with_allow_cloud_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    calls: list = []
    monkeypatch.setattr("lore.services.improve.run_improve", lambda *a, **k: calls.append(a) or [])

    rc = lore_cli.cmd_improve(_improve_args(tmp_path, provider="gemini", allow_cloud=True))

    assert rc in (0, None)
    assert len(calls) == 1


def test_improve_cloud_with_env_optin_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    calls: list = []
    monkeypatch.setattr("lore.services.improve.run_improve", lambda *a, **k: calls.append(a) or [])

    rc = lore_cli.cmd_improve(_improve_args(tmp_path, provider="jev"))

    assert rc in (0, None)
    assert len(calls) == 1


def test_improve_local_ollama_needs_no_optin(tmp_path, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    calls: list = []
    monkeypatch.setattr("lore.services.improve.run_improve", lambda *a, **k: calls.append(a) or [])

    rc = lore_cli.cmd_improve(_improve_args(tmp_path, provider="ollama"))

    assert rc in (0, None)
    assert len(calls) == 1


def test_improve_dry_run_needs_no_optin(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_LLM_PROVIDER", raising=False)
    calls: list = []
    monkeypatch.setattr("lore.services.improve.run_improve", lambda *a, **k: calls.append(a) or [])

    rc = lore_cli.cmd_improve(_improve_args(tmp_path, provider="gemini", dry_run=True))

    assert rc in (0, None)  # a dry run contacts no provider
    assert len(calls) == 1


# --- generate-titles ----------------------------------------------------------


def test_titles_cloud_without_optin_exits_2_and_provider_not_called(
    tmp_path, monkeypatch, capsys, title_calls
):
    rc = lore_cli.cmd_generate_titles(_titles_args(provider="gemini-cli"))

    assert rc == 2
    assert title_calls == {"gemini-cli": 0, "local": 0}
    err = capsys.readouterr().err
    assert "--allow-cloud" in err
    assert "LORE_ALLOW_CLOUD" in err


def test_titles_cloud_with_allow_cloud_calls_provider(title_calls):
    rc = lore_cli.cmd_generate_titles(_titles_args(provider="gemini-cli", allow_cloud=True))

    assert rc in (0, None)
    assert title_calls == {"gemini-cli": 1, "local": 0}


def test_titles_cloud_with_env_optin_calls_provider(title_calls, monkeypatch):
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    rc = lore_cli.cmd_generate_titles(_titles_args(provider="gemini-cli"))

    assert rc in (0, None)
    assert title_calls == {"gemini-cli": 1, "local": 0}


def test_titles_default_local_works_without_optin(title_calls, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    rc = lore_cli.cmd_generate_titles(_titles_args(provider=None))

    assert rc in (0, None)
    assert title_calls == {"gemini-cli": 0, "local": 1}


# --- parser flags --------------------------------------------------------------


def test_allow_cloud_flag_registered_for_improve_and_titles():
    parser = lore_cli.build_parser()
    improve = parser.parse_args(["improve"])
    titles = parser.parse_args(["generate-titles"])
    assert improve.allow_cloud is False
    assert titles.allow_cloud is False
    assert parser.parse_args(["improve", "--allow-cloud"]).allow_cloud is True
    assert parser.parse_args(["generate-titles", "--allow-cloud"]).allow_cloud is True
