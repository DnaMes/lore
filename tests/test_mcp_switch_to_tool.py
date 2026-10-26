"""MCP tool switch_to_tool launches the installed ``lore-session`` script."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from lore.interfaces import mcp
from lore.interfaces.mcp_tools import sessions

MISSING_MSG = (
    "lore-session command not found. "
    "Install it with: pipx install git+https://github.com/DnaMes/lore"
)


def _switch(arguments: dict) -> str:
    server = mcp.create_server()
    handler = server.tools["switch_to_tool"]["handler"]
    return asyncio.run(handler(arguments))


def _completed(stdout: str = "ctx") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_switch_to_tool_invokes_lore_session_when_installed(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []

    def fake_which(name: str) -> str | None:
        calls.append({"which": name})
        return "/usr/local/bin/lore-session"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append({"run": cmd})
        return _completed("context body")

    monkeypatch.setattr(sessions.shutil, "which", fake_which)
    monkeypatch.setattr(sessions.subprocess, "run", fake_run)

    result = _switch({"tool": "gemini", "max_messages": 5})

    assert calls[0] == {"which": "lore-session"}
    run_cmd = calls[1]["run"]
    assert run_cmd[0] == "lore-session"
    assert run_cmd[1] == "switch"
    assert "--messages" in run_cmd and "5" in run_cmd
    assert "context body" in result


def test_switch_to_tool_reports_missing_lore_session(monkeypatch: pytest.MonkeyPatch):
    run_calls: list[list[str]] = []

    monkeypatch.setattr(sessions.shutil, "which", lambda name: None)

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        run_calls.append(cmd)
        return _completed()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)

    result = _switch({"tool": "gemini"})

    assert result == MISSING_MSG
    assert "lore-session" in result
    assert "pipx install git+https://github.com/DnaMes/lore" in result
    assert run_calls == []
