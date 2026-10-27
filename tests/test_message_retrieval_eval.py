"""Message-level retrieval evaluation (#190).

The fixtures are the versioned evaluation set in
``tools/calibration/message-retrieval-eval.json``: German, English,
mixed-language and code-heavy sessions plus topically unrelated decoys, and
hand-labelled queries whose relevant passage sits late in its session. The same
labels calibrate ``LORE_MESSAGE_SEMANTIC_MAX_DISTANCE``
(``tools/calibration/calibrate_message_cutoff.py``), so retrieval and threshold
are measured against one hand-checked set.

Measured on that set and asserted here: recall of the labelled passage (1.0),
cross-session false positives (0), and negative queries returning no hit at all.
Ranking is asserted separately (a concise passage outranks a verbose one that
matches the same terms). No test downloads a model: the FTS path is the default
under test, and the vector-path tests replace the embedder with a deterministic
one (and the session vector pass is stubbed out in every test).
"""

from __future__ import annotations

import json
import zlib
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.storage import v2_db_path, write_sessions
from lore.storage.embeddings import EMBEDDING_DIM, sqlite_vec_available
from lore.storage.message_search import (
    DEFAULT_MESSAGE_SEMANTIC_MAX_DISTANCE,
    EXCERPT_CHARS,
    message_max_distance,
    search_messages,
)
from lore.storage.message_vectors import embed_messages
from lore.storage.schema import initialise

EVAL_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "calibration" / "message-retrieval-eval.json"
)
requires_vec = pytest.mark.skipif(
    not sqlite_vec_available(),
    reason="optional 'semantic' extra (sqlite-vec) not installed",
)


def _eval() -> dict:
    return json.loads(EVAL_PATH.read_text(encoding="utf-8"))


def _sessions() -> list[UnifiedSession]:
    sessions = []
    for raw in _eval()["sessions"]:
        messages = [
            UnifiedMessage(
                role=Role.USER if message["role"] == "user" else Role.ASSISTANT,
                content=message["content"],
                timestamp=datetime(2026, 6, 1, 12, seq % 60),
            )
            for seq, message in enumerate(raw["messages"])
        ]
        sessions.append(
            UnifiedSession(
                tool=Tool.CLAUDE_CODE,
                session_id=raw["id"],
                created_at=datetime(2026, 6, 1),
                last_updated=datetime(2026, 6, 2),
                project_path=raw["project"],
                title=raw["title"],
                messages=messages,
            )
        )
    return sessions


def _session_message(session_id: str, seq: int) -> str:
    for raw in _eval()["sessions"]:
        if raw["id"] == session_id:
            return raw["messages"][seq]["content"]
    raise KeyError(session_id)


def _cases(*, negative: bool) -> list[dict]:
    return [case for case in _eval()["queries"] if bool(case["negative"]) == negative]


def _ids(case: dict) -> str:
    return case["id"]


@pytest.fixture(autouse=True)
def _no_session_vectors(monkeypatch):
    """This module is about message vectors; keep the session pass out of it."""
    monkeypatch.setattr("lore.storage.writer.embed_sessions", lambda conn, items: 0)


@pytest.fixture
def store(tmp_path) -> Path:
    write_sessions(v2_db_path(tmp_path), _sessions())
    return tmp_path


# ---------------------------------------------------------------------------
# FTS path — the labelled late passages, recall/precision and negative queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(negative=False), ids=_ids)
def test_late_session_passages_are_found_and_linked(store, case):
    result = search_messages(store, case["query"], limit=5)

    assert result.path == "fts"  # no message vectors seeded in this test
    assert result.hits, f"no hit for {case['id']}"
    found = {(hit["session_id"], hit["seq"]) for hit in result.hits}
    for label in case["relevant"]:
        expected = (label["session"], label["seq"])
        assert expected in found
        hit = next(h for h in result.hits if (h["session_id"], h["seq"]) == expected)
        # The passage is late in its session, which is what a session-level
        # hit cannot point at.
        message_count = len(
            next(s for s in _eval()["sessions"] if s["id"] == label["session"])["messages"]
        )
        assert hit["seq"] >= message_count // 2
        assert hit["excerpt"].strip()
        assert hit["source"] == "message"
        assert hit["permalink"] == f"/session/{label['session']}/m/{label['seq']}"
        assert hit["evidence"]["message_seq"] == label["seq"]
        assert hit["evidence"]["message_sha256"]


@pytest.mark.parametrize("case", _cases(negative=True), ids=_ids)
def test_negative_queries_return_no_hit(store, case):
    result = search_messages(store, case["query"], limit=5)

    assert result.hits == [], f"{case['id']} fabricated a hit: {result.hits}"


def test_eval_set_recall_and_false_positives(store):
    """Aggregate measurement over the whole hand-labelled set."""
    assert _eval()["version"] == 1
    positive = _cases(negative=False)
    recalled = 0
    false_positives = 0
    for case in positive:
        result = search_messages(store, case["query"], limit=5)
        labelled = {(item["session"], item["seq"]) for item in case["relevant"]}
        labelled_sessions = {item["session"] for item in case["relevant"]}
        retrieved = {(hit["session_id"], hit["seq"]) for hit in result.hits}
        recalled += len(labelled & retrieved)
        false_positives += sum(
            1 for hit in result.hits if hit["session_id"] not in labelled_sessions
        )

    total_labelled = sum(len(case["relevant"]) for case in positive)
    assert recalled / total_labelled == 1.0  # measured recall@5
    assert false_positives == 0


def test_ranking_prefers_the_concise_passage(tmp_path):
    """A verbose message matching the same terms must rank below the answer."""
    concise = "Fehlercode E42 bedeutet: die Kalibrierung ist abgelaufen."
    verbose = (
        "Fehlercode E42 bedeutet: die Kalibrierung ist abgelaufen. "
        + " ".join(f"Randnotiz{i}" for i in range(40))
        + " Ende der Notiz."
    )
    write_sessions(
        v2_db_path(tmp_path),
        [
            _session("verbose", ["Frage zum Fehlercode", verbose]),
            _session("concise", ["Frage zum Fehlercode", concise]),
        ],
    )

    hits = search_messages(tmp_path, "fehlercode kalibrierung abgelaufen", limit=5).hits

    assert [(hit["session_id"], hit["seq"]) for hit in hits] == [("concise", 1), ("verbose", 1)]


def test_evidence_identity_survives_a_reindex(store):
    case = _cases(negative=False)[0]
    label = case["relevant"][0]
    before = next(
        hit
        for hit in search_messages(store, case["query"]).hits
        if (hit["session_id"], hit["seq"]) == (label["session"], label["seq"])
    )

    write_sessions(v2_db_path(store), _sessions())  # delete + re-insert message rows

    after = next(
        hit
        for hit in search_messages(store, case["query"]).hits
        if (hit["session_id"], hit["seq"]) == (label["session"], label["seq"])
    )
    assert after["permalink"] == before["permalink"]
    assert after["evidence"]["message_sha256"] == before["evidence"]["message_sha256"]
    assert after["evidence"]["message_seq"] == before["evidence"]["message_seq"]


def test_excerpt_truncated_reports_partial_coverage(tmp_path):
    long_body = "Kalibrierung " + " ".join(f"wort{i}" for i in range(60))
    write_sessions(
        v2_db_path(tmp_path),
        [
            _session("short", ["Frage", "Die Kalibrierung ist fertig."]),
            _session("long", ["Frage", long_body]),
        ],
    )

    hits = {
        hit["session_id"]: hit for hit in search_messages(tmp_path, "kalibrierung", limit=5).hits
    }

    assert hits["short"]["excerpt_truncated"] is False  # whole message shown
    assert hits["long"]["excerpt_truncated"] is True  # a window, not the message


def test_blank_query_and_missing_store_are_empty(tmp_path):
    assert search_messages(tmp_path, "   ").hits == []
    assert search_messages(tmp_path, "kalibrierung").hits == []


def test_limit_is_bounded(store):
    case = _cases(negative=False)[1]  # the English query hits one passage
    assert len(search_messages(store, case["query"], limit=1).hits) <= 1
    assert len(search_messages(store, case["query"], limit=10_000).hits) <= 100


# ---------------------------------------------------------------------------
# Vector path — deterministic fake embedder, no model download
# ---------------------------------------------------------------------------


def _fake_vector(text: str, overrides: dict) -> list[float]:
    """A unit vector: exact overrides first, else a stable per-text dimension.

    Every non-override text gets its own dimension, so a controlled override can
    be placed at a known L2 distance from it (sqrt(2) for a different axis).
    """
    if text in overrides:
        index = overrides[text]
    else:
        index = 1 + zlib.crc32(text.encode("utf-8")) % (EMBEDDING_DIM - 2)
    vector = [0.0] * EMBEDDING_DIM
    vector[index] = 1.0
    return vector


def _seed_message_vectors(tmp_path, monkeypatch, overrides: dict) -> int:
    from lore.storage import message_vectors

    monkeypatch.setattr(message_vectors, "embeddings_available", lambda: True)
    monkeypatch.setattr(message_vectors, "embed_text", lambda text: _fake_vector(text, overrides))
    conn = initialise(v2_db_path(tmp_path))
    try:
        return embed_messages(conn, budget_seconds=0)
    finally:
        conn.close()


def _patch_query_embedder(monkeypatch, overrides: dict) -> None:
    from lore.storage import message_search

    monkeypatch.setattr(message_search, "embeddings_available", lambda: True)
    monkeypatch.setattr(message_search, "embed_text", lambda text: _fake_vector(text, overrides))


@requires_vec
def test_vector_path_finds_a_paraphrase(store, monkeypatch):
    """A query with no shared term is found through its vector, not by FTS."""
    case = _cases(negative=False)[0]
    label = case["relevant"][0]
    passage = _session_message(label["session"], label["seq"])
    paraphrase = "wie komme ich nach einem abgebrochenen lauf wieder auf den alten stand"
    overrides = {paraphrase: 0, passage: 0}

    assert _seed_message_vectors(store, monkeypatch, overrides) > 0

    # The same query on the FTS fallback finds nothing: the vector path added
    # the hit, it did not just re-rank one.
    from lore.storage import message_search

    monkeypatch.setattr(message_search, "embeddings_available", lambda: False)
    assert search_messages(store, paraphrase, limit=5).hits == []

    _patch_query_embedder(monkeypatch, overrides)
    result = search_messages(store, paraphrase, limit=5)

    assert result.path == "vector"
    assert [(hit["session_id"], hit["seq"]) for hit in result.hits] == [
        (label["session"], label["seq"])
    ]


@requires_vec
def test_vector_path_rejects_a_negative_query(store, monkeypatch):
    """A far neighbour is dropped, not returned as a meaningless nearest hit."""
    case = _cases(negative=True)[0]
    overrides = {case["query"]: 0}  # every seeded message sits on another axis

    _seed_message_vectors(store, monkeypatch, overrides)
    _patch_query_embedder(monkeypatch, overrides)

    result = search_messages(store, case["query"], limit=5)

    assert result.hits == []


@requires_vec
def test_vector_excerpt_is_bounded(store, monkeypatch):
    long_body = "Kalibrierung " + "x" * (EXCERPT_CHARS * 2)
    write_sessions(v2_db_path(store), [_session("long", ["Frage", long_body])])
    overrides = {"kalibrierung abgelaufen": 0, long_body: 0}
    _seed_message_vectors(store, monkeypatch, overrides)
    _patch_query_embedder(monkeypatch, overrides)

    hits = search_messages(store, "kalibrierung abgelaufen", limit=5).hits

    assert hits and hits[0]["excerpt_truncated"] is True
    assert len(hits[0]["excerpt"]) == EXCERPT_CHARS


def test_fts_path_runs_when_the_vector_extra_is_missing(store, monkeypatch):
    """Without sqlite-vec/fastembed the FTS fallback still answers — and says so."""
    from lore.storage import message_search

    monkeypatch.setattr(message_search, "sqlite_vec_available", lambda: False)
    monkeypatch.setattr(message_search, "embeddings_available", lambda: False)
    case = _cases(negative=False)[0]
    label = case["relevant"][0]

    result = search_messages(store, case["query"], limit=5)

    assert result.path == "fts"
    assert (label["session"], label["seq"]) in {
        (hit["session_id"], hit["seq"]) for hit in result.hits
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", DEFAULT_MESSAGE_SEMANTIC_MAX_DISTANCE),
        ("0.5", 0.5),
        ("0", None),
        ("-1", None),
        ("nan", DEFAULT_MESSAGE_SEMANTIC_MAX_DISTANCE),
        ("garbage", DEFAULT_MESSAGE_SEMANTIC_MAX_DISTANCE),
    ],
)
def test_message_max_distance_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("LORE_MESSAGE_SEMANTIC_MAX_DISTANCE", raw)
    assert message_max_distance() == expected


def _session(session_id: str, contents: list[str]) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=session_id,
        created_at=datetime(2026, 6, 1),
        last_updated=datetime(2026, 6, 2),
        project_path="/p",
        title=f"Session {session_id}",
        messages=[
            UnifiedMessage(
                role=Role.USER if seq % 2 == 0 else Role.ASSISTANT,
                content=content,
                timestamp=datetime(2026, 6, 1, 12, seq),
            )
            for seq, content in enumerate(contents)
        ],
    )
