"""Review queue for memory candidates (#197 B6.3): storage, CLI, web, MCP guard."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

import lore.storage.embeddings as emb
import lore_cli
from lore.interfaces import mcp, web, web_data
from lore.storage import (
    add_memory,
    count_candidates,
    list_candidates,
    list_memory,
    recall_memory,
)
from lore.storage.memory import set_memory_status
from lore.storage.schema import open_connection
from lore.storage.writer import v2_db_path


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed_text", lambda _t: None)
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", tmp_path)
    with web.app.test_client() as c:
        yield c, tmp_path


def _metadata_json(tmp_path, memory_id: int) -> dict:
    conn = open_connection(v2_db_path(tmp_path))
    try:
        raw = conn.execute(
            "SELECT metadata_json FROM memory WHERE id = ?", (memory_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_list_candidates_oldest_first_limit_and_scope(tmp_path):
    a = add_memory(tmp_path, "lesson", "first", "body a", status="candidate")
    c = add_memory(tmp_path, "lesson", "third", "body c", status="candidate")
    b = add_memory(
        tmp_path, "lesson", "second", "body b", status="candidate", scope_project="proj-x"
    )
    ids = [e.id for e in list_candidates(tmp_path, limit=2)]
    assert ids == [a, c]
    scoped = list_candidates(tmp_path, scope_project="proj-x")
    assert [e.id for e in scoped] == [b]


def test_list_candidates_excludes_non_queue_rows(tmp_path):
    confirmed = add_memory(tmp_path, "note", "ok", "body")
    candidate = add_memory(tmp_path, "note", "wait", "body", status="candidate")
    rejected = add_memory(tmp_path, "note", "no", "body", status="candidate")
    set_memory_status(tmp_path, rejected, "rejected")
    old = add_memory(tmp_path, "note", "old", "body")
    add_memory(tmp_path, "note", "new", "replacement", supersedes=old)
    assert [e.id for e in list_candidates(tmp_path)] == [candidate]
    assert confirmed not in {e.id for e in list_candidates(tmp_path)}
    assert rejected not in {e.id for e in list_candidates(tmp_path)}


def test_count_candidates(tmp_path):
    add_memory(tmp_path, "note", "a", "b", status="candidate")
    add_memory(tmp_path, "note", "c", "d", status="candidate")
    add_memory(tmp_path, "note", "e", "f")
    assert count_candidates(tmp_path) == 2


def test_set_memory_status_reject_reason_sanitized_and_capped(tmp_path):
    mid = add_memory(
        tmp_path,
        "lesson",
        "t",
        "b",
        status="candidate",
        metadata={"keep": True},
    )
    reason = "  bad\x00idea " + "x" * 300
    assert set_memory_status(tmp_path, mid, "rejected", reason=reason) is True
    meta = _metadata_json(tmp_path, mid)
    assert meta["keep"] is True
    assert len(meta["reject_reason"]) == 280
    assert "\x00" not in meta["reject_reason"]


def test_set_memory_status_reason_ignored_for_confirmed(tmp_path):
    mid = add_memory(tmp_path, "note", "t", "b", status="candidate")
    assert set_memory_status(tmp_path, mid, "confirmed", reason="ignored") is True
    assert _metadata_json(tmp_path, mid) == {}


def test_rejected_row_stays_for_dedupe(tmp_path):
    body = "unique lesson body for dedupe"
    mid = add_memory(tmp_path, "lesson", "title", body, status="candidate")
    set_memory_status(tmp_path, mid, "rejected")
    conn = open_connection(v2_db_path(tmp_path))
    try:
        rows = conn.execute("SELECT body, status FROM memory").fetchall()
    finally:
        conn.close()
    assert any(r["body"] == body and r["status"] == "rejected" for r in rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_memory_review_text_and_json(tmp_path, capsys):
    mid = add_memory(
        tmp_path,
        "decision",
        "Ship it",
        "line one\nline two",
        author="lore-improve",
        status="candidate",
        source_session="sess-abc",
    )
    lore_cli.cmd_memory(
        Namespace(
            output_dir=str(tmp_path),
            memory_action="review",
            project=None,
            limit=20,
            json=False,
        )
    )
    out = capsys.readouterr().out
    assert f"#{mid}" in out and "Ship it" in out and "sess-abc" in out
    assert "line one line two" in out or "line one" in out

    lore_cli.cmd_memory(
        Namespace(
            output_dir=str(tmp_path),
            memory_action="review",
            project=None,
            limit=20,
            json=True,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == mid
    assert payload[0]["kind"] == "decision"


def test_cli_accept_reject_mixed_exit_code(tmp_path, capsys):
    ok = add_memory(tmp_path, "note", "ok", "b", status="candidate")
    confirmed = add_memory(tmp_path, "note", "done", "b")
    missing = 99999
    with pytest.raises(SystemExit) as exc:
        lore_cli.cmd_memory(
            Namespace(
                output_dir=str(tmp_path),
                memory_action="accept",
                memory_ids=[ok, confirmed, missing],
                reason=None,
            )
        )
    assert exc.value.code == 1
    err = capsys.readouterr()
    assert f"accepted #{ok}" in err.out
    assert f"skipped #{confirmed}" in err.out
    assert f"not found #{missing}" in err.out


def test_cli_reject_with_reason(tmp_path, capsys):
    mid = add_memory(tmp_path, "note", "t", "b", status="candidate")
    lore_cli.cmd_memory(
        Namespace(
            output_dir=str(tmp_path),
            memory_action="reject",
            memory_ids=[mid],
            reason="too vague",
        )
    )
    assert f"rejected #{mid}" in capsys.readouterr().out
    assert _metadata_json(tmp_path, mid)["reject_reason"] == "too vague"


def test_cli_accept_makes_memory_visible(tmp_path, capsys):
    mid = add_memory(tmp_path, "note", "Visible", "zebra stripe", status="candidate")
    lore_cli.cmd_memory(
        Namespace(
            output_dir=str(tmp_path),
            memory_action="accept",
            memory_ids=[mid],
            reason=None,
        )
    )
    capsys.readouterr()
    assert [e.id for e in list_memory(tmp_path)] == [mid]
    assert [e.id for e in recall_memory(tmp_path, "zebra", semantic=False)] == [mid]


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def test_candidates_tab_lists_and_count_badge(client):
    c, tmp_path = client
    add_memory(tmp_path, "note", "Cand", "body", status="candidate")
    add_memory(tmp_path, "note", "Done", "body")
    body = c.get("/memory?tab=candidates").get_data(as_text=True)
    assert "Candidates (1)" in body
    assert "Cand</h2>" in body
    assert "Done</h2>" not in body


def test_accept_post_redirects_and_confirms(client):
    c, tmp_path = client
    mid = add_memory(tmp_path, "note", "Approve me", "body", status="candidate")
    resp = c.post(f"/memory/{mid}/accept", headers={"Origin": "http://localhost"})
    assert resp.status_code == 302
    assert "tab=candidates" in resp.location
    assert "Approve me" not in c.get("/memory?tab=candidates").get_data(as_text=True)
    assert "Approve me" in c.get("/memory").get_data(as_text=True)


def test_reject_post_with_reason(client):
    c, tmp_path = client
    mid = add_memory(tmp_path, "note", "Nope", "body", status="candidate")
    resp = c.post(
        f"/memory/{mid}/reject",
        data={"reason": "not useful"},
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 302
    assert _metadata_json(tmp_path, mid)["reject_reason"] == "not useful"


def test_accept_reject_cross_origin_like_delete(client):
    c, tmp_path = client
    mid = add_memory(tmp_path, "note", "Protected", "body", status="candidate")
    for path in ("accept", "reject"):
        resp = c.post(
            f"/memory/{mid}/{path}",
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 403
    assert "Protected" in c.get("/memory?tab=candidates").get_data(as_text=True)


def test_accept_unknown_404_confirmed_409(client):
    c, tmp_path = client
    confirmed = add_memory(tmp_path, "note", "x", "y")
    assert c.post("/memory/424242/accept", headers={"Origin": "http://localhost"}).status_code == 404
    assert (
        c.post(f"/memory/{confirmed}/accept", headers={"Origin": "http://localhost"}).status_code
        == 409
    )


def test_candidate_html_escaped_on_candidates_tab(client):
    c, tmp_path = client
    add_memory(
        tmp_path,
        "note",
        "<script>x</script>",
        "<img onerror=alert(1)>",
        status="candidate",
    )
    body = c.get("/memory?tab=candidates").get_data(as_text=True)
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


def test_invalid_tab_falls_back_to_memories(client):
    c, tmp_path = client
    add_memory(tmp_path, "note", "Only confirmed", "body")
    body = c.get("/memory?tab=bogus").get_data(as_text=True)
    assert "Only confirmed" in body
    assert "Candidates" in body


def test_get_accept_reject_returns_405(client):
    c, _ = client
    assert c.get("/memory/1/accept").status_code == 405
    assert c.get("/memory/1/reject").status_code == 405


def test_mcp_has_no_accept_reject_tools(tmp_path):
    server = mcp.create_server()
    server.output_dir = tmp_path
    names = {t["name"] for t in server.tools.values()}
    assert "memory_accept" not in names
    assert "memory_reject" not in names
    assert "memory_review" not in names
