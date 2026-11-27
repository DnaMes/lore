import json

from annuli.search.engine import SearchEngine


def test_search_tool_alias_filters_to_canonical_tool(tmp_path):
    index_path = tmp_path / "index.json"
    payload = {
        "sessions": [
            {
                "id": "g-1",
                "tool": "gemini-cli",
                "project": "/repo/a",
                "title": "Fix parser bug",
                "keywords": ["parser", "bug"],
                "search_text": "fix parser bug",
            },
            {
                "id": "c-1",
                "tool": "codex",
                "project": "/repo/b",
                "title": "Refactor index",
                "keywords": ["index", "refactor"],
                "search_text": "refactor index",
            },
        ],
        "search_index": {},
    }
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    engine = SearchEngine(index_path)
    results = engine.search("parser", tool="gemini")

    assert len(results) == 1
    assert results[0]["session"]["id"] == "g-1"
    assert results[0]["session"]["tool"] == "gemini-cli"
