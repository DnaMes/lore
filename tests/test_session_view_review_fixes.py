"""Regressions from the session-view review (commit bcaeea4).

Four findings, one test each:

1. the code-block collapse button could count a negative number of hidden lines;
2. an empty tool output (``""``) was treated as "no output" and could be
   overwritten by a later result record;
3. the collapsed tool row did not escape ``data-status`` and crashed on a
   ``None`` target;
4. the v2 store stamp opened a SQLite connection on every request and the v2
   session cache ignored the noise-rules version.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.interfaces import web
from lore.interfaces.routes import sessions as sessions_routes
from lore.interfaces.web_formatting import _tool_group_row_html
from lore.interfaces.web_services import _attach_result
from lore.storage.schema import initialise
from lore.storage.writer import v2_db_path, write_sessions

SID = "sess-review-fixes"
T0 = datetime(2026, 11, 7, 19, 58, 0, tzinfo=timezone.utc)
TEMPLATES_DIR = Path(web.__file__).resolve().parent.parent / "templates"


# --- 1. code-block collapse count -----------------------------------------------------


def test_code_block_collapse_count_is_never_negative():
    """The collapse rule must clamp the "N more lines" count (JS clamp in base.html).

    A JS-level assertion: the template must compare the line count with the
    preview before collapsing and clamp the remaining count to at least one.
    """
    source = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    # The old rule collapsed whenever a preview size was set, ignoring the
    # actual number of lines, and computed a negative "more" count.
    assert "previewLines || lines > 24" not in source
    assert "Math.max(1, lines - shown)" in source
    assert "lines > collapseLimit" in source


# --- 2. empty tool output -------------------------------------------------------------


def test_empty_tool_output_is_not_overwritten_by_a_later_result():
    """An empty output is a real output: a later result goes to the next call."""
    calls = [
        {"tool": "Bash", "input": {}, "output": ""},
        {"tool": "Read", "input": {}},
    ]
    assert _attach_result(calls, "second result") is True
    assert calls[0]["output"] == ""
    assert calls[1]["output"] == "second result"


def test_missing_output_key_is_still_filled():
    calls = [{"tool": "Bash", "input": {}}]
    assert _attach_result(calls, "result") is True
    assert calls[0]["output"] == "result"


# --- 3. tool row escaping -------------------------------------------------------------


def test_tool_row_escapes_none_target_and_quotes_in_status():
    row = _tool_group_row_html("Bash", None, 'ok" onmouseover="alert(1)', None, 1)
    assert 'data-status="ok&quot; onmouseover=&quot;alert(1)"' in row
    assert '<span class="tool-row-target"></span>' in row
    # The quotes are escaped, so the attribute cannot be broken out of.
    assert '" onmouseover="' not in row


def test_tool_row_escapes_quotes_in_target():
    row = _tool_group_row_html("Bash", 'say "hi" <b>', "ok", None, 1)
    assert '<span class="tool-row-target">say &quot;hi&quot; &lt;b&gt;</span>' in row


# --- 4. v2 store stamp + noise-rules cache key ----------------------------------------


def _session() -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=SID,
        created_at=T0,
        last_updated=T0 + timedelta(hours=1),
        project_path="/repo",
        messages=[
            UnifiedMessage(Role.USER, "first real prompt", T0),
            UnifiedMessage(Role.ASSISTANT, "The answer.", T0 + timedelta(minutes=1)),
        ],
    )


def _store_client(monkeypatch, tmp_path):
    """A client whose session body only exists in the v2 store."""
    session = _session()
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [session])
    index_payload = {
        "sessions": [
            {
                "id": SID,
                "tool": "claude-code",
                "title": "review fixes",
                "created": T0.isoformat(),
                "updated": (T0 + timedelta(hours=1)).isoformat(),
                "project": "/repo",
                "export_path": str(tmp_path / "projects" / "gone.md"),
                "messages": len(session.messages),
                "prompts": 1,
            }
        ],
        "stats": {},
    }
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    for module in (web,):
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(module, "INDEX_PATH", index_path)
    monkeypatch.setattr(web, "load_index", lambda: index_payload)
    monkeypatch.setattr(web, "load_session_by_id", lambda *a, **k: None)
    monkeypatch.setattr(web, "load_sessions_for_tool", lambda _tool=None: [])
    sessions_routes._STORE_STAMP_MEMO.clear()
    sessions_routes._enriched_session_from_v2_cached.cache_clear()
    return web.app.test_client()


def test_noise_rule_change_invalidates_the_v2_cache(monkeypatch, tmp_path):
    """A noise-rules change must re-render the session, not serve the cached build."""
    from lore.interfaces import web_utils

    rules_v1 = {"default": {"strip_ansi_sequences": True}}
    rules_v2 = {"default": {"strip_ansi_sequences": True}, "claude-code": {"strip_thinking": True}}
    noise_path = tmp_path / "noise_rules.json"
    noise_path.write_text(json.dumps(rules_v1), encoding="utf-8")
    monkeypatch.setattr(web_utils, "NOISE_RULES_PATH", noise_path)
    sessions_routes._NOISE_RULES_VERSION_MEMO.clear()
    client = _store_client(monkeypatch, tmp_path)

    builds = []
    real_build = sessions_routes._build_session_from_v2

    def _counting(*args, **kwargs):
        builds.append(args)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(sessions_routes, "_build_session_from_v2", _counting)

    client.get(f"/session/{SID}")
    client.get(f"/session/{SID}")
    assert len(builds) == 1, "unchanged rules must keep serving the cached build"

    noise_path.write_text(json.dumps(rules_v2), encoding="utf-8")
    client.get(f"/session/{SID}")
    assert len(builds) == 2, "a noise-rules change must invalidate the v2 cache"


def test_store_stamp_memo_does_not_reopen_the_db(monkeypatch, tmp_path):
    """The store stamp is memoised on the DB/WAL file signatures."""
    from lore.storage import schema

    session = _session()
    initialise(v2_db_path(tmp_path))
    write_sessions(v2_db_path(tmp_path), [session])
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    sessions_routes._STORE_STAMP_MEMO.clear()

    opened = []
    real_open = schema.open_connection

    def _counting(path):
        opened.append(path)
        return real_open(path)

    monkeypatch.setattr(schema, "open_connection", _counting)

    first = sessions_routes._v2_store_stamp()
    assert first, "the store has a generated_at stamp"
    assert len(opened) == 1

    second = sessions_routes._v2_store_stamp()
    assert second == first
    assert len(opened) == 1, "unchanged store files must not open the DB again"

    # A changed store file invalidates the memo.
    sessions_routes._STORE_STAMP_MEMO.clear()
    assert sessions_routes._v2_store_stamp()
    assert len(opened) == 2
