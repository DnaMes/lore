"""Quoted phrase queries and the dedicated query validator (#197)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.interfaces import mcp, web
from lore.storage import add_memory, recall_memory, search_sessions
from lore.storage.fts import build_fts_query, phrase_of
from lore.utils.security import validate_search_param, validate_search_query

# ---------------------------------------------------------------------------
# build_fts_query
# ---------------------------------------------------------------------------


def test_phrase_builds_one_quoted_token():
    assert build_fts_query('"exact phrase"') == '"exact phrase"'
    assert build_fts_query('  "exact phrase"  ') == '"exact phrase"'


def test_plain_terms_are_unchanged():
    assert build_fts_query("a b") == '"a"* "b"*'
    assert build_fts_query("  oauth ") == '"oauth"*'


def test_empty_query_is_none():
    assert build_fts_query("") is None
    assert build_fts_query("   ") is None


@pytest.mark.parametrize(
    "query",
    ['a"b', '"', '""', '"foo', 'foo"', 'a "b" c', '"a" "b"', '"a "b" c"', "NEAR(a b)", "a*", "-a"],
)
def test_no_input_causes_an_fts5_syntax_error(query):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    conn.execute("INSERT INTO t VALUES ('a b exact phrase')")
    fts_query = build_fts_query(query)
    assert fts_query is not None
    conn.execute("SELECT rowid FROM t WHERE t MATCH ?", (fts_query,)).fetchall()


# ---------------------------------------------------------------------------
# validate_search_query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "plain words", '"foo bar"', '  "foo"  ', "oauth2 *"])
def test_validate_search_query_accepts(query):
    assert validate_search_query(query)


@pytest.mark.parametrize(
    "query",
    [
        '"foo',
        'foo"',
        'a "b" c',
        '"a" "b"',
        '""',
        '"  "',
        "'x'",
        '"a"b"',
        "a;b",
        "x" * 257,
        '"foo"\n',
        '\r"foo"',
        '"foo\nbar"',
    ],
)
def test_validate_search_query_rejects(query):
    assert not validate_search_query(query)


def test_validate_search_query_length_boundary():
    assert validate_search_query("x" * 256)
    assert validate_search_query('"' + "x" * 254 + '"')
    assert not validate_search_query('"' + "x" * 255 + '"')


def test_validate_search_param_still_rejects_quotes():
    assert not validate_search_param('"x"')
    assert not validate_search_param("'x'")


# ---------------------------------------------------------------------------
# End to end: session search and memory recall
# ---------------------------------------------------------------------------


def _session(sid, body):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title="T",
        messages=[UnifiedMessage(role=Role.USER, content=body, timestamp=datetime(2026, 1, 1))],
    )


def _ids(results):
    return {r["session"]["id"] for r in results}


def test_search_sessions_phrase_vs_terms(tmp_path):
    IndexBuilder(tmp_path).build_index(
        [
            _session("adjacent", "we need the exact phrase here"),
            _session("scattered", "an exact match of some other phrase"),
        ],
        {},
    )
    assert _ids(search_sessions(tmp_path, '"exact phrase"')) == {"adjacent"}
    assert _ids(search_sessions(tmp_path, "exact phrase")) == {"adjacent", "scattered"}


def test_search_sessions_phrase_with_scope(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("adjacent", "the exact phrase here")], {})
    results = search_sessions(tmp_path, '"exact phrase"', scope="user_only")
    assert _ids(results) == {"adjacent"}


def _chat(sid, *turns):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title="T",
        messages=[
            UnifiedMessage(role=role, content=text, timestamp=datetime(2026, 1, 1))
            for role, text in turns
        ],
    )


def test_scoped_phrase_requires_adjacency_inside_one_role_message(tmp_path):
    """The phrase is in the assistant text; the user text only has the words apart."""
    IndexBuilder(tmp_path).build_index(
        [
            _chat(
                "assistant-has-it",
                (Role.USER, "an exact match of some other phrase"),
                (Role.ASSISTANT, "yes the exact phrase is documented"),
            ),
            _chat("user-has-it", (Role.USER, "what is the exact phrase here")),
        ],
        {},
    )

    results = search_sessions(tmp_path, '"exact phrase"', scope="user_only")

    assert _ids(results) == {"user-has-it"}


def test_scoped_wildcard_characters_match_literally(tmp_path):
    """``%`` is a word separator for FTS, never a LIKE wildcard (#199).

    The role check runs the FTS engine now, so ``50%`` and ``50`` are the same
    query: both match the messages holding the token ``50`` and neither matches
    a message without it. What must never happen — a ``%`` matching unrelated
    text — is pinned by ``unrelated`` staying out, and the scoped result must be
    identical to the unscoped FTS result for role-matching content (the old
    ``LIKE '%50\\%%'`` check dropped ``fifty-bucks``, which the query itself had
    matched).
    """
    IndexBuilder(tmp_path).build_index(
        [
            _chat("literal", (Role.USER, "a 50% discount today")),
            _chat("fifty-bucks", (Role.USER, "50 bucks only")),
            _chat("unrelated", (Role.USER, "discount only")),
        ],
        {},
    )

    scoped = _ids(search_sessions(tmp_path, "50%", scope="user_only"))
    assert scoped == {"literal", "fifty-bucks"}
    assert _ids(search_sessions(tmp_path, "50", scope="user_only")) == scoped
    assert scoped == _ids(search_sessions(tmp_path, "50%"))
    assert "unrelated" not in scoped


def test_nul_in_query_is_neutralised(tmp_path):
    assert build_fts_query("a\x00b") == '"a"* "b"*'
    IndexBuilder(tmp_path).build_index([_session("a", "alpha beta")], {})
    assert _ids(search_sessions(tmp_path, "alpha\x00beta")) == {"a"}


@pytest.mark.parametrize(
    ("query", "phrase"),
    [('"a b"', "a b"), ('  "a b"  ', "a b"), ("a b", None), ('""', None), ('"  "', None)],
)
def test_phrase_of(query, phrase):
    assert phrase_of(query) == phrase


@pytest.mark.parametrize(
    ("query", "expected"),
    [('a"b', {"a"}), ('"foo', {"a"}), ('a "b" c', {"a"}), ('"', set()), ('""', set())],
)
def test_search_sessions_odd_quotes_do_not_raise(tmp_path, query, expected):
    """Stray quotes never raise; the terms they contain are still matched."""
    IndexBuilder(tmp_path).build_index([_session("a", "foo a b c hello world")], {})
    assert _ids(search_sessions(tmp_path, query)) == expected


def test_recall_memory_phrase_vs_terms(tmp_path):
    add_memory(tmp_path, "fact", "Adjacent", "remember the exact phrase always")
    add_memory(tmp_path, "fact", "Scattered", "an exact match of another phrase")
    assert [e.title for e in recall_memory(tmp_path, '"exact phrase"')] == ["Adjacent"]
    assert {e.title for e in recall_memory(tmp_path, "exact phrase", semantic=False)} == {
        "Adjacent",
        "Scattered",
    }
    add_memory(tmp_path, "fact", "Stray", "alpha beta gamma")
    assert [e.title for e in recall_memory(tmp_path, 'alpha"beta', semantic=False)] == ["Stray"]
    assert recall_memory(tmp_path, '"', semantic=False) == []


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    with web.app.test_client() as c:
        yield c


@pytest.mark.parametrize("route", ["/api/search", "/api/v1/search"])
def test_web_search_accepts_phrase_and_rejects_unbalanced(client, monkeypatch, route):
    seen: list = []

    def fake_search_index(q, **kwargs):
        seen.append(q)
        return []

    monkeypatch.setattr(web, "search_index", fake_search_index)

    ok = client.get(f"{route}?q=%22exact+phrase%22")
    assert ok.status_code == 200
    assert seen == ['"exact phrase"']

    bad = client.get(f"{route}?q=%22unbalanced")
    assert bad.status_code == 400
    assert bad.get_json() == {"error": "Invalid search query"}
    assert seen == ['"exact phrase"']


def test_web_memory_page_phrase_and_unbalanced(client, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "INDEX_PATH", tmp_path / "index.json")
    add_memory(tmp_path, "fact", "Adjacent", "remember the exact phrase always")
    add_memory(tmp_path, "fact", "Scattered", "an exact match of another phrase")
    ok = client.get("/memory?q=%22exact+phrase%22")
    assert ok.status_code == 200
    html = ok.get_data(as_text=True)
    assert "Adjacent" in html and "Scattered" not in html
    bad = client.get("/memory?q=%22unbalanced")
    assert bad.status_code == 400


def _call(server, name, arguments):
    return asyncio.run(server.tools[name]["handler"](arguments))


def test_mcp_memory_recall_phrase_and_unbalanced(tmp_path):
    server = mcp.create_server()
    server.output_dir = tmp_path
    add_memory(tmp_path, "fact", "Adjacent", "remember the exact phrase always")
    add_memory(tmp_path, "fact", "Scattered", "an exact match of another phrase")
    payload = json.loads(_call(server, "memory_recall", {"query": '"exact phrase"'}))
    assert [m["title"] for m in payload["memories"]] == ["Adjacent"]
    assert _call(server, "memory_recall", {"query": '"unbalanced'}) == "Invalid search query."


def test_mcp_search_history_rejects_unbalanced(tmp_path):
    server = mcp.create_server()
    server.output_dir = tmp_path
    result = _call(server, "search_history", {"query": '"unbalanced'})
    assert result == "Invalid search query parameter."
