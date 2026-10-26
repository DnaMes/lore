"""Bare lore knowledge and cloud providers without opt-in must not call a provider."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import lore.llm
import lore_cli


def _args(tmp_path: Path, **overrides) -> Namespace:
    base = {
        "output_dir": str(tmp_path),
        "provider": None,
        "model": None,
        "allow_cloud": False,
        "limit": 1,
        "tool": None,
        "output": None,
        "include_automated": False,
    }
    base.update(overrides)
    return Namespace(**base)


def test_knowledge_default_makes_no_provider_call(tmp_path, monkeypatch, capsys):
    (tmp_path / "index.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    provider = MagicMock()
    monkeypatch.setattr(lore.llm, "get_provider", provider)

    rc = lore_cli.cmd_knowledge(_args(tmp_path))

    assert rc == 2
    provider.assert_not_called()
    err = capsys.readouterr().err
    assert "--provider" in err
    assert "--allow-cloud" in err
    assert "LORE_ALLOW_CLOUD" in err


def test_knowledge_gemini_without_opt_in_makes_no_provider_call(tmp_path, monkeypatch, capsys):
    (tmp_path / "index.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    provider = MagicMock()
    monkeypatch.setattr(lore.llm, "get_provider", provider)

    rc = lore_cli.cmd_knowledge(_args(tmp_path, provider="gemini"))

    assert rc == 2
    provider.assert_not_called()
    err = capsys.readouterr().err
    assert "--allow-cloud" in err
    assert "LORE_ALLOW_CLOUD" in err
    assert "gemini" in err


def test_knowledge_parser_default_provider_is_none():
    args = lore_cli.build_parser().parse_args(["knowledge"])
    assert args.provider is None
