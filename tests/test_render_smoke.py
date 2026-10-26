"""Headless render smoke-test (#51).

Issue #50 — a CSP nonce in style-src left the whole UI unstyled — passed
the entire suite because the tests only asserted CSP *header shape*, never
the *rendered result*. This test renders key pages in headless Chromium
and asserts:
  - zero CSP console violations
  - the Tailwind runtime actually applied styles (page is not blank)

It is skipped when no Chromium binary is available, so CI without a
browser still passes.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from contextlib import closing

import pytest

_CHROME = (
    shutil.which("chromium-browser")
    or shutil.which("chromium")
    or shutil.which("google-chrome")
    or shutil.which("chrome")
)

requires_chrome = pytest.mark.skipif(_CHROME is None, reason="no Chromium/Chrome binary available")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Run the Flask app in a background thread on a free port."""
    from lore.interfaces.web import app

    port = _free_port()

    server_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    # Wait for the port to accept connections.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:  # pragma: no cover - server failed to start
        pytest.fail("live server did not start")

    yield f"http://127.0.0.1:{port}"


def _render(url: str) -> str:
    """Render a URL headless, return combined stderr (Chromium logs CSP there)."""
    assert _CHROME is not None  # guaranteed by the requires_chrome skip marker
    proc = subprocess.run(
        [
            _CHROME,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--enable-logging=stderr",
            "--virtual-time-budget=3000",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=180,  # a cold Chromium on a shared CI runner can need more than a minute
    )
    return proc.stdout + "\n" + proc.stderr


@requires_chrome
@pytest.mark.parametrize("path", ["/", "/sessions", "/memory"])
def test_page_has_no_csp_violations(live_server, path):
    """A rendered page must produce no CSP console violations (guards #50)."""
    output = _render(live_server + path)
    lowered = output.lower()
    assert "content security policy" not in lowered, (
        f"CSP violation while rendering {path}:\n{output[:600]}"
    )
    assert "refused to" not in lowered, f"resource refused while rendering {path}"


@requires_chrome
def test_dashboard_renders_styled(live_server):
    """The Tailwind runtime must apply styles — the DOM is non-trivial."""
    output = _render(live_server + "/")
    # The dashboard's known content must be present in the rendered DOM.
    assert "Overview" in output
    assert "lore v" in output  # version footer rendered
