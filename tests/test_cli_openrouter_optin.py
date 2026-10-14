"""openrouter needs --allow-cloud on analyze/knowledge/format, like summarize."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import lore_cli


def _args(tmp_path: Path, **overrides) -> Namespace:
    base = {
        "output_dir": str(tmp_path),
        "provider": "openrouter",
        "model": None,
        "allow_cloud": False,
        "no_llm": False,
        "limit": 1,
        "tool": None,
        "session_id": None,
        "output": None,
        "include_automated": False,
    }
    base.update(overrides)
    return Namespace(**base)


@pytest.mark.parametrize("command", ["cmd_analyze", "cmd_knowledge", "cmd_format"])
def test_openrouter_without_allow_cloud_exits_2(command: str, tmp_path: Path, capsys) -> None:
    (tmp_path / "index.json").write_text(json.dumps({"sessions": []}))

    rc = getattr(lore_cli, command)(_args(tmp_path))

    assert rc == 2
    assert "--allow-cloud" in capsys.readouterr().err


def test_gemini_and_explicit_opt_in_pass(tmp_path: Path) -> None:
    assert lore_cli._cloud_optin_error(_args(tmp_path, provider="gemini")) is None
    assert lore_cli._cloud_optin_error(_args(tmp_path, allow_cloud=True)) is None
