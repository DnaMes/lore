"""LLM provider privacy: no transcript disk cache, Ollama num_ctx, prompt redaction."""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse, prepare_prompt
from lore.llm.gemini import GeminiProvider
from lore.llm.ollama import OllamaProvider, resolve_ollama_num_ctx
from lore.llm.tasks.knowledge import KnowledgeExtractor
from lore.llm.tasks.stats import StatsGenerator

VALID_JSON = json.dumps(
    {
        "topic": "T",
        "key_points": ["a"],
        "code_snippets": [],
        "decisions": [],
        "tools_used": [],
    }
)

INSIGHTS_JSON = json.dumps(
    {
        "patterns": "ok",
        "productivity": "ok",
        "recommendations": [],
        "trends": [],
    }
)


def _gemini_client_mock(captured: Dict[str, Any]) -> Any:
    class _Usage:
        total_token_count = 1

    class _Response:
        text = INSIGHTS_JSON
        usage_metadata = _Usage()

    class _GenerationConfig:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class _GenerativeModel:
        def __init__(self, model: str, system_instruction: Optional[str] = None) -> None:
            captured["system_instruction"] = system_instruction

        def generate_content(self, prompt: str, generation_config: Any = None) -> _Response:
            captured["prompt"] = prompt
            return _Response()

    class _Types:
        GenerationConfig = _GenerationConfig

    class _Genai:
        types = _Types()
        GenerativeModel = _GenerativeModel

    return _Genai()


def _session_with_secret() -> UnifiedSession:
    secret = "sk-" + "a" * 24
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="s1",
        created_at=__import__("datetime").datetime(2026, 1, 1),
        last_updated=__import__("datetime").datetime(2026, 1, 2),
        project_path="/p",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content=f"my key is {secret}",
                timestamp=__import__("datetime").datetime(2026, 1, 1),
            )
        ],
    )


@pytest.fixture
def lore_tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_for_transcripts_disables_cache():
    cfg = LLMConfig.for_transcripts(provider="gemini", cache_enabled=True)
    assert cfg.cache_enabled is False


def test_gemini_transcript_task_no_cache_dir(lore_tmp_home, monkeypatch):
    cache_root = lore_tmp_home / "llm_cache"
    captured: Dict[str, Any] = {}
    config = LLMConfig.for_transcripts(
        provider="gemini",
        model="gemini-2.0-flash",
        cache_dir=str(cache_root),
    )
    provider = GeminiProvider(config, allow_cloud=True)
    monkeypatch.setattr(provider, "_get_client", lambda: _gemini_client_mock(captured))

    provider.generate("USER: hello from a transcript")

    assert not cache_root.exists()


def test_ollama_transcript_task_no_cache_dir(lore_tmp_home, monkeypatch):
    cache_root = lore_tmp_home / "llm_cache"
    calls: List[Dict[str, Any]] = []

    def chat(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {"message": {"content": VALID_JSON}, "eval_count": 1, "prompt_eval_count": 1}

    fake_ollama = types.SimpleNamespace(chat=chat)
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)

    config = LLMConfig.for_transcripts(
        provider="ollama",
        model="llama3.2",
        cache_dir=str(cache_root),
    )
    provider = OllamaProvider(config)
    provider.generate("USER: transcript text")

    assert not cache_root.exists()
    assert calls


def test_non_transcript_task_still_caches(lore_tmp_home, monkeypatch):
    cache_root = lore_tmp_home / "llm_cache"
    captured: Dict[str, Any] = {}
    config = LLMConfig(
        provider="gemini",
        model="gemini-2.0-flash",
        cache_enabled=True,
        cache_dir=str(cache_root),
    )
    provider = GeminiProvider(config, allow_cloud=True)
    monkeypatch.setattr(provider, "_get_client", lambda: _gemini_client_mock(captured))

    stats = StatsGenerator(provider)
    stats._generate_llm_insights({"total_sessions": 1}, [])

    assert cache_root.is_dir()
    assert list(cache_root.glob("*.json"))


def test_ollama_num_ctx_default(lore_tmp_home, monkeypatch):
    monkeypatch.delenv("LORE_OLLAMA_NUM_CTX", raising=False)
    assert resolve_ollama_num_ctx(LLMConfig()) == 8192


def test_ollama_num_ctx_from_config(lore_tmp_home, monkeypatch):
    monkeypatch.delenv("LORE_OLLAMA_NUM_CTX", raising=False)
    assert resolve_ollama_num_ctx(LLMConfig(num_ctx=12000)) == 12000


def test_ollama_num_ctx_from_env(lore_tmp_home, monkeypatch):
    monkeypatch.setenv("LORE_OLLAMA_NUM_CTX", "10000")
    assert resolve_ollama_num_ctx(LLMConfig()) == 10000


def test_ollama_num_ctx_clamped(lore_tmp_home, monkeypatch):
    monkeypatch.delenv("LORE_OLLAMA_NUM_CTX", raising=False)
    assert resolve_ollama_num_ctx(LLMConfig(num_ctx=999)) == 2048
    assert resolve_ollama_num_ctx(LLMConfig(num_ctx=200000)) == 131072


def test_ollama_num_ctx_invalid_env_ignored(lore_tmp_home, monkeypatch):
    monkeypatch.setenv("LORE_OLLAMA_NUM_CTX", "not-a-number")
    assert resolve_ollama_num_ctx(LLMConfig()) == 8192


def test_ollama_options_passthrough_and_temperature_wins(lore_tmp_home, monkeypatch):
    calls: List[Dict[str, Any]] = []

    def chat(**kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        return {"message": {"content": "{}"}, "eval_count": 0, "prompt_eval_count": 0}

    monkeypatch.setitem(sys.modules, "ollama", types.SimpleNamespace(chat=chat))
    config = LLMConfig(
        provider="ollama",
        model="m",
        options={"top_k": 40, "temperature": 0.1},
        cache_enabled=False,
    )
    provider = OllamaProvider(config)
    provider.generate("p", temperature=0.9)

    opts = calls[0]["options"]
    assert opts["num_ctx"] == 8192
    assert opts["top_k"] == 40
    assert opts["temperature"] == 0.9


def test_knowledge_extractor_redacts_secret_in_prompt(lore_tmp_home):
    secret = "sk-" + "a" * 24
    session = _session_with_secret()
    prompts: List[str] = []

    class _Prov(LLMProvider):
        def __init__(self) -> None:
            super().__init__(LLMConfig.for_transcripts(provider="fake"))

        def generate(self, prompt, system_prompt=None, **kw):  # type: ignore[no-untyped-def]
            prompts.append(prompt)
            if system_prompt:
                prompts.append(f"SYS:{system_prompt}")
            return LLMResponse(content=VALID_JSON, model="m")

        def generate_batch(self, prompts, system_prompt=None):  # type: ignore[no-untyped-def]
            return [self.generate(p, system_prompt) for p in prompts]

        def is_available(self) -> bool:
            return True

        def get_model_info(self) -> Dict[str, Any]:
            return {}

    provider = _Prov()
    extractor = KnowledgeExtractor(provider, lore_tmp_home / "knowledge")
    entry = extractor.extract_from_session(session)

    assert entry is not None
    assert entry.redactions >= 1
    assert "[REDACTED:" in prompts[0]
    assert secret not in prompts[0]
    assert "Extract structured knowledge" in prompts[0]


def test_knowledge_extractor_no_secrets_unchanged_prompt(lore_tmp_home):
    session = UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id="s2",
        created_at=__import__("datetime").datetime(2026, 1, 1),
        last_updated=__import__("datetime").datetime(2026, 1, 2),
        project_path="/p",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="plain sqlite discussion",
                timestamp=__import__("datetime").datetime(2026, 1, 1),
            )
        ],
    )
    raw = KnowledgeExtractor.build_prompt(session)
    prompts: List[str] = []

    class _Prov(LLMProvider):
        def __init__(self) -> None:
            super().__init__(LLMConfig.for_transcripts(provider="fake"))

        def generate(self, prompt, system_prompt=None, **kw):  # type: ignore[no-untyped-def]
            prompts.append(prompt)
            return LLMResponse(content=VALID_JSON, model="m")

        def generate_batch(self, prompts, system_prompt=None):  # type: ignore[no-untyped-def]
            return [self.generate(p, system_prompt) for p in prompts]

        def is_available(self) -> bool:
            return True

        def get_model_info(self) -> Dict[str, Any]:
            return {}

    extractor = KnowledgeExtractor(_Prov(), lore_tmp_home / "knowledge")
    entry = extractor.extract_from_session(session)

    assert entry is not None
    assert entry.redactions == 0
    assert prompts[0] == raw


def test_prepare_prompt():
    secret = "sk-" + "b" * 24
    text, count = prepare_prompt(f"token {secret} here")
    assert count >= 1
    assert secret not in text
    assert "[REDACTED:" in text


def test_tasks_force_cache_off_on_a_cache_enabled_provider(tmp_path):
    from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
    from lore.llm.tasks.format import SessionFormatter
    from lore.llm.tasks.knowledge import KnowledgeExtractor

    class Fake(LLMProvider):
        def generate(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
            return LLMResponse(content="{}", model="fake")

        def generate_batch(self, prompts, system_prompt=None):
            return []

        def is_available(self):
            return True

        def get_model_info(self):
            return {}

    for task_cls in (KnowledgeExtractor, SessionFormatter):
        provider = Fake(LLMConfig(cache_enabled=True))
        task_cls(provider, output_dir=tmp_path / task_cls.__name__)
        assert provider.config.cache_enabled is False


def test_llm_config_positional_oauth_arguments_keep_their_meaning():
    from lore.llm.base import LLMConfig

    config = LLMConfig("gemini", "m", "key", None, 4096, 0.7, 60, True, None, "cid", "csecret")
    assert config.oauth_client_id == "cid"
    assert config.oauth_client_secret == "csecret"
    assert config.options == {} and config.num_ctx is None
