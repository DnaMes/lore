"""`lore run` writes raw transcripts: the log directory and files must be private."""

from __future__ import annotations

import os
import stat
import sys
from argparse import Namespace
from pathlib import Path

import pytest

import lore_cli

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def test_run_log_is_owner_only_from_creation(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "archive"
    old_umask = os.umask(0o022)
    try:
        monkeypatch.setitem(lore_cli.TOOL_EXECUTABLES, "claude", sys.executable)
        monkeypatch.setattr(lore_cli, "cmd_sync", lambda *a, **k: 0)

        def fake_spawn(argv, master_read):
            # Stands in for the pseudo-terminal: feed one chunk of transcript to the logger.
            r, w = os.pipe()
            os.write(w, b"SYNTHETIC PRIVATE SESSION TEXT")
            os.close(w)
            master_read(r)
            os.close(r)
            return 0

        monkeypatch.setattr(pytest.importorskip("pty"), "spawn", fake_spawn)
        args = Namespace(
            tool="claude",
            tool_args=["-c", "print('SYNTHETIC PRIVATE SESSION TEXT')"],
            output_dir=str(out),
            no_pty=False,
        )
        lore_cli.cmd_run(args)
    finally:
        os.umask(old_umask)

    runs = out / "runs"
    assert stat.S_IMODE(runs.stat().st_mode) == 0o700
    logs = list(runs.glob("*.log"))
    assert logs, "no log written"
    for log in logs:
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        assert b"SYNTHETIC PRIVATE SESSION TEXT" in log.read_bytes()
    for meta in runs.glob("*.json"):
        assert stat.S_IMODE(meta.stat().st_mode) == 0o600


def test_existing_log_is_tightened(tmp_path: Path) -> None:
    log = tmp_path / "old.log"
    log.write_bytes(b"x")
    log.chmod(0o644)
    handle = lore_cli._open_private_append(log)
    handle.close()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
