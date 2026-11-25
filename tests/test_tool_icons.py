"""Tests for vendored tool brand icons and letter-badge fallbacks."""

from __future__ import annotations

import fnmatch
import hashlib
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from lore.core.models import Tool
from lore.interfaces import web
from lore.interfaces.web_helpers import TOOL_STYLES, get_style

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = Path(web.__file__).parent / "static" / "icons"


@pytest.fixture()
def client():
    with web.app.test_client() as c:
        yield c


def test_every_icon_file_exists_and_is_svg():
    icon_files = {style["icon_file"] for style in TOOL_STYLES.values() if style.get("icon_file")}
    assert icon_files, "expected mapped icon_file entries in TOOL_STYLES"
    for stem in sorted(icon_files):
        svg_path = ICONS_DIR / f"{stem}.svg"
        assert svg_path.is_file(), f"missing icon SVG: {svg_path}"
        content = svg_path.read_text(encoding="utf-8").lstrip()
        assert content.startswith("<svg"), f"{svg_path.name} does not start with <svg"


def test_every_tool_enum_resolves_and_colors_unique():
    for member in Tool:
        style = get_style(member.value)
        assert isinstance(style, dict)
        for key in ("color", "icon", "name"):
            assert style.get(key), f"Tool {member.value} missing {key}"

    colors = [style["color"].lower() for style in TOOL_STYLES.values()]
    assert len(colors) == len(set(colors)), "TOOL_STYLES colors must be unique"

    unknown = get_style("unknown-tool-xyz")
    assert isinstance(unknown, dict)
    assert unknown["name"] == "unknown-tool-xyz"
    assert "icon_file" not in unknown


def test_sha256sums_match_icon_files():
    sums_path = ICONS_DIR / "SHA256SUMS"
    assert sums_path.is_file(), "missing SHA256SUMS"
    lines = [line for line in sums_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "SHA256SUMS is empty"
    for line in lines:
        expected_hex, filename = line.split("  ", 1)
        target = ICONS_DIR / filename
        assert target.is_file(), f"SHA256SUMS references missing file: {filename}"
        actual_hex = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual_hex == expected_hex, f"SHA256 mismatch for {filename}"


def test_dashboard_renders_svg_for_claude_and_letter_for_aider(client):
    payload = {
        "sessions": [
            {
                "id": "s-claude",
                "tool": "claude-code",
                "title": "Claude session",
                "display_title": "Claude session",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo/demo",
                "messages": 2,
                "prompts": 1,
                "tokens": 100,
                "export_path": None,
            },
            {
                "id": "s-aider",
                "tool": "aider",
                "title": "Aider session",
                "display_title": "Aider session",
                "created": "2026-04-01T09:00:00",
                "updated": "2026-04-01T09:05:00",
                "project": "/repo/demo",
                "messages": 2,
                "prompts": 1,
                "tokens": 100,
                "export_path": None,
            },
        ],
        "stats": {
            "total_sessions": 2,
            "total_messages": 4,
            "by_tool": {"claude-code": 1, "aider": 1},
            "by_project": {"/repo/demo": 2},
        },
        "generated_at": "2026-04-01T00:00:00Z",
    }
    with (
        patch.object(web, "load_index", return_value=payload),
        patch.object(web, "load_deleted_session_ids", return_value=set()),
    ):
        resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/static/icons/claudecode.svg" in html
    assert "Ai" in html
    assert "/static/icons/aider.svg" not in html


def test_static_icon_route_and_csp(client):
    icon_resp = client.get("/static/icons/claudecode.svg")
    assert icon_resp.status_code == 200
    assert icon_resp.content_type.startswith("image/")

    page_resp = client.get("/")
    csp = page_resp.headers.get("Content-Security-Policy", "")
    assert "img-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_package_data_covers_static_icons():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = pyproject["tool"]["setuptools"]["package-data"]["lore.interfaces"]
    required_files = [
        "static/icons/claudecode.svg",
        "static/icons/LICENSE-lobe-icons.txt",
        "static/icons/SHA256SUMS",
    ]
    for rel_path in required_files:
        assert any(fnmatch.fnmatch(rel_path, pattern) for pattern in globs), (
            f"package-data globs {globs} do not cover {rel_path}"
        )
