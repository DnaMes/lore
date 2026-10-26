"""Search hits carry message evidence, and paging reaches every match (#175).

A session-level hit said "this session matched" without saying where, and the
API answered with at most ten results and a ``count`` that was just the length of
that page. Both are fixed here: each hit names the message it is based on (seq,
role, excerpt, permalink) and marks a shortened excerpt, and `/api/v1/search`
reports a real ``total`` with ``page``/``per_page`` and a ``next_page``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web, web_data
from lore.interfaces.api_payloads import serialize_search_result
from lore.services.index import count_index_hits, search_index
from lore.storage import message_evidence_for_sessions, v2_db_path, write_sessions
from lore.storage.search import session_message_evidence

TERM = "kwblossom"


def _session(session_id: str, *, matches: int = 1, filler: str = "nothing here") -> UnifiedSession:
    """A session whose matching messages are placed late in the transcript."""
    start = datetime(2026, 2, 1, 9, 0, 0)
    messages = []
    for index in range(6):
        body = filler
        if index >= 6 - matches:
            body = f"{filler} and then the {TERM} appears in this message"
        role = Role.USER if index % 2 == 0 else Role.ASSISTANT
        messages.append(
            UnifiedMessage(role=role, content=body, timestamp=start + timedelta(minutes=index))
        )
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=start,
        last_updated=start + timedelta(minutes=len(messages)),
        project_path="/repo/lore",
        title=f"Session {session_id}",
        messages=messages,
    )


def _write(tmp_path, sessions):
    # index.json first, then the store: the v2 staleness guard skips a store
    # whose generated_at stamp is older than the JSON index, so the reverse
    # order would silently disable v2 in these tests.
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": session.session_id,
                        "tool": session.tool.value,
                        "title": session.title,
                        "display_title": session.title,
                        "created": session.created_at.isoformat(),
                        "updated": session.last_updated.isoformat(),
                        "project": session.project_path,
                        "messages": len(session.messages),
                        "prompts": 3,
                    }
                    for session in sessions
                ],
                "stats": {},
            }
        ),
        encoding="utf-8",
    )
    write_sessions(v2_db_path(tmp_path), sessions)
    return index_path


@pytest.fixture()
def one_match(tmp_path):
    _write(tmp_path, [_session("ses_ev")])
    return tmp_path


@pytest.fixture()
def twenty_five(tmp_path):
    sessions = [
        _session(f"ses_{index:02d}", filler=f"filler body number {index}") for index in range(25)
    ]
    index_path = _write(tmp_path, sessions)
    return tmp_path, index_path


# ---------------------------------------------------------------------------
# message evidence
# ---------------------------------------------------------------------------


def test_evidence_names_the_matching_message(one_match):
    evidence = session_message_evidence(one_match, "ses_ev", TERM)
    assert evidence, "no message evidence found"
    assert evidence[0]["seq"] == 5
    assert evidence[0]["role"] == "assistant"
    assert TERM in evidence[0]["excerpt"]


def test_evidence_batch_returns_one_entry_per_session(tmp_path):
    _write(tmp_path, [_session("ses_a"), _session("ses_b")])
    found = message_evidence_for_sessions(tmp_path, ["ses_a", "ses_b", "ses_missing"], TERM)
    assert set(found) == {"ses_a", "ses_b"}


def test_evidence_batch_is_empty_for_an_unknown_term(one_match):
    assert message_evidence_for_sessions(one_match, ["ses_ev"], "nosuchterm") == {}


def test_evidence_marks_a_shortened_excerpt(tmp_path):
    session = _session("ses_long")
    long_body = (
        ("prefix " * 200) + f" and the {TERM} sits deep inside this long message " + ("tail " * 200)
    )
    session.messages[-1].content = long_body
    _write(tmp_path, [session])
    evidence = session_message_evidence(tmp_path, "ses_long", TERM)[0]
    assert evidence["truncated"] is True
    assert evidence["excerpt"]


def test_search_result_carries_evidence_and_permalink(one_match):
    index_path = one_match / "index.json"
    results = search_index(index_path, TERM, set())
    payload = serialize_search_result(results[0])
    evidence = payload["evidence"]
    assert evidence["message_seq"] == 5
    assert evidence["permalink"] == "/session/ses_ev/m/5"
    assert evidence["excerpt_truncated"] is False
    assert TERM in evidence["excerpt"]


# ---------------------------------------------------------------------------
# totals and paging
# ---------------------------------------------------------------------------


def test_count_matches_the_number_of_pages(twenty_five):
    tmp_path, index_path = twenty_five
    assert count_index_hits(index_path, TERM, set()) == 25


def test_offsets_walk_every_match(twenty_five):
    tmp_path, index_path = twenty_five
    seen = []
    for page in range(3):
        results = search_index(index_path, TERM, set(), limit=10, offset=page * 10)
        seen.extend(result["session"]["id"] for result in results)
    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_count_respects_the_filters(twenty_five):
    tmp_path, index_path = twenty_five
    assert count_index_hits(index_path, TERM, set(), project="/repo/none") == 0


def test_count_excludes_deleted_ids(twenty_five):
    tmp_path, index_path = twenty_five
    assert count_index_hits(index_path, TERM, {"ses_00"}) == 24


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(twenty_five, monkeypatch):
    tmp_path, index_path = twenty_five
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
    with web.app.test_client() as test_client:
        yield test_client


def test_api_reports_a_real_total_and_pages(client):
    first = client.get(f"/api/v1/search?q={TERM}&per_page=10&page=1").get_json()
    assert first["total"] == 25
    assert first["total_is_partial"] is False
    assert first["page"] == 1
    assert first["count"] == 10
    assert first["next_page"] == 2
    assert "evidence" in first["results"][0]

    last = client.get(f"/api/v1/search?q={TERM}&per_page=10&page=3").get_json()
    assert last["count"] == 5
    assert "next_page" not in last


def test_api_pages_reach_every_match(client):
    ids = []
    for page in (1, 2, 3):
        payload = client.get(f"/api/v1/search?q={TERM}&per_page=10&page={page}").get_json()
        ids.extend(result["id"] for result in payload["results"])
    assert len(set(ids)) == 25


def test_api_accepts_limit_as_the_old_parameter(client):
    payload = client.get(f"/api/v1/search?q={TERM}&limit=5").get_json()
    assert payload["per_page"] == 5
    assert payload["count"] == 5


def test_api_rejects_a_bad_page(client):
    assert client.get(f"/api/v1/search?q={TERM}&page=0").status_code == 400
    assert client.get(f"/api/v1/search?q={TERM}&page=abc").status_code == 400


def test_api_scoped_search_marks_the_total_partial(client):
    payload = client.get(f"/api/v1/search?q={TERM}&scope=user_only").get_json()
    assert payload["total_is_partial"] is True
    assert "next_page" not in payload


def test_legacy_search_endpoint_carries_evidence(client):
    payload = client.get(f"/api/search?q={TERM}").get_json()
    assert payload
    assert payload[0]["evidence"]["permalink"].startswith("/session/ses_")


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_prints_total_evidence_and_page(twenty_five, monkeypatch, capsys):
    import argparse

    import lore_cli

    tmp_path, index_path = twenty_five
    args = argparse.Namespace(
        query=TERM,
        tool=None,
        project=None,
        tag=None,
        page=2,
        include_automated=False,
        output_dir=str(tmp_path),
    )
    lore_cli.cmd_search(args)
    out = capsys.readouterr().out
    assert "Found 25 results" in out
    assert "page 2" in out
    assert "Evidence: #5" in out
    assert "/session/" in out
