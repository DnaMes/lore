"""Jev delegate provider: text-only lanes, no real broker calls in unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import lore_cli
from lore.llm.base import LLMConfig
from lore.llm.jev import JevError, JevProvider, validate_jev_lanes
from lore.services.summarize_cli import default_model, provider_locality


def _config(**overrides: Any) -> LLMConfig:
    return LLMConfig(provider="jev", model="local,fastcheap", cache_enabled=False, **overrides)


def test_non_allowlisted_lane_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for lane in ("auto", "bulk", "antigravity-gemini"):
        monkeypatch.setenv("LORE_JEV_LANES", lane)
        with pytest.raises(ValueError, match="disallowed jev lane"):
            JevProvider(_config())


def test_validate_jev_lanes_lists_allowed() -> None:
    with pytest.raises(ValueError, match="allowed: fastcheap, local"):
        validate_jev_lanes(["explore"])


def test_is_available_follows_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lore.llm.jev.shutil.which", lambda _: None)
    assert JevProvider(_config()).is_available() is False
    monkeypatch.setattr(
        "lore.llm.jev.shutil.which", lambda name: "/bin/delegate" if name == "delegate" else None
    )
    assert JevProvider(_config()).is_available() is True


def test_provider_locality_jev_is_cloud() -> None:
    assert provider_locality("jev") == "cloud"


def test_default_model_jev() -> None:
    assert default_model("jev") == "local,fastcheap"


def test_subprocess_stdin_argv_and_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_JEV_LANES", "local")
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        cwd = Path(kwargs["cwd"])
        captured["cwd_empty"] = cwd.is_dir() and list(cwd.iterdir()) == []
        return subprocess.CompletedProcess(cmd, 0, stdout="answer\n", stderr="")

    monkeypatch.setattr("lore.llm.jev.subprocess.run", fake_run)
    provider = JevProvider(_config())
    result = provider.generate("user bit", system_prompt="system bit")

    assert result.content == "answer"
    assert captured["cmd"] == ["delegate", "--type", "local"]
    assert captured["kwargs"]["input"] == "system bit\n\nuser bit"
    assert captured["kwargs"].get("shell") is not True
    assert captured["cwd_empty"] is True


def test_exit_3_falls_through_to_second_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_JEV_LANES", "local,fastcheap")
    calls: list[str] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        lane = cmd[2]
        calls.append(lane)
        if lane == "local":
            return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="quota")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("lore.llm.jev.subprocess.run", fake_run)
    result = JevProvider(_config()).generate("hi")

    assert calls == ["local", "fastcheap"]
    assert result.content == "ok"


def test_all_lanes_fail_raises_jev_error_without_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "UNTRUSTED_TRANSCRIPT_SNIPPET_XYZ"
    monkeypatch.setenv("LORE_JEV_LANES", "local,fastcheap")

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("lore.llm.jev.subprocess.run", fake_run)
    with pytest.raises(JevError) as exc:
        JevProvider(_config()).generate(secret)

    msg = str(exc.value)
    assert secret not in msg
    assert "local" in msg and "fastcheap" in msg


def test_empty_stdout_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LORE_JEV_LANES", "local")

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 0, stdout="   \n", stderr="")

    monkeypatch.setattr("lore.llm.jev.subprocess.run", fake_run)
    with pytest.raises(JevError, match="empty response"):
        JevProvider(_config()).generate("x")


def test_summarize_jev_without_allow_cloud_exits_2(tmp_path: Path, capsys) -> None:
    from tests.test_cli_summarize import _args, _build_store, _rich_session

    store = tmp_path / "out"
    _build_store(store, _rich_session())
    rc = lore_cli.cmd_summarize(_args(store, provider="jev"))
    assert rc == 2
    assert "--allow-cloud" in capsys.readouterr().err
