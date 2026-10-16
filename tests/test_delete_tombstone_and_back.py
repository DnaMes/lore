import json

from lore.interfaces import web, web_data


def test_delete_remembers_tombstone_and_redirects_back(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    deleted_path = tmp_path / "deleted_sessions.json"

    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 1,
                    "total_messages": 1,
                    "by_tool": {"cursor": 1},
                    "by_project": {},
                },
                "sessions": [
                    {
                        "id": "ses-del-1",
                        "tool": "cursor",
                        "messages": 1,
                        "project": None,
                        "export_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "DELETED_SESSIONS_PATH", deleted_path)
    monkeypatch.setenv("LORE_USE_V2", "0")
    web.clear_index_cache()

    with web.app.test_client() as client:
        response = client.post("/session/ses-del-1/delete", data={"next": "/projects?tool=cursor"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/projects?tool=cursor")

    deleted_payload = json.loads(deleted_path.read_text(encoding="utf-8"))
    assert "ses-del-1" in deleted_payload["session_ids"]


def test_load_index_filters_tombstoned_sessions(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    deleted_path = tmp_path / "deleted_sessions.json"

    index_path.write_text(
        json.dumps(
            {
                "stats": {
                    "total_sessions": 2,
                    "total_messages": 2,
                    "by_tool": {"cursor": 2},
                    "by_project": {},
                },
                "sessions": [
                    {
                        "id": "ses-keep",
                        "tool": "cursor",
                        "title": "Keep",
                        "messages": 1,
                        "prompts": 1,
                    },
                    {
                        "id": "ses-delete",
                        "tool": "cursor",
                        "title": "Delete",
                        "messages": 1,
                        "prompts": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    deleted_path.write_text(json.dumps({"session_ids": ["ses-delete"]}), encoding="utf-8")

    monkeypatch.setattr(web, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_data, "load_deleted_session_ids", lambda: {"ses-delete"})
    web.clear_index_cache()

    payload = web.load_index()
    ids = [s["id"] for s in payload["sessions"]]
    assert ids == ["ses-keep"]
    assert payload["stats"]["total_sessions"] == 1
