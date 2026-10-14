"""CLI tests for ``lore summarize`` (B4.4, #197)."""

from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import lore.llm
import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.llm.base import LLMConfig, LLMProvider, LLMResponse
from lore.services.summarize import SummarizeReport
from lore.storage import initialise
from lore.storage.writer import v2_db_path, write_sessions

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _idle_ts(hours_ago: float = 2.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


SENTINEL = "SENTINEL_TRANSCRIPT_BODY_DO_NOT_PRINT"
MODEL = "fake/test-model"


def _msg(role: Role, content: str, **kwargs: Any) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=T0, **kwargs)


def _session(sid: str, *contents: str, **kwargs: Any) -> UnifiedSession:
    msgs = [
        _msg(Role.USER if i % 2 == 0 else Role.ASSISTANT, text) for i, text in enumerate(contents)
    ]
    return UnifiedSession(
        tool=kwargs.get("tool", Tool.CLAUDE_CODE),
        session_id=sid,
        created_at=T0,
        last_updated=kwargs.get("last_updated", _idle_ts()),
        project_path=kwargs.get("project_path", "/proj"),
        origin=kwargs.get("origin", "interactive"),
        messages=msgs,
    )


def _rich_session(sid: str = "rich") -> UnifiedSession:
    chunks: list[str] = []
    for i in range(6):
        chunks.append(f"User goal paragraph {i} " + ("detail " * 80))
        chunks.append(
            "[Tool: Read] /src/module.py\n"
            "[Tool: Bash]\n```\npytest -q tests/\n```\n"
            f"[Tool Result]\n{SENTINEL}\n" + ("log " * 40)
        )
    return _session(sid, *chunks)


def _build_store(output_dir: Path, *sessions: UnifiedSession) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    initialise(v2_db_path(output_dir))
    write_sessions(v2_db_path(output_dir), sessions)


def _valid_summary(cite: int = 1) -> dict[str, Any]:
    return {
        "one_liner": "Shipped the feature",
        "goal": {"text": "build feature", "cites": [cite]},
        "outcome": {"status": "done", "text": "shipped", "cites": [cite]},
        "decisions": [],
        "open_questions": [],
        "next_steps": [],
        "topics": ["feature"],
        "confidence": 0.85,
    }


class FakeProvider(LLMProvider):
    def __init__(self, responses: Optional[List[str]] = None) -> None:
        super().__init__(LLMConfig.for_transcripts(provider="fake", model="test"))
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
        content = self.responses.pop(0) if self.responses else json.dumps(_valid_summary())
        return LLMResponse(content=content, model="test", tokens_used=120)

    def generate_batch(
        self, prompts: List[str], system_prompt: Optional[str] = None
    ) -> List[LLMResponse]:
        return [self.generate(p, system_prompt=system_prompt) for p in prompts]

    def is_available(self) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"model": "test"}


def _args(output_dir: Path, **overrides: Any) -> Namespace:
    base = {
        "output_dir": str(output_dir),
        "session": None,
        "project": None,
        "tool": None,
        "since": None,
        "provider": "ollama",
        "model": None,
        "allow_cloud": False,
        "dry_run": False,
        "force": False,
        "max_sessions": None,
        "max_input_tokens": None,
        "max_session_tokens": None,
        "level": "session",
        "include_automated": False,
        "json": False,
    }
    base.update(overrides)
    return Namespace(**base)


@pytest.fixture
def use_provider(monkeypatch: pytest.MonkeyPatch):
    def install(provider: FakeProvider) -> None:
        monkeypatch.setattr(lore.llm, "get_provider", lambda **kwargs: provider)

    return install


def test_dry_run_prints_estimates_without_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    monkeypatch.setattr(
        lore.llm,
        "get_provider",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )

    rc = lore_cli.cmd_summarize(_args(store, dry_run=True))
    assert rc == 0


def test_cloud_without_allow_cloud_exits_2(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    provider = FakeProvider()
    use_provider(provider)

    rc = lore_cli.cmd_summarize(_args(store, provider="gemini"))

    assert rc == 2
    assert provider.prompts == []


def test_cloud_with_allow_cloud_runs(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    secret_session = _rich_session()
    secret_session.messages.append(
        _msg(Role.USER, "my key is " + "sk-" + "a" * 24 + " please keep it safe " + "pad " * 80)
    )
    _build_store(store, secret_session)
    provider = FakeProvider()
    use_provider(provider)

    rc = lore_cli.cmd_summarize(_args(store, provider="gemini", allow_cloud=True))

    assert rc == 0
    assert provider.prompts
    assert all("sk-" + "a" * 24 not in prompt for prompt in provider.prompts)


def test_announcement_precedes_provider_call(tmp_path: Path, use_provider, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    seen: list[str] = []
    provider = FakeProvider()
    original = provider.generate

    def spy(prompt: str, *a: Any, **kw: Any) -> LLMResponse:
        seen.append(capsys.readouterr().out)
        return original(prompt, *a, **kw)

    provider.generate = spy  # type: ignore[method-assign]
    use_provider(provider)

    assert lore_cli.cmd_summarize(_args(store)) == 0
    assert "Provider: ollama/llama3.2 (local)" in seen[0]


def test_json_stdout_contains_report_keys(tmp_path: Path, use_provider, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    use_provider(FakeProvider())

    assert lore_cli.cmd_summarize(_args(store, json=True)) == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    for field in fields(SummarizeReport):
        assert field.name in payload
    assert {"provider", "model", "selected"} <= set(payload)


def test_level_day_exits_2(tmp_path: Path, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session())

    rc = lore_cli.cmd_summarize(_args(store, level="day"))

    assert rc == 2
    assert "not implemented yet" in capsys.readouterr().err


def test_unknown_session_exits_2(tmp_path: Path, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session("aaa111"))

    rc = lore_cli.cmd_summarize(_args(store, session="nope"))

    assert rc == 2
    assert "No session found" in capsys.readouterr().err


def test_ambiguous_session_prefix_exits_2(tmp_path: Path, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session("abc-one"), _rich_session("abc-two"))

    rc = lore_cli.cmd_summarize(_args(store, session="abc"))

    assert rc == 2
    assert "Ambiguous" in capsys.readouterr().err


def test_prefix_session_resolves(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    _build_store(store, _rich_session("unique-prefix-id"))
    provider = FakeProvider()
    use_provider(provider)

    rc = lore_cli.cmd_summarize(_args(store, session="unique-prefix"))

    assert rc == 0
    assert provider.prompts


def test_all_failed_exits_1(tmp_path: Path, use_provider, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    bad = json.dumps({**_valid_summary(), "one_liner": "x" * 200})
    use_provider(FakeProvider([bad, bad]))

    rc = lore_cli.cmd_summarize(_args(store, force=True))

    assert rc == 1
    assert "failed" in capsys.readouterr().err.lower()


def test_partial_failure_exits_0(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    good = _rich_session("good")
    good.last_updated = _idle_ts(1.5)
    bad = _rich_session("bad")
    bad.last_updated = _idle_ts(3)
    _build_store(store, good, bad)
    bad_json = json.dumps({**_valid_summary(), "one_liner": "x" * 200})
    good_json = json.dumps(_valid_summary())
    use_provider(FakeProvider([good_json, bad_json, bad_json]))

    assert lore_cli.cmd_summarize(_args(store, force=True)) == 0


def test_automated_excluded_without_include_automated(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    auto = _rich_session("auto")
    auto.origin = "exec"
    _build_store(store, auto)
    provider = FakeProvider()
    use_provider(provider)

    assert lore_cli.cmd_summarize(_args(store)) == 0
    assert provider.prompts == []


def test_automated_included_with_flag(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    auto = _rich_session("auto")
    auto.origin = "exec"
    _build_store(store, auto)
    provider = FakeProvider()
    use_provider(provider)

    assert lore_cli.cmd_summarize(_args(store, include_automated=True)) == 0
    assert provider.prompts


def test_tombstoned_never_sent(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    (store / "deleted_sessions.json").write_text(json.dumps({"session_ids": ["rich"]}))
    provider = FakeProvider()
    use_provider(provider)

    assert lore_cli.cmd_summarize(_args(store)) == 0
    assert provider.prompts == []


def test_max_sessions_honoured(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    _build_store(store, _rich_session("a"), _rich_session("b"))
    provider = FakeProvider()
    use_provider(provider)

    lore_cli.cmd_summarize(_args(store, max_sessions=1, force=True))

    assert len(provider.prompts) == 1


def test_max_input_tokens_honoured(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    _build_store(store, _rich_session("a"), _rich_session("b"))
    provider = FakeProvider()
    use_provider(provider)

    lore_cli.cmd_summarize(_args(store, max_sessions=2, max_input_tokens=1, force=True))

    assert len(provider.prompts) <= 1


def test_since_default_excludes_old_session(tmp_path: Path, use_provider):
    store = tmp_path / "out"
    old = _rich_session("old")
    old.last_updated = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _build_store(store, old)
    provider = FakeProvider()
    use_provider(provider)

    assert lore_cli.cmd_summarize(_args(store)) == 0
    assert provider.prompts == []


def test_output_contains_no_transcript_sentinel(tmp_path: Path, use_provider, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    use_provider(FakeProvider())

    lore_cli.cmd_summarize(_args(store))
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_empty_session_value_is_a_usage_error(tmp_path: Path, use_provider, capsys):
    store = tmp_path / "out"
    _build_store(store, _rich_session())
    provider = FakeProvider()
    use_provider(provider)

    rc = lore_cli.cmd_summarize(_args(store, session="   "))

    assert rc == 2
    assert provider.prompts == []


def test_missing_store_is_not_exit_code_1(tmp_path: Path, capsys):
    rc = lore_cli.cmd_summarize(_args(tmp_path / "does-not-exist"))

    assert rc == 2
