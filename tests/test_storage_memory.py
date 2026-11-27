"""Tests for the shared agent-memory store (issue #44 PR 4 / #33).

`add_memory` / `recall_memory` / `supersede_memory` are the cross-tool
knowledge API: an agent in one tool records a memory, an agent elsewhere
recalls it. All operations run against the v2 SQLite database.
"""

from __future__ import annotations

import pytest

from annuli.storage import (
    add_memory,
    delete_memory,
    get_memory,
    link_memory_to_session,
    list_memory,
    recall_memory,
    supersede_memory,
)

# ---------------------------------------------------------------------------
# add_memory
# ---------------------------------------------------------------------------


def test_add_memory_returns_id(tmp_path):
    mid = add_memory(tmp_path, "fact", "Title", "Body text")
    assert isinstance(mid, int) and mid > 0


def test_add_memory_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "bogus", "T", "B")


def test_add_memory_rejects_empty_title(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "fact", "   ", "B")


def test_add_memory_rejects_empty_body(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "fact", "T", "")


def test_add_memory_persists_fields(tmp_path):
    mid = add_memory(
        tmp_path,
        "decision",
        "Use Postgres 16",
        "Upgrade the stack.",
        author="cursor",
        scope_project="/home/u/proj",
        tags=["db", "infra"],
    )
    entry = get_memory(tmp_path, mid)
    assert entry is not None
    assert entry.kind == "decision"
    assert entry.author == "cursor"
    assert entry.scope_project == "/home/u/proj"
    assert sorted(entry.tags) == ["db", "infra"]


# ---------------------------------------------------------------------------
# recall_memory — the cross-tool retrieval
# ---------------------------------------------------------------------------


def test_recall_finds_by_title(tmp_path):
    add_memory(tmp_path, "fact", "Kubernetes scaling", "HPA autoscales pods.")
    hits = recall_memory(tmp_path, "kubernetes")
    assert len(hits) == 1
    assert hits[0].title == "Kubernetes scaling"


def test_recall_finds_by_body(tmp_path):
    add_memory(tmp_path, "lesson", "A title", "the secret is in the docker layer cache")
    hits = recall_memory(tmp_path, "layer cache")
    assert [h.title for h in hits] == ["A title"]


def test_recall_cross_tool_scenario(tmp_path):
    """An agent in one tool writes; an agent 'elsewhere' recalls it."""
    add_memory(tmp_path, "decision", "Auth strategy", "Use OAuth2 device flow.", author="cursor")
    # Different 'agent' — just another call — recalls it.
    hits = recall_memory(tmp_path, "oauth")
    assert len(hits) == 1
    assert hits[0].author == "cursor"


def test_recall_empty_query_lists_recent(tmp_path):
    add_memory(tmp_path, "note", "One", "first")
    add_memory(tmp_path, "note", "Two", "second")
    hits = recall_memory(tmp_path, "")
    assert len(hits) == 2


def test_recall_filters_by_kind(tmp_path):
    add_memory(tmp_path, "fact", "shared keyword alpha", "x")
    add_memory(tmp_path, "decision", "shared keyword beta", "y")
    hits = recall_memory(tmp_path, "shared keyword", kind="fact")
    assert all(h.kind == "fact" for h in hits)
    assert len(hits) == 1


def test_recall_filters_by_project(tmp_path):
    add_memory(tmp_path, "fact", "topic one", "a", scope_project="/p1")
    add_memory(tmp_path, "fact", "topic two", "b", scope_project="/p2")
    hits = recall_memory(tmp_path, "topic", scope_project="/p1")
    assert len(hits) == 1
    assert hits[0].scope_project == "/p1"


# ---------------------------------------------------------------------------
# list_memory
# ---------------------------------------------------------------------------


def test_list_memory_newest_first(tmp_path):
    add_memory(tmp_path, "note", "First", "a")
    add_memory(tmp_path, "note", "Second", "b")
    titles = [m.title for m in list_memory(tmp_path)]
    assert titles == ["Second", "First"]


def test_list_memory_hides_superseded_by_default(tmp_path):
    old = add_memory(tmp_path, "fact", "Old fact", "outdated")
    supersede_memory(tmp_path, old, "New fact", "current")
    active = list_memory(tmp_path)
    titles = {m.title for m in active}
    assert "New fact" in titles
    assert "Old fact" not in titles


def test_list_memory_can_include_superseded(tmp_path):
    old = add_memory(tmp_path, "fact", "Old fact", "outdated")
    supersede_memory(tmp_path, old, "New fact", "current")
    titles = {m.title for m in list_memory(tmp_path, include_superseded=True)}
    assert {"Old fact", "New fact"} <= titles


# ---------------------------------------------------------------------------
# supersede_memory — the audit trail
# ---------------------------------------------------------------------------


def test_supersede_links_old_to_new(tmp_path):
    old = add_memory(tmp_path, "decision", "v1", "first decision")
    new = supersede_memory(tmp_path, old, "v2", "revised decision")
    old_entry = get_memory(tmp_path, old)
    assert old_entry is not None
    assert old_entry.superseded_by == new


def test_supersede_inherits_kind_and_scope(tmp_path):
    old = add_memory(tmp_path, "decision", "v1", "x", scope_project="/proj", tags=["t1"])
    new = supersede_memory(tmp_path, old, "v2", "y")
    new_entry = get_memory(tmp_path, new)
    assert new_entry is not None
    assert new_entry.kind == "decision"
    assert new_entry.scope_project == "/proj"
    assert new_entry.tags == ["t1"]


def test_supersede_unknown_id_raises(tmp_path):
    with pytest.raises(ValueError):
        supersede_memory(tmp_path, 9999, "t", "b")


# ---------------------------------------------------------------------------
# linking + deletion
# ---------------------------------------------------------------------------


def test_link_memory_to_session(tmp_path):
    """Linking a memory to a session must not raise (FK is SET NULL-safe)."""
    mid = add_memory(tmp_path, "fact", "T", "B")
    link_memory_to_session(tmp_path, mid, "some-session-id", note="decided here")
    # No exception == pass; the memory itself is unchanged.
    assert get_memory(tmp_path, mid) is not None


def test_delete_memory(tmp_path):
    mid = add_memory(tmp_path, "note", "Trash", "delete me")
    assert delete_memory(tmp_path, mid) is True
    assert get_memory(tmp_path, mid) is None


def test_delete_memory_removes_from_search(tmp_path):
    mid = add_memory(tmp_path, "note", "Findable", "unique-token-xyz")
    assert recall_memory(tmp_path, "unique-token-xyz")
    delete_memory(tmp_path, mid)
    assert recall_memory(tmp_path, "unique-token-xyz") == []


def test_delete_missing_memory_returns_false(tmp_path):
    assert delete_memory(tmp_path, 12345) is False


# ---------------------------------------------------------------------------
# #42 — input size limits
# ---------------------------------------------------------------------------


def test_add_memory_rejects_oversized_title(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "note", "x" * 600, "body")


def test_add_memory_rejects_oversized_body(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "note", "title", "x" * 70_000)


def test_add_memory_rejects_too_many_tags(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "note", "title", "body", tags=[f"t{i}" for i in range(40)])


def test_add_memory_rejects_oversized_tag(tmp_path):
    with pytest.raises(ValueError):
        add_memory(tmp_path, "note", "title", "body", tags=["x" * 100])


def test_add_memory_truncates_long_author(tmp_path):
    mid = add_memory(tmp_path, "note", "realistic title", "body", author="a" * 200)
    entry = get_memory(tmp_path, mid)
    assert entry is not None
    assert len(entry.author or "") <= 64


# ---------------------------------------------------------------------------
# #33 — memory_sources provenance linking
# ---------------------------------------------------------------------------


def test_add_memory_links_source_session(tmp_path):
    from annuli.storage import list_memory_sources

    mid = add_memory(
        tmp_path,
        "decision",
        "Provenance test",
        "body",
        source_session="sess-origin-1",
    )
    sources = list_memory_sources(tmp_path, mid)
    assert len(sources) == 1
    assert sources[0]["session_id"] == "sess-origin-1"


def test_add_memory_without_source_has_no_links(tmp_path):
    from annuli.storage import list_memory_sources

    mid = add_memory(tmp_path, "fact", "No provenance", "body")
    assert list_memory_sources(tmp_path, mid) == []


def test_link_memory_to_session_after_creation(tmp_path):
    from annuli.storage import list_memory_sources

    mid = add_memory(tmp_path, "note", "Late-linked memory", "body")
    link_memory_to_session(tmp_path, mid, "sess-later", note="linked afterwards")
    sources = list_memory_sources(tmp_path, mid)
    assert sources[0]["session_id"] == "sess-later"
    assert sources[0]["note"] == "linked afterwards"


def test_memory_sources_cascade_on_delete(tmp_path):
    """Deleting a memory removes its memory_sources rows (FK cascade)."""
    import sqlite3

    from annuli.storage import v2_db_path

    mid = add_memory(tmp_path, "fact", "Soon deleted", "body", source_session="s1")
    delete_memory(tmp_path, mid)
    conn = sqlite3.connect(v2_db_path(tmp_path))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM memory_sources WHERE memory_id = ?", (mid,)
    ).fetchone()[0]
    assert remaining == 0
