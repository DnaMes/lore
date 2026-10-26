"""A console that cannot encode a character must not turn a command into a traceback.

The first Windows run of the install matrix crashed `lore check` with
`UnicodeEncodeError: 'charmap' codec can't encode character '\\u274c'` on a cp1252 console.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from lore.utils.console import make_console_safe

REPO = Path(__file__).resolve().parent.parent


def test_make_console_safe_replaces_unencodable_characters(monkeypatch) -> None:
    # newline="\n": without it Windows would write \r\n and the comparison below would differ
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    make_console_safe()
    print("status ❌ done")
    stream.flush()

    assert stream.buffer.getvalue().decode("cp1252") == "status ? done\n"


def test_make_console_safe_ignores_streams_without_reconfigure(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    make_console_safe()  # must not raise


def test_lore_check_survives_a_cp1252_console(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONIOENCODING": "cp1252",
        "PYTHONPATH": str(REPO),
    }
    proc = subprocess.run(
        [sys.executable, str(REPO / "lore_cli.py"), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("cp1252", "replace")[-1500:]
    assert b"Traceback" not in proc.stderr
