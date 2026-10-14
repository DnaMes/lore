"""Title generation routes through the *selected* provider and records the call (#185).

Titles send a transcript excerpt, so the old implicit chain (Gemini CLI whenever
its binary existed, then ollama, then silent fallthrough) could push session text
to a provider nobody chose. The provider is now explicit, a failure never
switches to another provider, and every generated title records provider, model,
prompt version, a bounded excerpt and the status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.titles.generator import (
    TITLE_PROMPT_VERSION,
    TitleGenerator,
    TitleStrategy,
)


def _session(*, messages: int = 12) -> UnifiedSession:
    turns = [
        UnifiedMessage(
            role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
            content=f"turn {i}: fix the login button alignment please"
            if i == 0
            else f"turn {i}: ok",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(messages)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="title-provider-0001",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project_path="/p",
        title="",
        messages=turns,
    )


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Record which provider methods were asked for a title."""
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
    return seen


def _generator(tmp_path: Path, **kwargs) -> TitleGenerator:
    gen = TitleGenerator(cache_dir=tmp_path / "title_cache", **kwargs)
    gen._yake = None  # keep the fallback chain deterministic
    return gen


def test_default_provider_is_local_even_when_a_cloud_cli_exists(tmp_path, calls):
    gen = _generator(tmp_path, strategy=TitleStrategy.AUTO)

    assert gen.generate(_session()) == "Ollama Title"
    assert calls == {"gemini-cli": 0, "local": 1}


def test_explicit_cloud_provider_is_used_and_recorded(tmp_path, calls):
    gen = _generator(tmp_path, strategy=TitleStrategy.AUTO, provider="gemini-cli")

    assert gen.generate(_session()) == "Gemini Title"
    assert calls == {"gemini-cli": 1, "local": 0}


def test_a_failing_provider_never_switches_to_another_one(tmp_path, calls, monkeypatch):
    # The selected provider is asked, answers nothing, and the local *provider*
    # must never be tried instead — only the local strategies.
    monkeypatch.setattr(
        TitleGenerator,
        "_generate_llm_gemini",
        lambda self, session: calls.__setitem__("gemini-cli", calls["gemini-cli"] + 1),
    )
    gen = _generator(tmp_path, strategy=TitleStrategy.AUTO, provider="gemini-cli")

    title = gen.generate(_session())

    assert calls == {"gemini-cli": 1, "local": 0}  # the local provider was never tried
    assert title  # fell back to a local *strategy*, not another provider
    record = gen._cache["title-provider-0001"]["record"]
    assert record["status"] == "first_message"
    assert record["llm_failed"]["status"] == "llm-failed"
    assert record["llm_failed"]["provider"] == "gemini-cli"


def test_llm_record_carries_provider_model_prompt_version_and_excerpt(tmp_path, calls):
    gen = _generator(tmp_path, strategy=TitleStrategy.AUTO, provider="local", model="phi3:mini")

    gen.generate(_session())

    record = gen._cache["title-provider-0001"]["record"]
    assert record["provider"] == "local"
    assert record["model"] == "phi3:mini"
    assert record["prompt_version"] == TITLE_PROMPT_VERSION
    assert record["status"] == "llm"
    assert record["excerpt"].startswith("USER:")
    assert len(record["excerpt"]) <= 120


def test_fast_strategy_never_calls_an_llm(tmp_path, calls):
    gen = _generator(tmp_path, strategy=TitleStrategy.FAST)

    title = gen.generate(_session())

    assert calls == {"gemini-cli": 0, "local": 0}
    assert "login button alignment" in title  # first-message title, as before
    assert gen._cache["title-provider-0001"]["record"]["status"] == "first_message"
    assert gen._cache["title-provider-0001"]["record"]["provider"] == "none"


def test_native_title_is_recorded_without_a_provider(tmp_path, calls):
    gen = _generator(tmp_path, strategy=TitleStrategy.FAST)
    session = _session()
    session.title = "Tool Provided Title"

    assert gen.generate(session) == "Tool Provided Title"
    assert calls == {"gemini-cli": 0, "local": 0}
    record = gen._cache["title-provider-0001"]["record"]
    assert record["status"] == "native"
    assert record["provider"] == "none"


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown title provider"):
        TitleGenerator(cache_dir=tmp_path / "title_cache", provider="cloudy-model")


def test_title_cache_file_is_owner_only(tmp_path, calls):
    gen = _generator(tmp_path, strategy=TitleStrategy.FAST)
    gen.generate(_session())

    cache_file = tmp_path / "title_cache" / "titles.json"
    assert cache_file.exists()
    assert cache_file.stat().st_mode & 0o777 == 0o600  # it can hold an excerpt
