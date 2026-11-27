"""Guard the console-script entry points declared in pyproject.

Regression cover for the empty-module bug (issue #66): `annuli/cli/web.py` and
`annuli/cli/mcp.py` were 0 bytes while pyproject declared them as entry points, so
`annuli-web` / `annuli-mcp` crashed with ImportError. These tests fail if the
entry-point callables go missing again.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path


def _declared_scripts() -> dict[str, str]:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    return data["project"]["scripts"]


def test_web_entry_point_is_importable_and_callable():
    from annuli.cli.web import build_parser, main

    assert callable(main)
    assert isinstance(build_parser(), argparse.ArgumentParser)


def test_mcp_entry_point_is_importable_and_callable():
    from annuli.cli.mcp import main, main_sync

    assert callable(main)
    assert callable(main_sync)


def test_pyproject_entry_targets_resolve():
    """Every `module:attr` declared under [project.scripts] must exist."""
    import importlib

    for name, target in _declared_scripts().items():
        module_path, _, attr = target.partition(":")
        module = importlib.import_module(module_path)
        assert hasattr(module, attr), f"{name}: {target} has no attribute {attr!r}"


def test_web_parser_defaults_and_overrides(monkeypatch):
    from annuli.cli.web import build_parser

    monkeypatch.delenv("ANNULI_WEB_HOST", raising=False)
    monkeypatch.delenv("ANNULI_WEB_PORT", raising=False)
    monkeypatch.delenv("ANNULI_WEB_DEBUG", raising=False)
    args = build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 5000
    assert args.debug is False

    args = build_parser().parse_args(["--host", "0.0.0.0", "--port", "5057", "--debug"])
    assert args.host == "0.0.0.0"
    assert args.port == 5057
    assert args.debug is True


def test_web_main_invokes_start_web_ui(monkeypatch):
    """`main()` wires parsed args into start_web_ui without actually serving."""
    import annuli.interfaces.web as web

    captured: dict[str, object] = {}

    def fake_start(port, host, debug):
        captured.update(port=port, host=host, debug=debug)

    monkeypatch.setattr(web, "start_web_ui", fake_start)

    from annuli.cli.web import main

    rc = main(["--host", "127.0.0.1", "--port", "5099"])
    assert rc == 0
    assert captured == {"port": 5099, "host": "127.0.0.1", "debug": False}
