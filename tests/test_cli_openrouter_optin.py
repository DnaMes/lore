"""Cloud providers need --allow-cloud (or LORE_ALLOW_CLOUD) on analyze/knowledge/format."""

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
@pytest.mark.parametrize("provider", ["openrouter", "jev", "gemini"])
def test_cloud_optin_provider_without_allow_cloud_exits_2(
    command: str, provider: str, tmp_path: Path, capsys
) -> None:
    (tmp_path / "index.json").write_text(json.dumps({"sessions": []}))

    rc = getattr(lore_cli, command)(_args(tmp_path, provider=provider))

    assert rc == 2
    err = capsys.readouterr().err
    assert "--allow-cloud" in err
    assert "LORE_ALLOW_CLOUD" in err
    assert provider in err


def test_ollama_and_explicit_opt_in_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert lore_cli._cloud_optin_error(_args(tmp_path, provider="ollama")) is None
    assert lore_cli._cloud_optin_error(_args(tmp_path, provider="gemini", allow_cloud=True)) is None


def test_lore_allow_cloud_env_grants_opt_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_ALLOW_CLOUD", "1")
    assert lore_cli._cloud_optin_error(_args(tmp_path, provider="gemini")) is None
