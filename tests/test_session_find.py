"""In-session find API and detail-page find bar (B5.7a)."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web
from lore.interfaces.session_find import find_session_matches, visible_text_nodes_from_html

SESSION_ID = "abc12345-def6-7890-abcd-ef1234567890"
NEEDLE = "zzfindneedlezz"


def _meta(session_id: str = SESSION_ID) -> dict:
    return {
        "id": session_id,
        "tool": "claude-code",
        "title": "Find session",
        "created": "2026-01-01T12:00:00",
        "updated": "2026-01-01T12:00:00",
        "project": "/tmp/proj",
        "messages": 0,
        "prompts": 0,
    }


def _make_session(
    pair_count: int,
    *,
    session_id: str = SESSION_ID,
    mutate_last_assistant: str | None = None,
    extra_messages: list[UnifiedMessage] | None = None,
) -> UnifiedSession:
    messages: list[UnifiedMessage] = list(extra_messages or [])
    ts = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(pair_count):
        messages.append(UnifiedMessage(role=Role.USER, content=f"Question {i}", timestamp=ts))
        body = (
            mutate_last_assistant
            if i == pair_count - 1 and mutate_last_assistant
            else f"Answer {i}"
        )
        messages.append(UnifiedMessage(role=Role.ASSISTANT, content=body, timestamp=ts))
    return UnifiedSession(
        session_id=session_id,
        tool=Tool.CLAUDE_CODE,
        title="Find session",
        messages=messages,
        created_at=ts,
        last_updated=ts,
        project_path="/tmp/proj",
    )


def _enrich(session: UnifiedSession) -> UnifiedSession:
    web.enrich_session_for_detail(
        session,
        _meta(session.session_id),
        web.format_message_content,
        web.format_tool_calls,
        web.format_thinking,
        noise_rules=None,
    )
    return session


@pytest.fixture()
def find_client(monkeypatch):
    """Client with a 120-pair session; needle only on the last assistant turn."""

    def _factory(session: UnifiedSession | None = None, session_id: str = SESSION_ID):
        sess = session or _enrich(
            _make_session(120, session_id=session_id, mutate_last_assistant=f"Tail {NEEDLE} here")
        )

        def _fake_index():
            return {"sessions": [_meta(session_id)], "stats": {}}

        def _fake_resolve(sid, force_live=False):
            if sid != session_id:
                return None
            return sess, []

        monkeypatch.setattr(web, "load_index", _fake_index)
        monkeypatch.setattr(web, "_enriched_session_for_detail", _fake_resolve)
        monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: set())
        return web.app.test_client()

    return _factory


def test_find_hit_on_later_page(find_client):
    client = find_client()
    r = client.get(f"/api/session/{SESSION_ID}/find?q={NEEDLE}")
    assert r.status_code == 200
    assert r.content_type.startswith("application/json")
    data = r.get_json()
    assert data["total"] >= 1
    hit = data["matches"][0]
    assert hit["page"] == 3
    assert hit["role"] == "assistant"
    assert hit["index"] == 119 * 2


def test_find_case_insensitive(find_client):
    client = find_client()
    data = client.get(f"/api/session/{SESSION_ID}/find?q={NEEDLE.upper()}").get_json()
    assert data["total"] >= 1


def test_find_snippet_plain_text_no_script_in_json(find_client):
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _enrich(
        UnifiedSession(
            session_id=SESSION_ID,
            tool=Tool.CLAUDE_CODE,
            title="x",
            created_at=ts,
            last_updated=ts,
            project_path="/p",
            messages=[
                UnifiedMessage(Role.USER, f"Hello {NEEDLE}", ts),
                UnifiedMessage(
                    Role.ASSISTANT,
                    f"See <script>alert(1)</script> and {NEEDLE}",
                    ts,
                ),
            ],
        )
    )
    client = find_client(session)
    raw = client.get(f"/api/session/{SESSION_ID}/find?q={NEEDLE}").get_data(as_text=True)
    assert "<script" not in raw.lower()
    data = json.loads(raw)
    assert NEEDLE in data["matches"][0]["snippet"]


@pytest.mark.parametrize("q", ["", "a", "x" * 101])
def test_find_query_length_400(find_client, q):
    client = find_client()
    assert client.get(f"/api/session/{SESSION_ID}/find?q={q}").status_code == 400


def test_find_404_unknown(find_client):
    client = find_client()
    assert client.get("/api/session/unknown-not-in-index/find?q=ab").status_code == 404


def test_find_404_tombstoned(find_client, monkeypatch):
    client = find_client()
    monkeypatch.setattr(web, "load_deleted_session_ids", lambda _path: {SESSION_ID})
    assert client.get(f"/api/session/{SESSION_ID}/find?q=ab").status_code == 404


def test_find_invalid_session_id_rejected(find_client):
    client = find_client()
    assert client.get("/api/session/badid!!!/find?q=ab").status_code == 400


def test_find_truncated_at_200_matches():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    messages: list[UnifiedMessage] = []
    for i in range(250):
        messages.append(UnifiedMessage(Role.USER, f"{NEEDLE} {i}", ts))
        messages.append(UnifiedMessage(Role.ASSISTANT, "ok", ts))
    session = _enrich(
        UnifiedSession(
            session_id=SESSION_ID,
            tool=Tool.CLAUDE_CODE,
            title="x",
            created_at=ts,
            last_updated=ts,
            project_path="/p",
            messages=messages,
        )
    )
    data = find_session_matches(session, NEEDLE, per_page=web.MESSAGES_PER_PAGE)
    assert data["truncated"] is True
    assert data["total"] == 250
    assert len(data["matches"]) == 200


def test_find_counts_tool_card_text_like_the_dom():
    # Tool cards render inside `.prose`, so the client marks their text; the server must count it.
    ts = datetime(2026, 1, 1, 12, 0, 0)
    huge = "x" * 25_000 + NEEDLE
    session = _enrich(
        UnifiedSession(
            session_id=SESSION_ID,
            tool=Tool.CLAUDE_CODE,
            title="x",
            created_at=ts,
            last_updated=ts,
            project_path="/p",
            messages=[
                UnifiedMessage(Role.USER, "go", ts),
                UnifiedMessage(
                    Role.ASSISTANT,
                    "",
                    ts,
                    tool_calls=[{"tool": "bash", "input": {}, "output": huge, "status": "ok"}],
                ),
            ],
        )
    )
    data = find_session_matches(session, NEEDLE, per_page=40)
    assert data["total"] == 1
    assert data["matches"][0]["message"] == 1


def test_find_reasoning_excluded_by_default():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _enrich(
        UnifiedSession(
            session_id=SESSION_ID,
            tool=Tool.CLAUDE_CODE,
            title="x",
            created_at=ts,
            last_updated=ts,
            project_path="/p",
            messages=[
                UnifiedMessage(Role.USER, "hi", ts),
                UnifiedMessage(
                    Role.ASSISTANT,
                    "visible",
                    ts,
                    reasoning=f"secret {NEEDLE} thought",
                ),
            ],
        )
    )
    assert find_session_matches(session, NEEDLE)["total"] == 0
    assert find_session_matches(session, NEEDLE, include_reasoning=True)["total"] == 1


def test_find_matches_ordered_by_index():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _enrich(
        UnifiedSession(
            session_id=SESSION_ID,
            tool=Tool.CLAUDE_CODE,
            title="x",
            created_at=ts,
            last_updated=ts,
            project_path="/p",
            messages=[
                UnifiedMessage(Role.USER, f"{NEEDLE} one", ts),
                UnifiedMessage(Role.ASSISTANT, "a", ts),
                UnifiedMessage(Role.USER, f"{NEEDLE} two", ts),
                UnifiedMessage(Role.ASSISTANT, "b", ts),
            ],
        )
    )
    data = find_session_matches(session, NEEDLE, per_page=40)
    indices = [m["index"] for m in data["matches"]]
    assert indices == sorted(indices)


def test_detail_page_find_bar_markup(find_client):
    html = find_client().get(f"/session/{SESSION_ID}").get_data(as_text=True)
    assert 'aria-label="Find in this session"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert "sessionFindToggle" in html or 'id="sessionFindToggle"' in html
    assert "Prev" in html and "Next" in html
    scripts = re.findall(r"<script[^>]*nonce=[^>]*>(.*?)</script>", html, re.DOTALL)
    joined = "\n".join(scripts)
    assert "ctrlKey" in joined and "metaKey" in joined and "altKey" in joined
    assert "isContentEditable" in joined
    assert "Escape" in joined
    assert "new RegExp(" not in joined
    if "function applyFindHighlights" in joined:
        fn = joined.split("function applyFindHighlights", 1)[1]
        fn = fn.split("function ", 1)[0]
        assert "innerHTML" not in fn


def test_matching_uses_lower_so_server_and_browser_agree():
    from lore.interfaces.session_find import _occurrence_positions

    assert _occurrence_positions("Straße xx", "ss") == []
    assert _occurrence_positions("Straße SS", "ss") == [7]


def test_scan_budget_marks_the_result_truncated(monkeypatch):
    import lore.interfaces.session_find as sf

    class Msg:
        def __init__(self, content):
            self.content = content
            self.reasoning = None
            self.index = 1
            self.formatted_content = content
            self.formatted_reasoning = ""

    pairs = [{"user": Msg("needle " + "x" * 50), "responses": []} for _ in range(20)]

    class Session:
        pass

    session = Session()
    session.pairs = pairs
    monkeypatch.setattr(sf, "_MAX_TOTAL_SCAN", 120)

    result = sf.find_session_matches(session, "needle")

    assert result["truncated"] is True
    assert 0 < result["total"] < 20


def test_client_script_discards_stale_responses_and_discloses_truncation():
    from pathlib import Path

    page = Path("lore/templates/session.html").read_text(encoding="utf-8")
    assert "mySeq !== requestSeq" in page
    assert "requestSeq += 1" in page
    assert "payload.truncated" in page


def _session_with_messages(messages: list[UnifiedMessage]) -> UnifiedSession:
    ts = datetime(2026, 1, 1, 12, 0, 0)
    return _enrich(
        UnifiedSession(
            session_id=SESSION_ID,
            tool=Tool.CLAUDE_CODE,
            title="x",
            created_at=ts,
            last_updated=ts,
            project_path="/p",
            messages=messages,
        )
    )


def test_find_visible_markdown_link_and_plain_needle():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", ts),
            UnifiedMessage(
                Role.ASSISTANT,
                "[needle](https://needle.example) needle",
                ts,
            ),
        ]
    )
    data = find_session_matches(session, "needle", per_page=40)
    assert data["total"] == 2
    occ = [m["occurrence"] for m in data["matches"]]
    assert occ == [0, 1]
    assert all(m["message"] == 1 for m in data["matches"])


def test_find_needle_in_url_only_not_visible():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", ts),
            UnifiedMessage(Role.ASSISTANT, "[click](https://needle.example/path)", ts),
        ]
    )
    assert find_session_matches(session, "needle", per_page=40)["total"] == 0


def test_find_fenced_code_counts_each_occurrence():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    body = "needle intro\n```\nneedle\n```\nout `needle` tail"
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", ts),
            UnifiedMessage(Role.ASSISTANT, body, ts),
        ]
    )
    data = find_session_matches(session, "needle", per_page=40)
    assert data["total"] == 3


def test_find_html_entity_decoded_visible_text():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", ts),
            UnifiedMessage(Role.ASSISTANT, "prefix &amp;needle suffix", ts),
        ]
    )
    data = find_session_matches(session, "needle", per_page=40)
    assert data["total"] == 1


def test_find_distinct_snippets_per_occurrence():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", ts),
            UnifiedMessage(
                Role.ASSISTANT,
                f"alpha {'x' * 80} {NEEDLE} beta {'y' * 80} {NEEDLE} gamma",
                ts,
            ),
        ]
    )
    data = find_session_matches(session, NEEDLE, per_page=40)
    assert data["total"] == 2
    snippets = [m["snippet"] for m in data["matches"]]
    assert snippets[0] != snippets[1]
    assert data["matches"][0]["occurrence"] == 0
    assert data["matches"][1]["occurrence"] == 1


def test_find_occurrence_restarts_per_message():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, f"{NEEDLE} user", ts),
            UnifiedMessage(Role.ASSISTANT, f"{NEEDLE} assistant", ts),
        ]
    )
    data = find_session_matches(session, NEEDLE, per_page=40)
    assert [(m["message"], m["occurrence"]) for m in data["matches"]] == [(0, 0), (1, 0)]


def test_find_assistant_response_message_positions():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", ts),
            UnifiedMessage(Role.ASSISTANT, f"{NEEDLE} first", ts),
            UnifiedMessage(Role.ASSISTANT, f"{NEEDLE} second", ts),
        ]
    )
    data = find_session_matches(session, NEEDLE, per_page=40)
    assert [m["message"] for m in data["matches"]] == [1, 2]


def test_find_matches_ordered_by_pair_message_occurrence():
    ts = datetime(2026, 1, 1, 12, 0, 0)
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, f"{NEEDLE}a", ts),
            UnifiedMessage(Role.ASSISTANT, "x", ts),
            UnifiedMessage(Role.USER, f"{NEEDLE}b", ts),
            UnifiedMessage(Role.ASSISTANT, f"{NEEDLE}c {NEEDLE}d", ts),
        ]
    )
    data = find_session_matches(session, NEEDLE, per_page=40)
    keys = [(m["index"], m["message"], m["occurrence"]) for m in data["matches"]]
    assert keys == sorted(keys)


def test_find_dom_visible_text_matches_server(find_client):
    session = _session_with_messages(
        [
            UnifiedMessage(Role.USER, "hi", datetime(2026, 1, 1, 12, 0, 0)),
            UnifiedMessage(
                Role.ASSISTANT,
                "[needle](https://x.example) needle",
                datetime(2026, 1, 1, 12, 0, 0),
            ),
        ]
    )
    client = find_client(session)
    api = client.get("/api/session/{}/find?q=needle".format(SESSION_ID)).get_json()
    page = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)

    prose_re = re.compile(
        r'<div class="prose[^"]*" data-find-index="(\d+)" data-find-message="(\d+)">'
        r"(.*?)</div>\s*</div>\s*</div>",
        re.DOTALL,
    )

    def count_needle_nodes(html_chunk: str) -> int:
        total_hits = 0
        for node in visible_text_nodes_from_html(html_chunk):
            lower = node.lower()
            start = 0
            while True:
                idx = lower.find("needle", start)
                if idx < 0:
                    break
                total_hits += 1
                start = idx + len("needle")
        return total_hits

    server_by_key: dict[tuple[str, int], int] = {}
    for m in api["matches"]:
        key = (str(m["index"]), int(m["message"]))
        server_by_key[key] = server_by_key.get(key, 0) + 1

    for match in prose_re.finditer(page):
        idx, msg, inner = match.group(1), match.group(2), match.group(3)
        dom_count = count_needle_nodes(inner)
        key = (idx, int(msg))
        assert dom_count == server_by_key.get(key, 0)


def test_client_script_tags_marks_by_index_message_occurrence():
    from pathlib import Path

    page = Path("lore/templates/session.html").read_text(encoding="utf-8")
    assert "data-find-occurrence" in page
    assert "data-find-message" in page
    assert "match.occurrence" in page
    assert "match.message" in page
