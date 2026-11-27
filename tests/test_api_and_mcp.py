import asyncio
import json
from datetime import datetime

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.interfaces import mcp, web


def _build_live_session(session_id: str = "session-1", thread_id: str = "thread-1"):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        last_updated=datetime(2026, 4, 1, 10, 5, 0),
        project_path="/repo/demo",
        thread_id=thread_id,
        title="Claude debugging session",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content="Find the sqlite issue",
                timestamp=datetime(2026, 4, 1, 10, 0, 0),
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="I found the locking bug",
                timestamp=datetime(2026, 4, 1, 10, 1, 0),
            ),
        ],
    )


def _index_payload(session_id: str = "session-1", thread_id: str = "thread-1"):
    return {
        "sessions": [
            {
                "id": session_id,
                "tool": "claude-code",
                "title": "Claude debugging session",
                "created": "2026-04-01T10:00:00",
                "updated": "2026-04-01T10:05:00",
                "project": "/repo/demo",
                "thread_id": thread_id,
                "messages": 2,
                "prompts": 1,
                "export_path": None,
            }
        ],
        "stats": {
            "total_sessions": 1,
            "total_messages": 2,
            "by_tool": {"claude-code": 1},
            "by_project": {"/repo/demo": 1},
        },
    }


def test_api_v1_sessions_and_projects_return_json_shapes(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _index_payload())

    with web.app.test_client() as client:
        sessions_response = client.get("/api/v1/sessions")
        projects_response = client.get("/api/v1/projects")

    assert sessions_response.status_code == 200
    sessions_payload = sessions_response.get_json()
    assert sessions_payload["count"] == 1
    assert sessions_payload["sessions"][0]["id"] == "session-1"
    assert sessions_payload["sessions"][0]["tool"] == "claude-code"

    assert projects_response.status_code == 200
    projects_payload = projects_response.get_json()
    assert projects_payload["count"] == 1
    assert projects_payload["projects"][0]["path"] == "/repo/demo"


def test_api_v1_session_detail_and_messages_use_live_session(monkeypatch):
    live_session = _build_live_session()
    monkeypatch.setattr(web, "load_index", lambda: _index_payload())
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [live_session])

    with web.app.test_client() as client:
        detail_response = client.get("/api/v1/sessions/session-1")
        messages_response = client.get("/api/v1/sessions/session-1/messages")

    assert detail_response.status_code == 200
    detail_payload = detail_response.get_json()
    assert detail_payload["live"] is True
    assert detail_payload["assistant_message_count"] == 1

    assert messages_response.status_code == 200
    messages_payload = messages_response.get_json()
    assert messages_payload["message_count"] == 2
    assert messages_payload["messages"][0]["role"] == "user"
    assert messages_payload["messages"][1]["content"] == "I found the locking bug"


def test_api_v1_thread_detail_returns_serialized_messages(monkeypatch):
    live_session = _build_live_session()
    monkeypatch.setattr(web, "load_index", lambda: _index_payload())
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [live_session])

    with web.app.test_client() as client:
        response = client.get("/api/v1/threads/thread-1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["thread"]["id"] == "thread-1"
    assert payload["thread"]["message_count"] == 2
    assert payload["timeline"][0]["id"] == "session-1"
    assert payload["messages"][0]["role"] == "user"


def _multi_session_index_payload(n: int = 5):
    """Build an index payload with n sessions for pagination tests."""
    sessions = []
    for i in range(1, n + 1):
        sessions.append(
            {
                "id": f"session-{i}",
                "tool": "claude-code",
                "title": f"Session {i}",
                "created": f"2026-04-0{i}T10:00:00",
                "updated": f"2026-04-0{i}T10:05:00",
                "project": "/repo/demo",
                "thread_id": "thread-1",
                "messages": i * 2,
                "prompts": i,
                "export_path": None,
            }
        )
    return {
        "sessions": sessions,
        "stats": {
            "total_sessions": n,
            "total_messages": sum(s["messages"] for s in sessions),
            "by_tool": {"claude-code": n},
            "by_project": {"/repo/demo": n},
        },
        "generated_at": "2026-05-13T17:00:00Z",
    }


def test_api_v1_sessions_pagination_returns_correct_page(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _multi_session_index_payload(5))

    with web.app.test_client() as client:
        # page 1 with per_page=2 should return first 2 sessions
        r1 = client.get("/api/v1/sessions?page=1&per_page=2")
        assert r1.status_code == 200
        p1 = r1.get_json()
        assert p1["total"] == 5
        assert p1["page"] == 1
        assert p1["per_page"] == 2
        assert p1["pages"] == 3
        assert len(p1["sessions"]) == 2

        # page 3 with per_page=2 should return the last session only
        r3 = client.get("/api/v1/sessions?page=3&per_page=2")
        assert r3.status_code == 200
        p3 = r3.get_json()
        assert p3["page"] == 3
        assert len(p3["sessions"]) == 1

        # page beyond range returns empty sessions list (not an error)
        r_over = client.get("/api/v1/sessions?page=99&per_page=2")
        assert r_over.status_code == 200
        assert r_over.get_json()["sessions"] == []


def test_api_v1_sessions_pagination_invalid_params(monkeypatch):
    monkeypatch.setattr(web, "load_index", lambda: _multi_session_index_payload(3))

    with web.app.test_client() as client:
        assert client.get("/api/v1/sessions?page=0").status_code == 400
        assert client.get("/api/v1/sessions?per_page=0").status_code == 400
        assert client.get("/api/v1/sessions?page=abc").status_code == 400
        assert client.get("/api/v1/sessions?per_page=abc").status_code == 400


def test_api_v1_sessions_legacy_limit_still_works(monkeypatch):
    """Existing ?limit= param must continue to work when page/per_page are absent."""
    monkeypatch.setattr(web, "load_index", lambda: _multi_session_index_payload(5))

    with web.app.test_client() as client:
        r = client.get("/api/v1/sessions?limit=3")
        assert r.status_code == 200
        payload = r.get_json()
        # Legacy response shape: count + sessions (no total/page/pages)
        assert "count" in payload
        assert payload["count"] == 3
        assert len(payload["sessions"]) == 3
        assert "page" not in payload


def test_api_v1_index_summary_returns_metadata(monkeypatch):

    def _fake_load_index_summary():
        return {
            "total_sessions": 1234,
            "by_tool": {"claude-code": 800, "cursor": 434},
            "last_updated": "2026-05-13T17:00:00Z",
            "index_size_bytes": 19_000_000,
        }

    monkeypatch.setattr(web, "load_index_summary", _fake_load_index_summary)

    with web.app.test_client() as client:
        r = client.get("/api/v1/index/summary")

    assert r.status_code == 200
    payload = r.get_json()
    assert payload["total_sessions"] == 1234
    assert payload["by_tool"]["claude-code"] == 800
    assert payload["index_size_bytes"] == 19_000_000
    assert payload["last_updated"] == "2026-05-13T17:00:00Z"


def test_load_index_summary_derives_counts_from_cached_index(monkeypatch, tmp_path):
    import json

    from annuli.interfaces import web_data

    # Write a minimal index.json into a temp dir
    index_file = tmp_path / "index.json"
    payload = {
        "generated_at": "2026-05-13T12:00:00Z",
        "stats": {
            "total_sessions": 3,
            "total_messages": 6,
            "by_tool": {"claude-code": 2, "cursor": 1},
            "by_project": {},
        },
        "sessions": [
            {"id": "s1", "tool": "claude-code", "messages": 2},
            {"id": "s2", "tool": "claude-code", "messages": 2},
            {"id": "s3", "tool": "cursor", "messages": 2},
        ],
    }
    index_file.write_text(json.dumps(payload))

    monkeypatch.setattr(web_data, "INDEX_PATH", index_file)
    # No deleted sessions
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: set())
    # Clear the LRU cache so it reads our file
    web_data._load_index_cached.cache_clear()

    summary = web_data.load_index_summary()
    assert summary["total_sessions"] == 3
    assert summary["by_tool"]["claude-code"] == 2
    assert summary["by_tool"]["cursor"] == 1
    assert summary["last_updated"] == "2026-05-13T12:00:00Z"
    assert summary["index_size_bytes"] == index_file.stat().st_size

    # Clean up cache to avoid polluting other tests
    web_data._load_index_cached.cache_clear()


def test_mcp_server_returns_structured_json_for_new_tools(monkeypatch):
    live_session = _build_live_session()
    monkeypatch.setattr(mcp, "load_index", lambda: _index_payload())
    monkeypatch.setattr(mcp, "load_sessions_for_tool", lambda _tool=None: [live_session])

    server = mcp.create_server()

    tools_response = asyncio.run(
        server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )
    tool_names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert "get_session" in tool_names
    assert "get_thread" in tool_names
    assert "list_projects" in tool_names

    session_response = asyncio.run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_session",
                    "arguments": {"session_id": "session-1"},
                },
            }
        )
    )
    session_payload = json.loads(session_response["result"]["content"][0]["text"])
    assert session_payload["id"] == "session-1"
    assert session_payload["live"] is True

    thread_response = asyncio.run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_thread",
                    "arguments": {"thread_id": "thread-1", "include_messages": True},
                },
            }
        )
    )
    thread_payload = json.loads(thread_response["result"]["content"][0]["text"])
    assert thread_payload["thread"]["id"] == "thread-1"
    assert len(thread_payload["messages"]) == 2


def test_mcp_initialize_reports_package_version():
    """serverInfo.version must track annuli.__version__, not a hardcoded literal."""
    import annuli
    from annuli.interfaces import mcp

    server = mcp.create_server()
    response = asyncio.run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
        )
    )
    info = response["result"]["serverInfo"]
    assert info["name"] == "annuli"
    assert info["version"] == annuli.__version__


def test_dev_extra_declares_test_tooling():
    """The [dev] extra must exist and carry the test runner (#setup)."""
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    joined = " ".join(dev)
    assert "pytest" in joined
    assert "pytest-cov" in joined
    assert "ruff" in joined
