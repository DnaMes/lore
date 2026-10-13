"""`lore knowledge` must send the real conversation to the LLM (#197, task 1)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.llm.tasks.knowledge import KnowledgeEntry, KnowledgeExtractor

VALID_JSON = json.dumps(
    {
        "topic": "Vector storage",
        "key_points": ["use sqlite-vec"],
        "code_snippets": [],
        "decisions": ["sqlite-vec over faiss"],
        "tools_used": ["sqlite"],
    }
)


class FakeProvider(LLMProvider):
    """Records every prompt and replays a scripted list of responses."""

    def __init__(self, responses: Optional[List[str]] = None) -> None:
        super().__init__(LLMConfig(provider="fake", model="fake-model"))
        self.prompts: List[str] = []
        self.responses = list(responses or [])

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        content = self.responses.pop(0) if self.responses else VALID_JSON
        return LLMResponse(content=content, model="fake-model")

    def generate_batch(
        self, prompts: List[str], system_prompt: Optional[str] = None
    ) -> List[LLMResponse]:
        return [self.generate(p) for p in prompts]

    def is_available(self) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"model": "fake-model"}


def _session(contents: List[str], sid: str = "s1") -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        messages=[
            UnifiedMessage(
                role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
                content=text,
                timestamp=datetime(2026, 1, 1),
            )
            for i, text in enumerate(contents)
        ],
    )


def test_prompt_contains_message_text(tmp_path):
    provider = FakeProvider()
    extractor = KnowledgeExtractor(provider, tmp_path / "knowledge")

    entry = extractor.extract_from_session(_session(["use sqlite-vec for vectors", "ok"]))

    assert entry is not None
    assert len(provider.prompts) == 1
    assert "use sqlite-vec for vectors" in provider.prompts[0]


def test_conversation_text_caps_messages_and_chars(tmp_path):
    extractor = KnowledgeExtractor(FakeProvider(), tmp_path / "knowledge")
    session = _session(["x" * 600 for _ in range(30)])

    lines = extractor.conversation_text(session).split("\n")

    assert len(lines) == 20
    for line in lines:
        role, _, content = line.partition(": ")
        assert role in {"USER", "ASSISTANT"}
        assert len(content) <= 500


def test_entry_roundtrip():
    entry = KnowledgeEntry(
        session_id="s1",
        topic="T",
        key_points=["a", "b"],
        code_snippets=["c"],
        decisions=["d"],
        tools_used=["e"],
        project="/p",
        timestamp=datetime(2026, 3, 4, 5, 6, 7),
    )

    assert KnowledgeEntry.from_dict(entry.to_dict()).to_dict() == entry.to_dict()


def test_entry_from_dict_defaults_missing_keys():
    entry = KnowledgeEntry.from_dict({"session_id": "s1", "timestamp": "garbage"})

    assert entry.session_id == "s1"
    assert entry.key_points == []
    assert isinstance(entry.timestamp, datetime)
