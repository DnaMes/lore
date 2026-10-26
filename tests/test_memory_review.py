"""Candidate review queue: CLI and web (#197).

Memories written by an agent can arrive as `candidate` — stored, but not part of
recall until a human accepts them. This covers the review command, the Candidates
tab and the accept/reject route, including that a rejection keeps the row.
"""

from __future__ import annotations

import argparse
import json

import pytest

from lore.interfaces import web, web_data
from lore.storage import add_memory, get_memory, list_memory, recall_memory, set_memory_status


@pytest.fixture()
def store(tmp_path):
    """One confirmed memory and two candidates."""
    add_memory(tmp_path, kind="fact", title="Confirmed fact", body="the build is green")
    add_memory(
        tmp_path,
        kind="lesson",
        title="Candidate lesson",
        body="pin the tool version",
        status="candidate",
    )
    add_memory(
        tmp_path,
        kind="decision",
        title="Candidate decision",
        body="use sqlite for the index",
        status="candidate",
    )
    return tmp_path


def _cli(args, store):
    import lore_cli

    return lore_cli.cmd_memory(
        argparse.Namespace(memory_action="review", output_dir=str(store), **args)
    )


# ---------------------------------------------------------------------------
# storage contract
# ---------------------------------------------------------------------------


def test_candidates_are_hidden_from_the_default_list(store):
    titles = [entry.title for entry in list_memory(store)]
    assert titles == ["Confirmed fact"]


def test_candidates_are_listed_on_request(store):
    candidates = list_memory(store, status="candidate")
    assert {entry.title for entry in candidates} == {"Candidate lesson", "Candidate decision"}


def test_candidates_are_not_recalled(store):
    assert all(hit.title != "Candidate lesson" for hit in recall_memory(store, "pin tool version"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_lists_candidates(store, capsys):
    _cli({"target": None, "ids": [], "kind": None, "json": True}, store)
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert {entry["title"] for entry in payload["candidates"]} == {
        "Candidate lesson",
        "Candidate decision",
    }


def test_cli_accepts_a_candidate(store, capsys):
    _cli({"target": "accept", "ids": ["2"], "kind": None, "json": True}, store)
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == [2]
    assert payload["status"] == "confirmed"
    assert get_memory(store, 2).status == "confirmed"
    assert "Candidate lesson" in [entry.title for entry in list_memory(store)]


def test_cli_rejects_a_candidate_and_keeps_the_row(store, capsys):
    _cli({"target": "reject", "ids": ["3"], "kind": None, "json": True}, store)
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == [3]
    rejected = get_memory(store, 3)
    assert rejected is not None and rejected.status == "rejected"
    assert "Candidate decision" not in [entry.title for entry in list_memory(store)]


def test_cli_reports_ids_that_are_not_candidates(store, capsys):
    _cli({"target": "accept", "ids": ["1", "999"], "kind": None, "json": True}, store)
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == []
    assert payload["unchanged"] == [1, 999]


def test_rejection_is_reversible(store):
    set_memory_status(store, 3, "rejected", from_status="candidate")
    assert set_memory_status(store, 3, "confirmed", from_status="rejected") is True
    assert get_memory(store, 3).status == "confirmed"


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(store, monkeypatch):
    for module in (web, web_data):
        monkeypatch.setattr(module, "OUTPUT_DIR", store)
        monkeypatch.setattr(module, "INDEX_PATH", store / "index.json")
    with web.app.test_client() as test_client:
        yield test_client


def test_candidates_tab_lists_them_with_actions(client):
    page = client.get("/memory?status=candidate").get_data(as_text=True)
    assert "Candidate lesson" in page
    assert "Confirmed fact" not in page
    assert 'name="action" value="accept"' in page
    assert 'name="action" value="reject"' in page
    assert "Candidates (2)" in page


def test_default_tab_hides_candidates(client):
    page = client.get("/memory").get_data(as_text=True)
    assert "Confirmed fact" in page
    assert "Candidate lesson" not in page
    assert "Confirmed (1)" in page


def test_accept_route_confirms_and_redirects(client, store):
    response = client.post("/memory/2/review", data={"action": "accept"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/memory?status=candidate")
    assert get_memory(store, 2).status == "confirmed"


def test_reject_route_rejects_the_candidate(client, store):
    response = client.post("/memory/3/review", data={"action": "reject"})
    assert response.status_code == 302
    assert get_memory(store, 3).status == "rejected"


def test_invalid_action_is_rejected(client, store):
    assert client.post("/memory/2/review", data={"action": "maybe"}).status_code == 400
    assert get_memory(store, 2).status == "candidate"


def test_review_requires_a_local_origin(client):
    """The mutating route goes through the same origin check as the others."""
    response = client.post(
        "/memory/2/review",
        data={"action": "accept"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
