"""v2 and the legacy JSON index must agree on a realistic archive (#48, exit criterion 4).

The dual-path migration is only safe while both stores serve the same archive.
This test builds a small but realistic one — several tools, multi-message
sessions, a thread, user tags, a memory, a tombstoned session and a pruned id —
then reads it twice through the *same* code path (``services.index.load_index``)
with the store switched to v2 and to the legacy JSON index, and asserts the two
payloads agree.

Deterministic and offline: every timestamp is fixed, no extractor reads a real
tool directory, and nothing in the comparison depends on wall-clock time.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.services import index as index_service
from lore.storage import add_memory, add_session_tag
from lore.storage.freshness import store_freshness
from lore.storage.reader import load_ignored_session_ids
from lore.storage.retention import record_ignored

# Fields both payloads must carry with the same value. ``source_mtime`` is
# excluded on purpose: it is a stat artifact (the stored ns timestamp versus the
# file's current mtime), not content. ``keywords``/``search_text`` are derived
# only by the JSON builder and are not part of the session payload.
SHARED_FIELDS = (
    "id",
    "tool",
    "project",
    "thread_id",
    "title",
    "created",
    "updated",
    "messages",
    "prompts",
    "prompt_outline",
    "export_path",
    "git_branch",
    "git_commit",
    "source_path",
    "origin",
    "parent_session_id",
    "relation",
    "source_version",
    "parser_version",
)


def _session(
    sid: str,
    tool: Tool,
    *,
    day: int,
    project: str = "/repo/lore",
    thread_id: str | None = None,
    turns: int = 3,
) -> UnifiedSession:
    messages = []
    for index in range(turns):
        role = Role.USER if index % 2 == 0 else Role.ASSISTANT
        messages.append(
            UnifiedMessage(
                role=role,
                content=f"turn {index} of {sid}",
                timestamp=datetime(2026, 3, day, 9 + index),
            )
        )
    return UnifiedSession(
        tool=tool,
        session_id=sid,
        created_at=datetime(2026, 3, day, 9),
        last_updated=datetime(2026, 3, day, 8 + turns),
        project_path=project,
        thread_id=thread_id,
        title=f"Session {sid}",
        messages=messages,
        git_branch="sweep/json-retire",
        git_commit="f6da3d1",
        source_path=f"/home/user/.tool/sessions/{sid}.jsonl",
        source_mtime_ns=1_772_000_000_000_000_000,
        origin="interactive",
    )


def _sessions() -> list[UnifiedSession]:
    parent = _session("claude-1", Tool.CLAUDE_CODE, day=5, thread_id="thread-a", turns=5)
    child = _session("claude-1-sub", Tool.CLAUDE_CODE, day=4, thread_id="thread-a")
    child.origin = "subagent"
    child.parent_session_id = "claude-1"
    child.relation = "subagent"
    return [
        parent,
        child,
        _session("cursor-1", Tool.CURSOR, day=6, project="/repo/other"),
        _session("gemini-1", Tool.GEMINI_CLI, day=3, turns=2),
        _session("codex-1", Tool.CODEX, day=2, turns=4),
        _session("tombstoned", Tool.CLAUDE_CODE, day=1),
        _session("pruned", Tool.CLAUDE_CODE, day=1),
    ]


def _counts(sessions: list[dict]) -> dict:
    by_tool: dict[str, int] = {}
    total_messages = 0
    for session in sessions:
        by_tool[session["tool"]] = by_tool.get(session["tool"], 0) + 1
        total_messages += int(session.get("messages") or 0)
    return {
        "total_sessions": len(sessions),
        "total_messages": total_messages,
        "by_tool": by_tool,
    }


def _content(session: dict) -> dict:
    content = {field: session.get(field) for field in SHARED_FIELDS}
    # ``tokens`` is None in the JSON index when a session reports none, while
    # the store keeps 0 for "no usage data". Same meaning, so compare the number.
    content["tokens"] = int(session.get("tokens") or 0)
    return content


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    """A built archive holding every state the exit criteria name."""
    sessions = _sessions()
    IndexBuilder(tmp_path).build_index(sessions, {})

    add_session_tag(tmp_path, "claude-1", "bookmark")
    add_session_tag(tmp_path, "cursor-1", "reference")
    add_memory(
        tmp_path,
        kind="decision",
        title="Retire the JSON dual-path",
        body="The v2 store is the source of truth; JSON is export-only.",
        scope_project="/repo/lore",
        tags=["architecture"],
        source_session="claude-1",
    )

    index_service.remember_deleted_session_id(
        "tombstoned", tmp_path, tmp_path / "deleted_sessions.json"
    )
    record_ignored(tmp_path, ["pruned"], reason="prune")
    (tmp_path / "ignored.json").write_text('["pruned"]', encoding="utf-8")
    return tmp_path


def _load(tmp_path, monkeypatch, *, use_v2: bool) -> dict:
    monkeypatch.setenv("LORE_USE_V2", "1" if use_v2 else "0")
    index_service.clear_index_cache()
    deleted = index_service.load_deleted_session_ids(tmp_path / "deleted_sessions.json")
    return index_service.load_index(tmp_path / "index.json", tmp_path, deleted)


def test_v2_and_json_payloads_agree_on_the_realistic_archive(archive, monkeypatch):
    tmp_path = archive
    v2_payload = _load(tmp_path, monkeypatch, use_v2=True)
    json_payload = _load(tmp_path, monkeypatch, use_v2=False)
    assert v2_payload["version"] == "2"
    assert json_payload["version"] == "1.0.0"

    # The legacy index applies a prune by rewriting the file; the store hides the
    # id at read time. Apply the same hide to both before comparing.
    ignored = load_ignored_session_ids(tmp_path)
    v2_sessions = [s for s in v2_payload["sessions"] if s["id"] not in ignored]
    json_sessions = [s for s in json_payload["sessions"] if s["id"] not in ignored]

    # Session ids: same set, nothing extra on either side.
    assert {s["id"] for s in v2_sessions} == {s["id"] for s in json_sessions}
    assert {s["id"] for s in v2_sessions} == {
        "claude-1",
        "claude-1-sub",
        "cursor-1",
        "gemini-1",
        "codex-1",
    }

    # Counts agree (stats blocks are computed over the same visible rows). The
    # raw ``stats`` blocks cannot be compared: the JSON one still counts the
    # pruned id, which the store hides at read time.
    assert _counts(v2_sessions) == _counts(json_sessions)

    # Content fields agree per session, order-insensitively (both stores order
    # newest-first today, but the payload contract does not depend on it).
    v2_by_id = {s["id"]: _content(s) for s in v2_sessions}
    json_by_id = {s["id"]: _content(s) for s in json_sessions}
    assert v2_by_id == json_by_id

    # States the exit criteria name are honoured by both.
    assert "tombstoned" not in {s["id"] for s in v2_sessions}
    assert "tombstoned" not in {s["id"] for s in json_sessions}
    assert "pruned" not in {s["id"] for s in json_sessions}

    # The store that was served is fresh and internally consistent (#48 step 3).
    report = store_freshness(tmp_path)
    assert report["ok"] is True
    assert report["hash_matches"] is True


def test_equivalence_holds_again_after_a_reindex(archive, monkeypatch):
    """A rebuild between the two reads must not change either payload's content."""
    tmp_path = archive
    IndexBuilder(tmp_path).build_index(_sessions(), {})

    v2_payload = _load(tmp_path, monkeypatch, use_v2=True)
    json_payload = _load(tmp_path, monkeypatch, use_v2=False)
    ignored = load_ignored_session_ids(tmp_path)
    v2_by_id = {s["id"]: _content(s) for s in v2_payload["sessions"] if s["id"] not in ignored}
    json_by_id = {s["id"]: _content(s) for s in json_payload["sessions"] if s["id"] not in ignored}

    assert v2_by_id == json_by_id
    assert "tombstoned" not in v2_by_id
    assert "pruned" not in v2_by_id
