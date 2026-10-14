"""Snippets and source tags on session search and memory recall (#197, task 4).

FTS5 ``snippet()`` marks matches with the sentinels ``\\x02`` / ``\\x03`` in
the storage layer; every payload converts them to ``«`` / ``»`` text and every
HTML renderer escapes first and substitutes ``<mark>`` afterwards, because
chat text is untrusted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.interfaces import mcp, web, web_data
from lore.storage import add_memory, recall_memory, search_sessions
from lore.storage.fts import SNIPPET_CLOSE, SNIPPET_OPEN, snippet_to_text
from lore.storage.memory import MemoryEntry
from lore.utils.formatting import escape_snippet_html

BASE_HTML = Path(web.__file__).parent.parent / "templates" / "base.html"
LONG_BODY = "filler " * 300 + "the zebracorn appears here " + "tail " * 300


def _session(sid: str, *, title: str = "Generic session title", body: str = LONG_BODY):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=title,
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1))],
    )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _assert_no_control_chars(payload: Any) -> None:
    for text in _strings(payload):
        assert SNIPPET_OPEN not in text and SNIPPET_CLOSE not in text, text


@pytest.fixture()
def v2_env(tmp_path, monkeypatch):
    """Point web, web_data and mcp at a temp v2 store and keep search FTS-only."""
    monkeypatch.setattr(web, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setenv("LORE_USE_V2", "1")
    monkeypatch.setenv("LORE_HYBRID_SEARCH", "0")
    web_data.clear_index_cache()
    return tmp_path


# ---------------------------------------------------------------------------
# snippet_to_text
# ---------------------------------------------------------------------------


def test_snippet_to_text_maps_markers():
    assert snippet_to_text(f"a {SNIPPET_OPEN}hit{SNIPPET_CLOSE} b") == "a «hit» b"


def test_snippet_to_text_neutralises_literal_guillemets():
    text = snippet_to_text(f"fake «x» and {SNIPPET_OPEN}real{SNIPPET_CLOSE}")
    assert text == "fake ‹x› and «real»"


def test_snippet_to_text_leaves_no_control_chars():
    # A body that itself contains the sentinels must not leak them.
    text = snippet_to_text("body \x02 with \x03 stray \x02 marks")
    assert "\x02" not in text and "\x03" not in text


# ---------------------------------------------------------------------------
# search_sessions
# ---------------------------------------------------------------------------


def test_search_sessions_returns_marked_short_snippet(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    hits = search_sessions(tmp_path, "zebracorn")
    assert len(hits) == 1
    snippet = hits[0]["snippet"]
    assert f"{SNIPPET_OPEN}zebracorn{SNIPPET_CLOSE}" in snippet
    assert len(snippet) < len(LONG_BODY) / 10


def test_search_sessions_title_match_yields_valid_snippet(tmp_path):
    IndexBuilder(tmp_path).build_index(
        [_session("a", title="Quantumfoo walkthrough guide", body="unrelated content words")], {}
    )
    hits = search_sessions(tmp_path, "quantumfoo")
    assert [h["session"]["id"] for h in hits] == ["a"]
    # The indexed body starts with the title, so a title hit is marked too.
    snippet = hits[0]["snippet"]
    assert snippet is not None
    assert "«Quantumfoo»" in snippet_to_text(snippet)


def test_search_sessions_keeps_existing_keys(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    hit = search_sessions(tmp_path, "zebracorn")[0]
    assert {"session", "score", "snippet"} <= set(hit)


# ---------------------------------------------------------------------------
# JSON / MCP payloads
# ---------------------------------------------------------------------------


def test_api_search_has_text_snippet_and_source(v2_env):
    IndexBuilder(v2_env).build_index([_session("a", body=f"x \x02 y {LONG_BODY}")], {})
    with web.app.test_client() as c:
        resp = c.get("/api/search?q=zebracorn")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert len(payload) == 1
    assert "«zebracorn»" in payload[0]["snippet"]
    assert payload[0]["source"] == "session"
    assert {"id", "title", "tool", "date"} <= set(payload[0])
    _assert_no_control_chars(payload)


def test_api_v1_search_has_text_snippet_and_source(v2_env):
    IndexBuilder(v2_env).build_index([_session("a", body=f"x \x03 y {LONG_BODY}")], {})
    with web.app.test_client() as c:
        resp = c.get("/api/v1/search?q=zebracorn")
    assert resp.status_code == 200
    payload = resp.get_json()
    result = payload["results"][0]
    assert "«zebracorn»" in result["snippet"]
    assert result["source"] == "session"
    assert result["id"] == "a" and "score" in result
    _assert_no_control_chars(payload)


def test_mcp_search_history_payload_has_snippet(v2_env):
    IndexBuilder(v2_env).build_index([_session("a")], {})
    handler = mcp.create_server().tools["search_history"]["handler"]
    payload = json.loads(asyncio.run(handler({"query": "zebracorn"})))
    result = payload["results"][0]
    assert "«zebracorn»" in result["snippet"]
    assert result["source"] == "session"
    _assert_no_control_chars(payload)


def test_list_sessions_payload_is_unchanged(v2_env):
    IndexBuilder(v2_env).build_index([_session("a")], {})
    handler = mcp.create_server().tools["list_sessions"]["handler"]
    payload = json.loads(asyncio.run(handler({})))
    assert "snippet" not in payload["sessions"][0]
    assert "source" not in payload["sessions"][0]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_memory_entry_to_dict_has_snippet_and_source():
    entry = MemoryEntry(
        id=1, kind="fact", title="t", body="b", author=None, created="c", updated="u"
    )
    assert entry.to_dict()["snippet"] is None
    assert entry.to_dict()["source"] == "memory"
    entry.snippet = "a «b» c"
    assert entry.to_dict()["snippet"] == "a «b» c"


def test_recall_memory_fts_sets_text_snippet(tmp_path):
    add_memory(tmp_path, "fact", "Cache note", "the quokkaword lives in the cache layer")
    [entry] = recall_memory(tmp_path, "quokkaword")
    assert entry.snippet is not None and "«quokkaword»" in entry.snippet
    _assert_no_control_chars(entry.to_dict())


def test_recall_memory_literal_guillemets_are_neutralised(tmp_path):
    add_memory(tmp_path, "fact", "Marker note", "literal «x» then needleword")
    [entry] = recall_memory(tmp_path, "needleword")
    assert entry.snippet is not None
    assert "‹x›" in entry.snippet and "«x»" not in entry.snippet
    assert "«needleword»" in entry.snippet


# ---------------------------------------------------------------------------
# HTML escaping
# ---------------------------------------------------------------------------


def test_escape_snippet_html_escapes_then_marks():
    assert str(escape_snippet_html("<b>«hit»</b>")) == "&lt;b&gt;<mark>hit</mark>&lt;/b&gt;"


def test_memory_page_escapes_payload_and_shows_snippet(v2_env):
    add_memory(v2_env, "fact", "XSS probe", "<img src=x onerror=alert(1)> payload")
    with web.app.test_client() as c:
        body = c.get("/memory?q=payload").get_data(as_text=True)
    assert "&lt;img" in body
    assert "<img src=x" not in body
    assert "<mark>payload</mark>" in body


def test_memory_page_literal_guillemets_do_not_become_mark(v2_env):
    add_memory(v2_env, "fact", "Marker probe", "literal «x» then needleword")
    with web.app.test_client() as c:
        body = c.get("/memory?q=needleword").get_data(as_text=True)
    assert "<mark>needleword</mark>" in body
    assert "<mark>x</mark>" not in body


def test_base_html_dropdown_escapes_before_marking():
    html = BASE_HTML.read_text(encoding="utf-8")
    expr = "escapeHtml(s.snippet || '').replace(/«/g, '<mark>').replace(/»/g, '</mark>')"
    assert expr in html
    assert "s.snippet ?" in html  # second line only when a snippet exists


def test_rrf_merge_keeps_the_fts_snippet_for_hybrid_results():
    from lore.storage import rrf_merge

    fts = [{"session": {"id": "a"}, "score": 1.0, "snippet": "the \x02hit\x03 here"}]
    vec = [{"session": {"id": "b"}, "score": 0.2}, {"session": {"id": "a"}, "score": 0.3}]

    merged = {r["session"]["id"]: r for r in rrf_merge(fts, vec)}

    assert merged["a"]["snippet"] == "the \x02hit\x03 here"
    assert "snippet" not in merged["b"]
