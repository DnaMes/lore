"""A chunked summary states exactly what it read (#178).

Sampling used to drop the middle of a long session and report only
``truncated: true``. A reader could not tell which chunks were missing, over what
message range, or whether user turns were skipped with them.
"""

from __future__ import annotations

from lore.services.summarize_chunks import (
    MAX_CHUNKS,
    chunk_coverage,
    sample_chunks,
)
from lore.services.summary_input import CompressedBlock, SummaryMessage, compress, split_chunks


def _block(seq: int, *, user: bool = False) -> CompressedBlock:
    return CompressedBlock(
        first_seq=seq,
        last_seq=seq,
        text=f"[seq={seq}] message body {seq}",
        is_user_turn=user,
    )


def _chunks(count: int, *, per_chunk: int = 2, user_every: int = 1):
    chunks = []
    seq = 1
    for index in range(count):
        blocks = tuple(
            _block(seq + offset, user=bool(user_every) and offset % user_every == 0)
            for offset in range(per_chunk)
        )
        chunks.append(blocks)
        seq += per_chunk
    return chunks


def test_a_complete_run_says_so():
    chunks = _chunks(4)
    selected, truncated, indices = sample_chunks(chunks)
    coverage = chunk_coverage(chunks, indices)
    assert truncated is False
    assert coverage["complete"] is True
    assert coverage["chunks_total"] == coverage["chunks_used"] == 4
    assert coverage["omitted_chunks"] == []
    assert coverage["seq_range"] == [1, 8]


def test_a_sampled_run_reports_the_gaps():
    chunks = _chunks(20)
    selected, truncated, indices = sample_chunks(chunks)
    coverage = chunk_coverage(chunks, indices)
    assert truncated is True
    assert coverage["complete"] is False
    assert coverage["chunks_total"] == 20
    assert coverage["chunks_used"] == MAX_CHUNKS
    assert coverage["omitted_chunks"], "no gap reported"
    # Every omitted index is covered by exactly one reported range.
    omitted_indices = {
        index
        for gap in coverage["omitted_chunks"]
        for index in range(gap["from_chunk"], gap["to_chunk"] + 1)
    }
    assert omitted_indices == set(range(20)) - set(indices)


def test_a_sampled_run_reports_the_skipped_user_turns():
    chunks = _chunks(20)
    _selected, _truncated, indices = sample_chunks(chunks)
    coverage = chunk_coverage(chunks, indices)
    expected = sum(
        1
        for index, blocks in enumerate(chunks)
        if index not in set(indices)
        for block in blocks
        if block.is_user_turn
    )
    assert coverage["user_turns_omitted"] == expected
    assert coverage["user_turns_omitted"] > 0


def test_the_seq_range_covers_the_whole_session_not_the_selection():
    chunks = _chunks(20, per_chunk=5)
    _selected, _truncated, indices = sample_chunks(chunks)
    coverage = chunk_coverage(chunks, indices)
    assert coverage["seq_range"] == [1, 100]


def test_head_and_tail_are_always_included():
    chunks = _chunks(20)
    _selected, _truncated, indices = sample_chunks(chunks)
    assert 0 in indices and 1 in indices
    assert 19 in indices and 18 in indices and 17 in indices


def test_coverage_is_stable_for_the_same_input():
    chunks = _chunks(20)
    first = chunk_coverage(chunks, sample_chunks(chunks)[2])
    second = chunk_coverage(chunks, sample_chunks(chunks)[2])
    assert first == second


def test_chunk_coverage_ignores_quoted_seq_markers():
    # A user message quoting a marker from a log must not widen the reported span.
    messages = [
        SummaryMessage(seq=1, role="user", content="the log said: [seq=99999] retrying"),
        SummaryMessage(seq=2, role="assistant", content="ack, retry scheduled"),
    ]
    transcript = compress(messages)
    chunks = split_chunks(transcript, 1_000_000)
    assert chunks
    coverage = chunk_coverage(chunks, list(range(len(chunks))))
    assert coverage["seq_range"] == [1, 2]
