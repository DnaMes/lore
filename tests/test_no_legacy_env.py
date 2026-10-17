"""The retired AI_HISTORY_* env names are not read; only LORE_* is honoured."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_ai_history_env_is_ignored(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LORE_", "AI_HISTORY_"))}
    env["HOME"] = str(tmp_path)
    env["AI_HISTORY_RATE_LIMIT_ENABLED"] = "false"

    code = (
        "import os\n"
        "import lore\n"
        "from lore.interfaces.web_utils import _rate_limit_enabled\n"
        "print(os.environ.get('LORE_RATE_LIMIT_ENABLED'))\n"
        "print(_rate_limit_enabled())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    lines = result.stdout.strip().splitlines()
    assert lines[-2:] == ["None", "True"], result.stdout + result.stderr
