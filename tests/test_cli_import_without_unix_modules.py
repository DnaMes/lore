"""The CLI must import where Unix-only modules are missing (Windows).

The first Windows run of the install matrix showed `ModuleNotFoundError: No module named
'termios'` on every command because `lore_cli` imported `pty` at module level.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

BLOCKER = """
import sys
for name in ("pty", "termios", "tty", "fcntl", "pwd", "grp", "resource"):
    sys.modules[name] = None  # `import name` now raises ImportError, like on Windows
import lore_cli
import lore.cli.web  # noqa: F401
import lore.cli.mcp  # noqa: F401
print("imported")
"""


def test_cli_modules_import_without_unix_only_modules() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", BLOCKER],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "imported"
