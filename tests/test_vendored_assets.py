"""Tests for vendored web assets — offline / air-gapped support (issues #19, #129).

The web UI must not depend on any CDN: highlight.js is checked into
``lore/interfaces/static/`` and served by Flask. Tailwind is a build-time
compiled stylesheet (#129) — also checked in, but produced by
``scripts/build_tailwind.sh`` rather than downloaded.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from lore.interfaces import web

STATIC_DIR = Path(web.__file__).parent / "static"

VENDORED_FILES = [
    "highlight-11.9.0.min.js",
    "highlight-github-11.9.0.min.css",
]


@pytest.fixture()
def client():
    with web.app.test_client() as c:
        yield c


@pytest.mark.parametrize("name", VENDORED_FILES)
def test_vendored_file_exists(name):
    path = STATIC_DIR / name
    assert path.is_file(), f"missing vendored asset: {path}"
    assert path.stat().st_size > 0


def test_tailwind_compiled_stylesheet_exists():
    """The precompiled Tailwind sheet must be checked in (#129)."""
    path = STATIC_DIR / "tailwind-compiled.min.css"
    assert path.is_file(), f"missing compiled Tailwind stylesheet: {path}"
    assert path.stat().st_size > 1000, "compiled Tailwind sheet looks truncated"


@pytest.mark.parametrize("name", VENDORED_FILES)
def test_static_route_serves_asset(client, name):
    resp = client.get(f"/static/{name}")
    assert resp.status_code == 200
    assert len(resp.data) > 0
    assert resp.data == (STATIC_DIR / name).read_bytes()


def test_static_route_serves_tailwind_stylesheet(client):
    resp = client.get("/static/tailwind-compiled.min.css")
    assert resp.status_code == 200
    assert len(resp.data) > 1000


def test_templates_reference_no_cdn():
    """No CDN host may appear in any template file (#30 — templates are now files)."""
    from pathlib import Path

    templates_dir = Path(__file__).resolve().parent.parent / "lore" / "templates"
    template_files = sorted(templates_dir.glob("*.html"))
    assert template_files, "no template files found under lore/templates/"

    cdn_hosts = (
        "cdn.tailwindcss.com",
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    )
    for tpl in template_files:
        value = tpl.read_text(encoding="utf-8")
        for host in cdn_hosts:
            assert host not in value, f"{tpl.name} still references CDN host {host}"


def test_dashboard_links_local_assets(client):
    body = client.get("/").get_data(as_text=True)
    assert "/static/tailwind-compiled.min.css" in body
    assert "/static/highlight-github-11.9.0.min.css" in body


def test_templates_load_no_tailwind_jit_runtime():
    """The Play-CDN JIT runtime must be gone from every template (#129)."""
    templates_dir = Path(__file__).resolve().parent.parent / "lore" / "templates"
    for tpl in sorted(templates_dir.glob("*.html")):
        value = tpl.read_text(encoding="utf-8")
        assert "tailwind-3.4.16.min.js" not in value, (
            f"{tpl.name} still loads the Tailwind JIT runtime"
        )
        assert "tailwind.config" not in value, (
            f"{tpl.name} still configures the (removed) JIT runtime"
        )


def test_vendor_manifest_hashes_match():
    """scripts/vendor_assets.py manifest must match the checked-in files."""
    import importlib.util

    script = Path(web.__file__).parents[2] / "scripts" / "vendor_assets.py"
    spec = importlib.util.spec_from_file_location("vendor_assets", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name, (_url, expected) in module.ASSETS.items():
        actual = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()
        assert actual == expected, f"{name}: manifest hash stale"


# ---------------------------------------------------------------------------
# Compiled-Tailwind coverage (#129)
# ---------------------------------------------------------------------------

# Classes that are JS behavior hooks or mode value tokens — deliberately not
# Tailwind utilities and deliberately unstyled (no CSS rule anywhere).
NON_STYLED_CLASS_TOKENS = {
    "tag-remove",  # JS hook on the tag editor remove button (session.html)
    "toast-close",  # JS hook on the dynamically created error toast
    "compact",  # density-mode value comparison, body gets density-compact
}


def _extract_template_class_tokens(templates_dir: Path) -> set[str]:
    """Every class token referenced anywhere in the templates."""
    tokens: set[str] = set()

    def _strip_jinja(chunk: str) -> str:
        chunk = re.sub(r"\{\{.*?\}\}", " ", chunk)
        return re.sub(r"\{%.*?%\}", " ", chunk)

    for tpl in sorted(templates_dir.glob("*.html")):
        text = tpl.read_text(encoding="utf-8")
        # class="..." / class='...' attributes — plain word tokens.
        for attr in re.findall(r'class="([^"]*)"', text) + re.findall(r"class='([^']*)'", text):
            tokens.update(_strip_jinja(attr).split())
        # className = '...' / "..." assignments — fully quoted values.
        for attr in re.findall(r"className\s*=\s*'([^']*)'", text) + re.findall(
            r'className\s*=\s*"([^"]*)"', text
        ):
            tokens.update(attr.split())
        # classList.add/toggle/remove('a', 'b', expr) — quoted args only;
        # the remaining call arguments are JS expressions, not classes.
        for call in re.findall(r"classList\.(?:add|toggle|remove)\(([^)]*)\)", text):
            for quoted in re.findall(r"'([^']*)'", call):
                tokens.update(quoted.split())
    return {t for t in tokens if t}


def test_compiled_tailwind_covers_every_template_class_token():
    """Every template class token must be styled by the compiled sheet, a
    template <style> rule, or the explicit JS-hook allowlist (#129).

    Guards against the classic precompilation failure mode: classes that
    only appear inside JS strings (search panel, toast, code-block chrome)
    silently losing their styles because the scanner missed their source.
    """
    static_dir = STATIC_DIR
    css = (static_dir / "tailwind-compiled.min.css").read_text(encoding="utf-8")

    # Compiled selectors, un-escaped: .sm\:group-hover\:opacity-100 → token.
    compiled = {m.replace("\\", "") for m in re.findall(r"\.((?:[\w-]|\\.)+)", css)}

    # Custom classes defined in template <style> blocks (non-Tailwind CSS).
    templates_dir = Path(web.__file__).parents[1] / "templates"
    custom: set[str] = set()
    for tpl in sorted(templates_dir.glob("*.html")):
        text = tpl.read_text(encoding="utf-8")
        for style in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S):
            custom.update(re.findall(r"\.([A-Za-z_][\w-]*)", style))

    tokens = _extract_template_class_tokens(templates_dir)

    missing = []
    for token in sorted(tokens):
        if token in NON_STYLED_CLASS_TOKENS or token in custom:
            continue
        # Variants stack (sm:group-hover:opacity-100) — match the full
        # token or its trailing utility segment.
        candidates = {token, token.split(":")[-1], token.replace("/", "\\/")}
        if not (candidates & compiled):
            missing.append(token)

    assert not missing, (
        f"template class tokens with no compiled Tailwind rule and no custom "
        f"CSS definition (rerun scripts/build_tailwind.sh if these are new "
        f"utilities): {missing}"
    )
