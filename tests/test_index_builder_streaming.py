"""Streaming-contract tests for IndexBuilder (issue #96).

These lock in the memory fix: build_index must consume its ``sessions`` input
as a single-pass stream, never holding more than a small constant number of
``UnifiedSession`` objects alive at once, and a mid-stream v2 failure must leave
the legacy index.json complete (v2 best-effort, #44).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder


def _session(session_id: str, *, n_messages: int = 3) -> UnifiedSession:
    now = datetime(2025, 6, 15, 10, 0, 0)
    msgs = [
        UnifiedMessage(role=Role.USER, content=f"message {i} for {session_id}", timestamp=now)
        for i in range(n_messages)
    ]
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=msgs,
        project_path="/proj",
        title=f"Session {session_id}",
    )


def test_build_index_streams_without_materialising_all(tmp_path):
    """build_index must not hold every session alive at once (#96).

    A tracking generator increments a live counter on yield and decrements it
    once the consumer has released the reference (garbage collected). The old
    list-materialising build_index kept all N alive (peak == N); the streamed
    build keeps only a small constant.
    """
    import gc
    import weakref

    live = 0
    peak = 0
    refs = []  # keep the weakrefs alive so their callbacks actually fire

    def tracked(session):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)

        def _released(_ref):
            nonlocal live
            live -= 1

        refs.append(weakref.ref(session, _released))
        return session

    total = 50

    def gen():
        for i in range(total):
            # Build each session inside the generator so nothing outside holds
            # a strong reference once build_index drops it.
            yield tracked(_session(f"s{i:03d}"))
            gc.collect()  # force the weakref callback to run deterministically

    IndexBuilder(tmp_path).build_index(gen(), {})
    gc.collect()

    data = json.loads((tmp_path / "index.json").read_text())
    assert len(data["sessions"]) == total
    # The streamed build should keep only a handful of sessions alive, never the
    # whole set. Allow generous slack (batch buffering) but far below `total`.
    assert peak < total // 2, f"peak live sessions {peak} suggests full materialisation"


def test_build_index_ordering_reused_then_streamed(tmp_path):
    """JSON session order stays reused-entries-first, then extraction order."""
    # Seed an index so the reused entry has a prior dict shape.
    reused = {
        "id": "reused-1",
        "tool": "claude-code",
        "project": "/proj",
        "thread_id": None,
        "title": "Reused One",
        "created": "2025-06-15T10:00:00",
        "updated": "2025-06-15T10:00:00",
        "messages": 1,
        "prompts": 1,
        "keywords": [],
        "search_text": "reused body",
    }

    def gen():
        yield _session("fresh-a")
        yield _session("fresh-b")

    IndexBuilder(tmp_path).build_index(gen(), {}, reused_entries=[reused])

    ids = [s["id"] for s in json.loads((tmp_path / "index.json").read_text())["sessions"]]
    assert ids == ["reused-1", "fresh-a", "fresh-b"]


def test_v2_failure_midstream_leaves_legacy_index_complete(tmp_path, monkeypatch):
    """A v2 write error must not break the legacy index (#44, streamed path).

    Inject a failure into the v2 streaming writer's add_full; the legacy
    index.json must still contain every session.
    """
    from lore.storage import writer as writer_mod

    calls = {"n": 0}
    original = writer_mod.StreamingV2Writer.add_full

    def flaky_add_full(self, session):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("injected v2 failure")
        return original(self, session)

    monkeypatch.setattr(writer_mod.StreamingV2Writer, "add_full", flaky_add_full)

    def gen():
        for i in range(4):
            yield _session(f"s{i}")

    # Must not raise — v2 is best-effort.
    IndexBuilder(tmp_path).build_index(gen(), {})

    data = json.loads((tmp_path / "index.json").read_text())
    assert [s["id"] for s in data["sessions"]] == ["s0", "s1", "s2", "s3"]
    # Legacy sqlite is complete too.
    conn = sqlite3.connect(tmp_path / "index.sqlite")
    try:
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()
    assert count == 4


def test_streamed_and_list_builds_are_equivalent(tmp_path):
    """A generator input yields the same index.json as a list input."""
    sessions = [_session(f"s{i}") for i in range(5)]

    out_list = tmp_path / "list"
    out_stream = tmp_path / "stream"
    IndexBuilder(out_list).build_index(list(sessions), {})
    IndexBuilder(out_stream).build_index(iter(sessions), {})

    a = json.loads((out_list / "index.json").read_text())
    b = json.loads((out_stream / "index.json").read_text())
    # generated_at differs by construction; everything else must match.
    a.pop("generated_at")
    b.pop("generated_at")
    assert a == b


@pytest.mark.parametrize("n", [0, 1, 250])
def test_build_index_batch_boundaries(tmp_path, n):
    """Batch flushing (N=100) writes exactly the right rows at any size."""

    def gen():
        for i in range(n):
            yield _session(f"s{i:04d}")

    IndexBuilder(tmp_path).build_index(gen(), {})
    data = json.loads((tmp_path / "index.json").read_text())
    assert len(data["sessions"]) == n
    conn = sqlite3.connect(tmp_path / "index.sqlite")
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == n
    finally:
        conn.close()


def test_reused_ids_route_to_v2_only_and_preserve_json(tmp_path):
    """A session whose id is in reused_ids is written v2-only (#103).

    Its JSON/legacy entry comes from reused_entries (not from the streamed
    session), while its full message rows still reach v2 — so the merged
    single-stream caller never needs a separate reused_sessions list.
    """
    reused = {
        "id": "r1",
        "tool": "claude-code",
        "project": "/proj",
        "thread_id": None,
        "title": "Reused One",
        "created": "2025-06-15T10:00:00",
        "updated": "2025-06-15T10:00:00",
        "messages": 3,
        "prompts": 0,
        "keywords": [],
        "search_text": "reused body",
    }

    # r1 arrives INSIDE the sessions stream and is tagged reused via reused_ids.
    def stream():
        yield _session("r1", n_messages=3)  # reused -> v2-only
        yield _session("fresh", n_messages=2)  # fresh -> full fan-out

    IndexBuilder(tmp_path).build_index(stream(), {}, reused_entries=[reused], reused_ids={"r1"})

    data = json.loads((tmp_path / "index.json").read_text())
    # JSON: reused entry (verbatim, reused-first) then the fresh session — r1
    # appears exactly once (from reused_entries, not duplicated by the stream).
    assert [s["id"] for s in data["sessions"]] == ["r1", "fresh"]
    assert data["sessions"][0]["title"] == "Reused One"  # verbatim prior dict

    # v2 got full message rows for BOTH r1 (reused) and fresh (#35 intact).
    from lore.storage.writer import v2_db_path

    conn = sqlite3.connect(v2_db_path(tmp_path))
    try:
        r1_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='r1'").fetchone()[0]
        r1_synced = conn.execute("SELECT messages_synced FROM sessions WHERE id='r1'").fetchone()[0]
    finally:
        conn.close()
    assert r1_msgs == 3, "reused session must still get its v2 message rows"
    assert r1_synced == 1


def test_reused_stream_preserves_existing_v2_messages(tmp_path):
    """A complete reused row keeps its stored messages during a later sync."""
    from lore.storage import v2_db_path

    IndexBuilder(tmp_path).build_index([_session("reused", n_messages=2)], {})
    prior = {
        "id": "reused",
        "tool": "claude-code",
        "project": "/proj",
        "thread_id": None,
        "title": "Reused One",
        "created": "2025-06-15T10:00:00",
        "updated": "2025-06-15T10:00:00",
        "messages": 2,
        "prompts": 0,
        "keywords": [],
        "search_text": "message body",
    }
    changed = _session("reused", n_messages=2)
    changed.messages[0].content = "replacement content"
    IndexBuilder(tmp_path).build_index([changed], {}, reused_entries=[prior], reused_ids={"reused"})

    conn = sqlite3.connect(v2_db_path(tmp_path))
    assert conn.execute(
        "SELECT content FROM messages WHERE session_id='reused' ORDER BY seq"
    ).fetchone() == ("message 0 for reused",)
    assert conn.execute("SELECT messages_synced FROM sessions WHERE id='reused'").fetchone() == (1,)


def test_reused_ids_stream_does_not_materialise(tmp_path):
    """Reused sessions in the merged stream are dropped one at a time (#103)."""
    import gc
    import weakref

    live = 0
    peak = 0
    refs = []

    def tracked(s):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        refs.append(weakref.ref(s, lambda _r: _dec()))
        return s

    def _dec():
        nonlocal live
        live -= 1

    total = 40
    reused_ids = {f"s{i:03d}" for i in range(0, total, 2)}  # half reused
    entries = [
        {
            "id": sid,
            "tool": "claude-code",
            "project": "/p",
            "thread_id": None,
            "title": sid,
            "created": "2025-06-15T10:00:00",
            "updated": "2025-06-15T10:00:00",
            "messages": 3,
            "prompts": 0,
            "keywords": [],
            "search_text": "x",
        }
        for sid in sorted(reused_ids)
    ]

    def stream():
        for i in range(total):
            yield tracked(_session(f"s{i:03d}"))
            gc.collect()

    IndexBuilder(tmp_path).build_index(stream(), {}, reused_entries=entries, reused_ids=reused_ids)
    gc.collect()
    assert peak < total // 2, f"peak {peak} suggests reused sessions were retained"
