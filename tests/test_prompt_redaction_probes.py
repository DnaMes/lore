"""The FINISHED outgoing prompt is redacted, not only the conversation excerpt.

Codex round 4: a GitHub token in the project path stayed in the gemini-cli call and a
token in a session title reached the stats provider. Every provider-bound prompt must
pass through the redactor after it is fully assembled.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.llm.tasks.stats import StatsGenerator
from lore.titles.generator import TitleGenerator, TitleStrategy

# Synthetic value shaped like a GitHub token (matches the redactor's github_token rule).
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


def _session(project_path: str, title: str = "") -> UnifiedSession:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    turns = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"turn {i}: working on the parser",
            timestamp=when,
        )
        for i in range(12)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="redaction-probe",
        created_at=when,
        last_updated=when,
        project_path=project_path,
        title=title,
        messages=turns,
    )


def test_gemini_cli_title_prompt_redacts_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lore.titles.generator as gen_mod

    calls: List[List[str]] = []

    def fake_run(cmd: List[str], **_kw: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="A Short Title", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(gen_mod, "OLLAMA_AVAILABLE", True)
    gen = TitleGenerator(
        cache_dir=tmp_path / "title_cache",
        strategy=TitleStrategy.SMART,
        provider="gemini-cli",
        allow_cloud=True,
    )
    gen.generate(_session(f"/home/dev/{TOKEN}/repo"), force=True)

    assert calls, "the gemini CLI was never reached"
    assert all(TOKEN not in part for part in calls[0])


def test_ollama_title_prompt_redacts_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lore.titles.generator as gen_mod

    prompts: List[str] = []

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def chat(self, *, messages: List[dict], **_k: Any) -> dict:
            prompts.append(messages[0]["content"])
            return {"message": {"content": "A Short Title"}}

    monkeypatch.setattr(gen_mod, "OLLAMA_AVAILABLE", True)
    monkeypatch.setattr(gen_mod, "ollama", SimpleNamespace(Client=_Client), raising=False)
    gen = TitleGenerator(
        cache_dir=tmp_path / "title_cache", strategy=TitleStrategy.SMART, provider="local"
    )
    gen.generate(_session(f"/home/dev/{TOKEN}/repo"), force=True)

    assert prompts, "the local model was never reached"
    assert TOKEN not in prompts[0]


def test_stats_prompt_redacts_titles_and_project_paths(tmp_path: Path) -> None:
    sent: List[str] = []

    class _Provider:
        def is_available(self) -> bool:
            return True

        def generate(self, prompt: str, **_k: Any) -> SimpleNamespace:
            sent.append(prompt)
            return SimpleNamespace(content="{}")

    gen = StatsGenerator(provider=_Provider(), output_dir=tmp_path)  # type: ignore[arg-type]
    gen.generate_session_stats(
        [_session(f"/home/dev/{TOKEN}/repo", title=f"fix {TOKEN} leak")], use_llm=True
    )

    assert sent, "the provider was never reached"
    assert TOKEN not in sent[0]
    assert gen.redactions >= 1
