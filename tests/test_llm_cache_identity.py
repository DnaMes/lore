"""Cache keys cover the whole request, not only its text (#186).

A cached title or analysis was keyed on `system|prompt` (plus the model for some
providers), so a changed prompt version, task, sampling parameter or source
revision could be served a stale artifact — and two providers could collide. The
key is now built from one canonical identity, and every entry records that
identity so an old result stays auditable.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.llm.gemini import GeminiProvider
from lore.llm.jev import JevProvider
from lore.llm.ollama import OllamaProvider
from lore.llm.openrouter import OpenRouterProvider

PROMPT = "Summmarise this session: the build broke on a float version."
SYSTEM = "You write short titles."


def _gemini(tmp_path: Path, **overrides) -> GeminiProvider:
    return GeminiProvider(LLMConfig(provider="gemini", cache_dir=str(tmp_path), **overrides))


def _key(provider: LLMProvider, **overrides) -> str:
    for name, value in overrides.items():
        setattr(provider.config, name, value)
    return provider._get_cache_key(PROMPT, SYSTEM)


# ---------------------------------------------------------------------------
# identity composition
# ---------------------------------------------------------------------------


def test_the_same_request_always_has_the_same_key(tmp_path):
    provider = _gemini(tmp_path)
    assert provider._get_cache_key(PROMPT, SYSTEM) == provider._get_cache_key(PROMPT, SYSTEM)


@pytest.mark.parametrize(
    "override",
    [
        {"model": "other-model"},
        {"task": "title"},
        {"prompt_version": "v2"},
        {"cache_scope": "revision:abc123"},
        {"temperature": 0.1},
        {"max_tokens": 512},
        {"num_ctx": 2048},
        {"options": {"seed": 7}},
        {"base_url": "http://127.0.0.1:11434"},
    ],
)
def test_changing_any_request_component_changes_the_key(tmp_path, override):
    base = _gemini(tmp_path)._get_cache_key(PROMPT, SYSTEM)
    changed = _gemini(tmp_path, **override)._get_cache_key(PROMPT, SYSTEM)
    assert base != changed, f"{override} did not change the cache key"


def test_the_prompt_and_the_system_prompt_are_part_of_the_key(tmp_path):
    key = _gemini(tmp_path)._get_cache_key(PROMPT, SYSTEM)
    assert _gemini(tmp_path)._get_cache_key(PROMPT + " extra", SYSTEM) != key
    assert _gemini(tmp_path)._get_cache_key(PROMPT, SYSTEM + " extra") != key
    assert _gemini(tmp_path)._get_cache_key(PROMPT, None) != key


def test_a_separator_inside_the_prompt_cannot_collide(tmp_path):
    a = _gemini(tmp_path)._get_cache_key("a|b", "c")
    b = _gemini(tmp_path)._get_cache_key("a", "b|c")
    assert a != b


def test_the_identity_is_readable_and_canonical():
    provider = _gemini(Path("/tmp"), task="summary", prompt_version="v3", cache_scope="s1@r2")
    identity = provider.cache_identity(PROMPT, SYSTEM)
    assert identity["task"] == "summary"
    assert identity["prompt_version"] == "v3"
    assert identity["cache_scope"] == "s1@r2"
    assert identity["provider"] == "GeminiProvider"
    assert identity["model"] == provider.config.model
    # No raw prompt text: only fingerprints, so the key stays bounded.
    serialized = json.dumps(identity, sort_keys=True)
    assert PROMPT not in serialized
    assert json.dumps(identity, sort_keys=True) == json.dumps(
        provider.cache_identity(PROMPT, SYSTEM), sort_keys=True
    )


def test_providers_do_not_share_a_key_for_the_same_text(tmp_path):
    gemini = GeminiProvider(LLMConfig(provider="gemini", cache_dir=str(tmp_path)))
    ollama = OllamaProvider(LLMConfig(provider="ollama", cache_dir=str(tmp_path)))
    assert gemini._get_cache_key(PROMPT, SYSTEM) != ollama._get_cache_key(PROMPT, SYSTEM)


def test_jev_includes_the_lane_list(tmp_path):
    first = JevProvider(
        LLMConfig(provider="jev", cache_dir=str(tmp_path), options={"lanes": ["local"]})
    )
    second = JevProvider(
        LLMConfig(
            provider="jev", cache_dir=str(tmp_path), options={"lanes": ["local", "fastcheap"]}
        )
    )
    assert first._get_cache_key(PROMPT, SYSTEM) != second._get_cache_key(PROMPT, SYSTEM)


def test_openrouter_uses_the_resolved_request_values(tmp_path):
    config = LLMConfig(provider="openrouter", cache_dir=str(tmp_path), model="openai/gpt-x")
    provider = OpenRouterProvider(config)
    base = provider._get_cache_key(PROMPT, SYSTEM, 100, 0.2, "https://openrouter.ai/api/v1")
    assert base != provider._get_cache_key(PROMPT, SYSTEM, 200, 0.2, "https://openrouter.ai/api/v1")
    assert base != provider._get_cache_key(PROMPT, SYSTEM, 100, 0.9, "https://openrouter.ai/api/v1")
    assert base != provider._get_cache_key(PROMPT, SYSTEM, 100, 0.2, "http://127.0.0.1:9")


# ---------------------------------------------------------------------------
# stored entries carry their request metadata
# ---------------------------------------------------------------------------


def test_a_cache_entry_records_the_request_it_belongs_to(tmp_path):
    provider = _gemini(tmp_path, task="title", prompt_version="v4")
    provider._ensure_cache_dir()
    identity = provider.cache_identity(PROMPT, SYSTEM)
    key = provider._get_cache_key(PROMPT, SYSTEM)
    provider._save_to_cache(
        key, LLMResponse(content="A short title", model="m", tokens_used=3), identity
    )
    stored = json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))
    assert stored["content"] == "A short title"
    assert stored["request"]["task"] == "title"
    assert stored["request"]["prompt_version"] == "v4"
    assert stored["request"]["cache_scope"] == ""
    assert provider._load_from_cache(key).cached is True


def test_an_old_entry_does_not_answer_a_new_request(tmp_path):
    provider = _gemini(tmp_path, prompt_version="v1")
    identity = provider.cache_identity(PROMPT, SYSTEM)
    old_key = provider._get_cache_key(PROMPT, SYSTEM)
    provider._save_to_cache(
        old_key, LLMResponse(content="stale", model="m", tokens_used=1), identity
    )
    provider.config = replace(provider.config, prompt_version="v2")
    new_key = provider._get_cache_key(PROMPT, SYSTEM)
    assert new_key != old_key
    assert provider._load_from_cache(new_key) is None
    # The old artifact is still readable with its own metadata.
    assert provider._load_from_cache(old_key).content == "stale"
    assert (
        json.loads((tmp_path / f"{old_key}.json").read_text())["request"]["prompt_version"] == "v1"
    )


def test_a_stored_entry_does_not_break_a_provider_without_identity(tmp_path):
    """Loading tolerates entries written before the identity field existed."""
    provider = _gemini(tmp_path)
    provider._ensure_cache_dir()
    key = provider._get_cache_key(PROMPT, SYSTEM)
    (tmp_path / f"{key}.json").write_text(
        json.dumps({"content": "legacy", "model": "m", "tokens_used": 0}), encoding="utf-8"
    )
    assert provider._load_from_cache(key).content == "legacy"


def test_concurrent_writes_never_expose_a_partial_entry(tmp_path):
    """Atomic replace: a reader sees either the old file or the new one (#186)."""
    provider = _gemini(tmp_path)
    provider._ensure_cache_dir()
    key = provider._get_cache_key(PROMPT, SYSTEM)
    identity = provider.cache_identity(PROMPT, SYSTEM)

    first = LLMResponse(content="first", model="m", tokens_used=1)
    second = LLMResponse(content="second", model="m", tokens_used=2)
    provider._save_to_cache(key, first, identity)

    # A temp file that a crashed writer leaves behind must not be mistaken for an
    # entry, and the entry itself is replaced in one step.
    (tmp_path / f".{key}.deadbeef.tmp").write_text('{"content": "partial"', encoding="utf-8")
    provider._save_to_cache(key, second, identity)

    stored = json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))
    assert stored["content"] == "second"
    assert provider._load_from_cache(key).content == "second"
    assert not list(tmp_path.glob(".*.tmp")) or all(
        path.stat().st_size >= 0 for path in tmp_path.glob(".*.tmp")
    )


def test_two_providers_writing_the_same_directory_stay_separate(tmp_path):
    gemini = GeminiProvider(LLMConfig(provider="gemini", cache_dir=str(tmp_path)))
    ollama = OllamaProvider(LLMConfig(provider="ollama", cache_dir=str(tmp_path)))
    for provider, content in ((gemini, "gemini answer"), (ollama, "ollama answer")):
        identity = provider.cache_identity(PROMPT, SYSTEM)
        key = provider._get_cache_key(PROMPT, SYSTEM)
        provider._save_to_cache(
            key, LLMResponse(content=content, model="m", tokens_used=0), identity
        )

    assert gemini._load_from_cache(gemini._get_cache_key(PROMPT, SYSTEM)).content == "gemini answer"
    assert ollama._load_from_cache(ollama._get_cache_key(PROMPT, SYSTEM)).content == "ollama answer"
