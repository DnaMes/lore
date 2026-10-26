"""Real headless-browser interaction checks for the Lore web UI (#194).

The static template/CSP tests cannot see a dead control: a button can render
with the right attributes and still do nothing. This module drives a real
Chromium against a real Flask server that serves a synthetic archive built from
synthetic Claude Code transcripts in a temporary ``HOME``. It covers the flows
that regressed through markup and nonce changes:

* the topbar search modal returns results and navigates to a session,
* the code-card copy button writes the exact code to the clipboard,
* the tag filter narrows the session list,
* a message permalink navigates within the bounded window and back to the
  full session,
* the session list "Load more" pagination appends the second page.

The tests are marked ``browser`` and skip cleanly when Playwright or its
Chromium build is not installed, so the default suite never depends on them.
Install and run with::

    pip install -e ".[browser]" && playwright install chromium
    .venv/bin/python -m pytest tests/test_browser_interactions.py -o addopts="" -q -m browser
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lore.interfaces import web, web_data
from lore.storage import add_session_tag


def _playwright():
    """Return the Playwright sync API, skipping the test when it is absent."""
    return pytest.importorskip(
        "playwright.sync_api",
        reason="playwright is not installed; pip install -e '.[browser]'",
    )


SEEDED_SESSIONS = 51
TAGGED_SESSION_IDS = ("browser-session-01", "browser-session-02")
TAG_NAME = "release"
CODE_MARKER = "print('hello browser')"
SEARCH_TERM = "needle"

pytestmark = pytest.mark.browser


def _session_records(session_id: str, index: int) -> list[dict]:
    """Claude Code JSONL records for one small, fully rendered session."""
    start = datetime(2026, 1, 1, 9, 0, 0) + timedelta(minutes=index)
    stride = timedelta(minutes=10)
    return [
        {
            "type": "user",
            "timestamp": (start).isoformat(),
            "sessionId": session_id,
            "message": {
                "role": "user",
                "content": f"Find the {SEARCH_TERM} in browser fixture session {index}",
            },
            "uuid": f"{session_id}-u1",
        },
        {
            "type": "assistant",
            "timestamp": (start + stride).isoformat(),
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "content": (
                    f"Here is the patch we need to read:\n\n```python\n{CODE_MARKER}\n```\n"
                ),
            },
            "uuid": f"{session_id}-a1",
        },
        {
            "type": "user",
            "timestamp": (start + stride * 2).isoformat(),
            "sessionId": session_id,
            "message": {"role": "user", "content": "Thanks, that is all."},
            "uuid": f"{session_id}-u2",
        },
        {
            "type": "assistant",
            "timestamp": (start + stride * 3).isoformat(),
            "sessionId": session_id,
            "message": {"role": "assistant", "content": "Done."},
            "uuid": f"{session_id}-a2",
        },
    ]


def _write_archive(home: Path) -> Path:
    """Write synthetic Claude Code transcripts and return the Lore data dir."""
    project_dir = home / ".claude" / "projects" / "-repo-browser-fixture"
    project_dir.mkdir(parents=True)
    for index in range(1, SEEDED_SESSIONS + 1):
        session_id = f"browser-session-{index:02d}"
        records = _session_records(session_id, index)
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records),
            encoding="utf-8",
        )
    output_dir = home / ".lore"
    output_dir.mkdir()
    return output_dir


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """A seeded, indexed Lore archive that the tests can serve repeatedly."""
    from lore.extractors.base import BaseExtractor
    from lore.services.extraction import build_search_index
    from lore.storage import embeddings

    home = tmp_path_factory.mktemp("browser-home")
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    # The fixture must stay offline and fast: skip the optional embedding pass.
    previous_model_failed = embeddings._model_failed
    embeddings._model_failed = True
    # The production threshold hides the tiny synthetic transcripts; the
    # conftest fixture relaxes it too, but a module-scoped fixture runs first.
    previous_min_prompts = BaseExtractor.MIN_USER_PROMPTS
    BaseExtractor.MIN_USER_PROMPTS = 1
    try:
        output_dir = _write_archive(home)
        index_path = output_dir / "index.json"
        build_search_index(output_dir, index_path, incremental=False)
        for session_id in TAGGED_SESSION_IDS:
            add_session_tag(output_dir, session_id, TAG_NAME)
    finally:
        BaseExtractor.MIN_USER_PROMPTS = previous_min_prompts
        embeddings._model_failed = previous_model_failed
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home
    assert (output_dir / "index.json").exists(), "fixture index was not built"
    return home, output_dir, index_path


@pytest.fixture
def lore_site(archive):
    """Serve the real Flask app on 127.0.0.1 with the seeded archive."""
    from werkzeug.serving import make_server

    home, output_dir, index_path = archive
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)

    saved: dict[object, dict[str, object]] = {}
    for module in (web, web_data):
        saved[module] = {
            name: getattr(module, name)
            for name in ("OUTPUT_DIR", "INDEX_PATH", "DELETED_SESSIONS_PATH")
        }
        module.OUTPUT_DIR = output_dir
        module.INDEX_PATH = index_path
        module.DELETED_SESSIONS_PATH = output_dir / "deleted_sessions.json"

    web.clear_index_cache()
    web.clear_sessions_cache()
    web_data.load_sessions_for_tool.cache_clear()
    web._enriched_session_cached.cache_clear()

    server = make_server("127.0.0.1", 0, web.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        for module, values in saved.items():
            for name, value in values.items():
                setattr(module, name, value)
        web.clear_index_cache()
        web.clear_sessions_cache()
        web_data.load_sessions_for_tool.cache_clear()
        web._enriched_session_cached.cache_clear()
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home


@pytest.fixture(scope="module")
def chromium():
    """Launch one headless Chromium, or skip when the browser is unavailable."""
    sync_api = _playwright()
    try:
        playwright = sync_api.sync_playwright().start()
    except sync_api.Error as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"playwright could not start: {exc}")
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium is not installed: {exc}")
        try:
            yield browser
        finally:
            browser.close()
    finally:
        playwright.stop()


@pytest.fixture
def page(chromium, lore_site):
    """A fresh page with clipboard access, closed after each test."""
    context = chromium.new_context(viewport={"width": 1280, "height": 800})
    context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=lore_site,
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def test_search_modal_navigates_to_session(page, lore_site):
    sync_api = _playwright()
    page.goto(lore_site + "/", wait_until="networkidle")
    page.keyboard.press("Control+k")
    search_input = page.locator("#searchInput")
    sync_api.expect(search_input).to_be_visible()
    search_input.fill(SEARCH_TERM)

    result = page.locator("#searchResults a[href^='/session/']").first
    sync_api.expect(result).to_be_visible(timeout=10_000)
    result.click()
    sync_api.expect(page).to_have_url(re.compile(r"/session/browser-session-\d+"))
    sync_api.expect(page.locator("h1")).to_be_visible()


def test_copy_code_button_writes_clipboard(page, lore_site):
    sync_api = _playwright()
    page.goto(f"{lore_site}/session/browser-session-01", wait_until="networkidle")
    copy_button = page.locator(".code-card-copy").first
    sync_api.expect(copy_button).to_be_visible(timeout=10_000)
    copy_button.click()
    sync_api.expect(copy_button).to_have_text("Copied")
    clipboard = page.evaluate("navigator.clipboard.readText()")
    assert CODE_MARKER in clipboard


def test_tag_filter_narrows_session_list(page, lore_site):
    sync_api = _playwright()
    page.goto(f"{lore_site}/sessions", wait_until="networkidle")
    page.locator('select[name="tag"]').select_option(TAG_NAME)
    sync_api.expect(page).to_have_url(re.compile(r"tag=release"))
    sync_api.expect(page.locator(".session-row")).to_have_count(len(TAGGED_SESSION_IDS))
    sync_api.expect(page.locator("select[name='tag']")).to_have_value(TAG_NAME)


def test_message_permalink_navigation(page, lore_site):
    sync_api = _playwright()
    page.goto(f"{lore_site}/session/browser-session-01/m/1", wait_until="networkidle")
    sync_api.expect(page.get_by_text(re.compile(r"Message 1 of"))).to_be_visible()

    page.locator("a[href$='/m/3']").first.click()
    sync_api.expect(page).to_have_url(re.compile(r"/m/3$"))
    sync_api.expect(page.locator("#msg-3")).to_be_visible()

    page.get_by_role("link", name="Open full session").click()
    sync_api.expect(page).to_have_url(re.compile(r"/session/browser-session-01$"))
    sync_api.expect(page.locator(".message-cluster").first).to_be_visible()


def test_session_list_load_more_pagination(page, lore_site):
    sync_api = _playwright()
    page.goto(f"{lore_site}/sessions", wait_until="networkidle")
    rows = page.locator(".session-row")
    sync_api.expect(rows).to_have_count(50)
    # Keyboard activation: the filters sheet is absolutely positioned at the
    # document bottom on desktop and intercepts pointer events over the button.
    load_more = page.locator("#loadMoreSessions")
    load_more.focus()
    page.keyboard.press("Enter")
    sync_api.expect(rows).to_have_count(SEEDED_SESSIONS)
    sync_api.expect(page.locator("#loadMoreSessions")).to_have_count(0)
