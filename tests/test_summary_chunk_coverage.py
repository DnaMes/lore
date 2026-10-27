"""A chunked summary states exactly what it read (#178).

Sampling used to drop the middle of a long session and report only
``truncated: true``. A reader could not tell which chunks were missing, over what
message range, or whether user turns were skipped with them.
"""

from __future__ import annotations

from lore.services.summarize_chunks import (
    MAX_CHUNKS,
    MAX_SESSION_TOKENS,
    MERGE_PROMPT_BASE_TOKENS,
    MERGE_TOKENS_PER_CHUNK,
    chunk_call_budget,
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
        seqs=frozenset([seq]),
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


def test_the_budget_covers_every_chunk_when_it_can():
    chunks = _chunks(30)
    budget = chunk_call_budget(MAX_SESSION_TOKENS, 3_000)
    assert budget >= len(chunks)
    selected, truncated, indices = sample_chunks(chunks, max_chunks=budget)
    coverage = chunk_coverage(chunks, indices)
    assert truncated is False
    assert coverage["complete"] is True
    assert coverage["chunks_used"] == coverage["chunks_total"] == 30
    assert coverage["seq_range"] == [1, 60]
    assert coverage["messages_total"] == coverage["messages_covered"] == 60
    assert coverage["messages_omitted"] == 0
    assert coverage["omitted_seq_ranges"] == []


def test_the_budget_never_sinks_below_one_chunk():
    # The budget is a hard limit, floored at one chunk while even one fits its
    # budget share — a tiny budget cannot pay the eight-chunk sample and must
    # not silently overrun it.
    assert chunk_call_budget(0, 3_000) == 1
    assert chunk_call_budget(MERGE_PROMPT_BASE_TOKENS, 0) == 1
    paid = MERGE_PROMPT_BASE_TOKENS + 20 * (1_000 + MERGE_TOKENS_PER_CHUNK)
    assert chunk_call_budget(paid, 1_000) == 20


def test_the_eight_chunk_minimum_applies_only_when_the_budget_pays():
    chunks = _chunks(30)
    # ~3000-token chunk prompts: the default budget pays for more than eight.
    budget = chunk_call_budget(MAX_SESSION_TOKENS, 3_000)
    assert budget >= MAX_CHUNKS
    _, truncated, _indices = sample_chunks(chunks, max_chunks=budget)
    assert truncated is False  # every chunk fits → none is sampled away


def test_a_budget_too_small_omits_exact_seq_ranges():
    chunks = _chunks(30)
    # 30000-token chunks: the budget pays for four chunk calls; the eight-chunk
    # sample would select eight and overrun the budget, so the four that fit is
    # the sample: head 1, two densest middle, tail 1.
    budget = chunk_call_budget(MAX_SESSION_TOKENS, 30_000)  # 147000 // 30_400 == 4
    assert budget == 4
    _selected, truncated, indices = sample_chunks(chunks, max_chunks=budget)
    assert indices == [0, 1, 2, 29]
    coverage = chunk_coverage(chunks, indices)
    assert truncated is True
    assert coverage["complete"] is False
    assert coverage["chunks_used"] == 4
    assert coverage["messages_total"] == 60
    assert coverage["messages_covered"] == 8
    assert coverage["messages_omitted"] == 52
    assert coverage["omitted_seq_ranges"] == [{"from_seq": 7, "to_seq": 58}]


def test_a_decisive_late_message_is_inside_a_covered_range():
    chunks = _chunks(30)
    _selected, _truncated, indices = sample_chunks(chunks, max_chunks=MAX_CHUNKS)
    coverage = chunk_coverage(chunks, indices)
    assert any(block.last_seq == 60 for index in indices for block in chunks[index])
    assert all(
        not (gap["from_seq"] <= 60 <= gap["to_seq"]) for gap in coverage["omitted_seq_ranges"]
    )


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


def test_the_message_counters_ignore_quoted_seq_markers():
    # The counters must come from the block anchors, not from the text: a quoted
    # marker would invent a message and an omission for it.
    messages = [
        SummaryMessage(seq=1, role="user", content="the log said: [seq=99999] retrying"),
        SummaryMessage(seq=2, role="user", content="ack, retry scheduled"),
    ]
    transcript = compress(messages)
    chunks = split_chunks(transcript, 1)
    assert len(chunks) == 2, "the probe needs one chunk per message"
    coverage = chunk_coverage(chunks, [1])  # only the second message is summarised
    assert coverage["seq_range"] == [1, 2]
    assert coverage["messages_total"] == 2
    assert coverage["messages_covered"] == 1
    assert coverage["messages_omitted"] == 1
    assert coverage["omitted_seq_ranges"] == [{"from_seq": 1, "to_seq": 1}]
    omitted = coverage["omitted_seq_ranges"]
    assert all(99999 not in (gap["from_seq"], gap["to_seq"]) for gap in omitted)


def test_coverage_counts_only_existing_sequences_after_injected_context_is_removed():
    # A dropped injected_context message must not be counted as a present
    # message, and it must not appear in any reported omitted range.
    messages = [
        SummaryMessage(seq=1, role="user", content="first user prompt"),
        SummaryMessage(seq=2, role="user", content="injected context", kind="injected_context"),
        SummaryMessage(seq=3, role="assistant", content="first assistant reply"),
        SummaryMessage(seq=4, role="assistant", content="second assistant reply"),
    ]
    transcript = compress(messages)
    chunks = split_chunks(transcript, 1_000_000)
    coverage = chunk_coverage(chunks, list(range(len(chunks))))
    assert coverage["messages_total"] == 3
    assert coverage["messages_covered"] == 3
    assert coverage["messages_omitted"] == 0
    assert coverage["omitted_seq_ranges"] == []
    for gap in coverage["omitted_seq_ranges"]:
        assert not (gap["from_seq"] <= 2 <= gap["to_seq"])
