"""Privacy tests for the session handoff (lore-session switch/continue).

Covers the release-review blockers:
- cloud gate: gemini/codex refuse without ``--allow-cloud``/``LORE_ALLOW_CLOUD=1``;
- redaction: secret-shaped strings never reach argv or the context file;
- protected context file: unique name, 0600, inside a private handoff directory,
  removed after launched runs, kept for manual tools, stale files cleaned up.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import lore_session_cli

# Same synthetic shape as tests/test_prompt_redaction_probes.py (github_token rule).
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
CONTEXT = f"# Session\n\ngithub token: {TOKEN}\n"
NAME_RE = re.compile(r"^context-[0-9a-f]{32}\.md$")

GEMINI_REFUSAL = (
    "Error: switching to gemini sends the session context to the cloud. "
    "Pass --allow-cloud or set LORE_ALLOW_CLOUD=1 to confirm."
)
CODEX_REFUSAL = (
    "Error: switching to codex sends the session context to the cloud. "
    "Pass --allow-cloud or set LORE_ALLOW_CLOUD=1 to confirm."
)


def _handoff_dir(home) -> str:
    return home / ".lore" / "handoff"


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Point HOME/USERPROFILE at tmp_path so lore_home() is isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("LORE_ALLOW_CLOUD", raising=False)
    return tmp_path


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch):
    """Mock subprocess.run and the executable lookup; record argv calls."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(lore_session_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        lore_session_cli,
        "get_safe_executable",
        lambda tool: f"/fake/bin/{tool}",
    )
    return calls


def test_gemini_and_codex_refused_without_opt_in(isolated_home, runs, capsys):
    for tool, refusal in (("gemini", GEMINI_REFUSAL), ("codex", CODEX_REFUSAL)):
        assert lore_session_cli.switch_to_tool(tool, CONTEXT) is False
        assert runs == []
        stderr = capsys.readouterr().err
        assert refusal in stderr
    handoff = _handoff_dir(isolated_home)
    assert not handoff.exists() or list(handoff.glob("h-*/context-*.md")) == []


def test_gemini_allowed_with_allow_cloud_flag(isolated_home, runs):
    assert lore_session_cli.switch_to_tool("gemini", CONTEXT, allow_cloud=True) is True
    assert len(runs) == 1
    assert runs[0][0] == "/fake/bin/gemini"


def test_gemini_allowed_with_lore_allow_cloud_env(monkeypatch, isolated_home, runs):
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    assert lore_session_cli.switch_to_tool("gemini", CONTEXT) is True
    assert len(runs) == 1


def test_codex_allowed_with_opt_in_and_receives_file(monkeypatch, isolated_home):
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        captured.append(cmd)
        # Snapshot the context file while the launched process would read it.
        files = list(_handoff_dir(isolated_home).glob("h-*/context-*.md"))
        assert len(files) == 1
        assert NAME_RE.match(files[0].name)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(lore_session_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        lore_session_cli,
        "get_safe_executable",
        lambda tool: f"/fake/bin/{tool}",
    )

    assert lore_session_cli.switch_to_tool("codex", CONTEXT, allow_cloud=True) is True
    assert len(captured) == 1
    assert captured[0][1].startswith("@")
    assert list(_handoff_dir(isolated_home).glob("h-*/context-*.md")) == []


def test_argv_does_not_contain_secret(isolated_home, runs):
    assert lore_session_cli.switch_to_tool("gemini", CONTEXT, allow_cloud=True) is True
    argv = " ".join(runs[0])
    assert TOKEN not in argv
    assert TOKEN in CONTEXT  # sanity: the fixture really contains the token


def test_argv_carries_a_file_reference_not_the_context(isolated_home, runs):
    assert lore_session_cli.switch_to_tool("gemini", CONTEXT, allow_cloud=True) is True
    argv = runs[0]
    folder = argv[argv.index("--include-directories") + 1]
    # Access is limited to this handoff's own folder, not the shared handoff root.
    assert Path(folder).parent == _handoff_dir(isolated_home)
    assert Path(folder).name.startswith("h-")
    # No part of the session text reaches the process list, only the file path.
    assert all(CONTEXT[:40] not in part for part in argv)
    assert any(part.startswith("Continue the coding session described in @") for part in argv)


def test_manual_file_is_redacted(isolated_home, runs):
    assert lore_session_cli.switch_to_tool("cursor", CONTEXT) is True
    files = list(_handoff_dir(isolated_home).glob("h-*/context-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert TOKEN not in content


def test_context_file_location_name_mode_and_removal(monkeypatch, isolated_home):
    seen: dict[str, list] = {}

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        files = list(_handoff_dir(isolated_home).glob("h-*/context-*.md"))
        seen["names"] = [f.name for f in files]
        seen["modes"] = [os.stat(f).st_mode & 0o777 for f in files]
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(lore_session_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        lore_session_cli,
        "get_safe_executable",
        lambda tool: f"/fake/bin/{tool}",
    )

    assert lore_session_cli.switch_to_tool("gemini", CONTEXT, allow_cloud=True) is True

    assert len(seen["names"]) == 1
    assert NAME_RE.match(seen["names"][0])
    if sys.platform != "win32":  # the mode argument is ignored on Windows
        assert seen["modes"] == [0o600]
    # Removed after the launched run.
    assert list(_handoff_dir(isolated_home).glob("h-*/context-*.md")) == []


def test_manual_tools_keep_the_file(isolated_home, runs, capsys):
    for tool in ("cursor", "vscode"):
        assert lore_session_cli.switch_to_tool(tool, CONTEXT) is True
        files = list(_handoff_dir(isolated_home).glob("h-*/context-*.md"))
        assert len(files) == 1
        out = capsys.readouterr().out
        assert str(files[0]) in out
        files[0].unlink()


def test_two_calls_produce_different_files(isolated_home, runs, capsys):
    paths = []
    for _ in range(2):
        assert lore_session_cli.switch_to_tool("cursor", CONTEXT) is True
        out = capsys.readouterr().out
        (path,) = _handoff_dir(isolated_home).glob("h-*/context-*.md")
        paths.append(path)
        assert path.name in out
        path.unlink()
    assert paths[0] != paths[1]


def test_stale_context_files_removed_fresh_kept(isolated_home, runs):
    handoff = _handoff_dir(isolated_home)
    handoff.mkdir(parents=True)
    stale = handoff / ("context-" + "a" * 32 + ".md")  # legacy bare file
    stale.write_text("old", encoding="utf-8")
    old = time.time() - 25 * 60 * 60
    os.utime(stale, (old, old))
    fresh = handoff / ("context-" + "b" * 32 + ".md")
    fresh.write_text("new", encoding="utf-8")

    assert lore_session_cli.switch_to_tool("gemini", CONTEXT, allow_cloud=True) is True
    assert not stale.exists()
    assert fresh.exists()


def test_predictable_old_symlink_path_is_irrelevant(isolated_home, runs, monkeypatch, tmp_path):
    # Historically the context was written to the fixed path
    # /tmp/ai-session-context.md with a symlink-following open(..., "w").
    # The new code never references that path (or any constant path), so an
    # attacker-planted symlink at the old location is simply left alone.
    canary = tmp_path / "canary.md"
    canary.write_text("canary", encoding="utf-8")
    old_path = tmp_path / "ai-session-context.md"
    try:
        old_path.symlink_to(canary)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    assert lore_session_cli.switch_to_tool("gemini", CONTEXT, allow_cloud=True) is True
    assert canary.read_text(encoding="utf-8") == "canary"
    assert old_path.is_symlink()


def test_claude_needs_no_opt_in(isolated_home, runs):
    assert lore_session_cli.switch_to_tool("claude", CONTEXT) is True
    assert len(runs) == 1
    assert runs[0][1] == "--continue"
    assert list(_handoff_dir(isolated_home).glob("h-*/context-*.md")) == []


# --- MCP tool (lore/interfaces/mcp_tools/sessions.py) -------------------------


def _mcp_switch(arguments: dict) -> str:
    from lore.interfaces import mcp

    server = mcp.create_server()
    handler = server.tools["switch_to_tool"]["handler"]
    import asyncio

    return asyncio.run(handler(arguments))


def _mcp_completed(stdout: str = "ctx") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_mcp_refuses_gemini_without_env(monkeypatch, isolated_home):
    from lore.interfaces.mcp_tools import sessions

    spawn_calls: list[list[str]] = []
    monkeypatch.setattr(sessions.shutil, "which", lambda name: "/usr/bin/lore-session")

    def fake_run(cmd, **kwargs):
        spawn_calls.append(cmd)
        return _mcp_completed()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)

    result = _mcp_switch({"tool": "gemini"})
    assert "LORE_ALLOW_CLOUD=1" in result
    assert spawn_calls == []


def test_mcp_passes_allow_cloud_when_granted(monkeypatch, isolated_home):
    from lore.interfaces.mcp_tools import sessions

    spawn_calls: list[list[str]] = []
    monkeypatch.setattr(sessions.shutil, "which", lambda name: "/usr/bin/lore-session")
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")

    def fake_run(cmd, **kwargs):
        spawn_calls.append(cmd)
        return _mcp_completed("context body")

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)

    result = _mcp_switch({"tool": "gemini", "max_messages": 5})
    assert len(spawn_calls) == 1
    cmd = spawn_calls[0]
    assert cmd[0] == "lore-session" and cmd[1] == "switch" and cmd[2] == "gemini"
    assert "--allow-cloud" in cmd
    assert "context body" in result
