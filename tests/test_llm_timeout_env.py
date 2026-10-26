"""`LORE_LLM_TIMEOUT` overrides the default timeout of the hosted providers.

Jev delegate hops routinely take 9-180 s and OpenRouter free models can be slow,
while `LLMConfig.timeout` defaults to 60 s. Track L step 2 gives exactly the two
hosted providers a per-call override; gemini and ollama keep their defaults so a
local setup never changes behaviour because of a global env var.
"""

from __future__ import annotations

import pytest

from lore.llm.factory import get_provider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LORE_LLM_TIMEOUT", raising=False)
    # These tests construct cloud providers directly; opt in once for the suite.
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")


def test_jev_default_timeout_is_180() -> None:
    assert get_provider("jev").config.timeout == 180


def test_openrouter_default_timeout_is_90() -> None:
    assert get_provider("openrouter").config.timeout == 90


def test_env_timeout_is_honored_for_hosted_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_LLM_TIMEOUT", "42")
    assert get_provider("jev").config.timeout == 42
    assert get_provider("openrouter").config.timeout == 42


@pytest.mark.parametrize("bad", ["soon", "0", "-5", "1.5", " "])
def test_invalid_env_falls_back_to_default_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    monkeypatch.setenv("LORE_LLM_TIMEOUT", bad)
    assert get_provider("openrouter").config.timeout == 90
    assert get_provider("jev").config.timeout == 180
    err = capsys.readouterr().err
    assert "LORE_LLM_TIMEOUT" in err
    assert bad.strip() in err or bad in err  # the rejected value is named


def test_explicit_timeout_kwarg_beats_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_LLM_TIMEOUT", "42")
    assert get_provider("jev", timeout=7).config.timeout == 7


def test_local_providers_keep_their_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_LLM_TIMEOUT", "42")
    assert get_provider("gemini").config.timeout == 60
    assert get_provider("ollama").config.timeout == 60
