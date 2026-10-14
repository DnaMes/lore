"""Tests for deterministic summary input (B4.1, issue #197)."""

from __future__ import annotations

import time
from datetime import datetime

from lore.core.models import Role, UnifiedMessage
from lore.services.summary_input import (
    COMPRESSOR_VERSION,
    CompressedTranscript,
    SummaryMessage,
    compress,
    estimate_tokens,
    extract_artifacts,
    from_unified,
    split_chunks,
)

T0 = datetime(2026, 6, 1, 12, 0, 0)


def _sm(
    seq: int,
    role: str,
    content: str,
    kind: str = "text",
    tool_events: tuple[dict, ...] = (),
) -> SummaryMessage:
    return SummaryMessage(seq=seq, role=role, content=content, kind=kind, tool_events=tool_events)


def _um(role: Role, content: str, kind: str = "text", **kwargs) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=T0, kind=kind, **kwargs)


# --- from_unified ---


def test_from_unified_seq_and_fields():
    msgs = [
        _um(Role.USER, "hi", kind="text"),
        _um(Role.ASSISTANT, "hello", tool_events=[{"name": "Read", "is_error": False}]),
    ]
    out = from_unified(msgs, first_seq=10)
    assert len(out) == 2
    assert out[0].seq == 10 and out[0].role == "user" and out[0].kind == "text"
    assert out[1].seq == 11 and out[1].role == "assistant"
    assert out[1].tool_events == ({"name": "Read", "is_error": False},)


def test_from_unified_skips_bad_tool_events():
    msgs = [_um(Role.USER, "x", tool_events=["not-a-dict", {"a": 1}])]
    assert from_unified(msgs)[0].tool_events == ({"a": 1},)


# --- extract_artifacts: files ---


def test_artifacts_files_read_dedup_order():
    content = "[Tool: Read] /a/foo.py\n[Tool: Read] /b/bar.py\n[Tool: Read] /a/foo.py\n"
    art = extract_artifacts([_sm(1, "assistant", content)])
    assert art.files_read == ("/a/foo.py", "/b/bar.py")
    assert art.files_modified == ()


def test_artifacts_files_modified_tools():
    content = (
        "[Tool: Write] /w.py\n"
        "[Tool: Edit] /e.py\n"
        "[Tool: MultiEdit] /m.py\n"
        "[Tool: NotebookEdit] /n.ipynb\n"
        "[Tool: apply_patch] /p.py\n"
    )
    art = extract_artifacts([_sm(1, "assistant", content)])
    assert art.files_modified == ("/w.py", "/e.py", "/m.py", "/n.ipynb", "/p.py")


def test_artifacts_rejects_control_chars_in_path():
    art = extract_artifacts([_sm(1, "assistant", "[Tool: Read] /bad\x00path")])
    assert art.files_read == ()


def test_artifacts_prose_untouched():
    art = extract_artifacts([_sm(1, "user", "I like [Tool: Read] fiction books")])
    assert art.files_read == ()


# --- extract_artifacts: commands ---


def test_artifacts_commands_dedup_cap_200():
    long_cmd = "x" * 300
    msgs = [
        _sm(1, "assistant", f"[Tool: Bash]\n{long_cmd}"),
        _sm(2, "assistant", f"[Tool: Bash]\n{long_cmd}"),
    ]
    art = extract_artifacts(msgs)
    assert len(art.commands) == 1
    assert len(art.commands[0]) == 200


def test_artifacts_commands_max_30():
    msgs = [_sm(i, "assistant", f"[Tool: Shell]\ncmd{i}") for i in range(1, 40)]
    art = extract_artifacts(msgs)
    assert len(art.commands) == 30


def test_artifacts_commands_fenced_after_bash():
    art = extract_artifacts([_sm(1, "assistant", "[Tool: Bash]\n```\npytest -q\n```")])
    assert "pytest" in art.commands[0]


# --- extract_artifacts: errors ---


def test_artifacts_errors_from_tool_result():
    art = extract_artifacts([_sm(5, "tool", "Error: something failed badly\nmore lines")])
    assert art.errors == ((5, "Error: something failed badly"),)


def test_artifacts_errors_tool_result_prefix():
    art = extract_artifacts(
        [_sm(3, "assistant", "[Tool Result]\nTraceback (most recent call last):")]
    )
    assert len(art.errors) == 1
    assert art.errors[0][0] == 3


def test_artifacts_errors_from_tool_events():
    art = extract_artifacts(
        [
            _sm(
                7,
                "assistant",
                "ok",
                tool_events=({"name": "Bash", "is_error": True},),
            )
        ]
    )
    assert (7, "Bash failed") in art.errors


def test_artifacts_errors_max_20():
    msgs = [_sm(i, "tool", f"error {i}: failed") for i in range(1, 30)]
    assert len(extract_artifacts(msgs).errors) == 20


# --- compress: caps ---


def test_compress_user_head_tail_elision():
    body = "U" * 5000
    t = compress([_sm(1, "user", body)])
    assert "[+2000 chars elided]" in t.text
    assert t.text.count("U") == 2000 + 1000


def test_compress_assistant_head_tail():
    body = "A" * 5000
    t = compress([_sm(1, "assistant", body)])
    assert "[+3000 chars elided]" in t.text
    assert len(t.text) < 2500


def test_compress_tool_result_head_tail_errors():
    noise = "n" * 2000
    body = f"{noise}\nERROR: disk full\n{noise}"
    t = compress([_sm(1, "tool", body)])
    assert "[+N chars elided]" in t.text or "ERROR" in t.text
    assert len(t.text) < len(body)


def test_compress_short_tool_result_whole():
    short = "ok output"
    t = compress([_sm(1, "tool", short)])
    assert short in t.text
    assert 1 not in t.elided_seqs


# --- compress: tool run collapse ---


def test_compress_collapses_12_tool_only_messages():
    msgs: list[SummaryMessage] = []
    for i in range(1, 13):
        if i % 2 == 1:
            msgs.append(_sm(i, "assistant", "[Tool: Bash]\nls"))
        else:
            msgs.append(_sm(i, "tool", "listing..."))
    t = compress(msgs)
    assert "tool calls:" in t.text
    assert len(t.elided_seqs) >= 6
    assert 1 in t.seqs


def test_compress_native_compaction_summary():
    t = compress(
        [
            _sm(1, "user", "go"),
            _sm(2, "assistant", "prior work", kind="compaction_summary"),
        ]
    )
    assert "[NATIVE-SUMMARY seq=2]" in t.text
    assert t.native_summary_seqs == (2,)


def test_compress_drops_injected_context():
    t = compress(
        [
            _sm(1, "user", "hi"),
            _sm(2, "assistant", "SECRET AGENTS", kind="injected_context"),
            _sm(3, "assistant", "hello"),
        ]
    )
    assert "SECRET AGENTS" not in t.text
    assert t.user_prompt_count == 1


# --- compress: blocks ---


def test_compress_blocks_at_user_turn():
    t = compress(
        [
            _sm(1, "assistant", "warmup"),
            _sm(2, "user", "q1"),
            _sm(3, "assistant", "[Tool: Read] /x\n[Tool Result]\nfile"),
            _sm(4, "user", "q2"),
        ]
    )
    assert len(t.blocks) >= 2
    user_blocks = [b for b in t.blocks if b.is_user_turn]
    assert len(user_blocks) == 2
    # call/result in same block as first user q
    block2 = next(b for b in t.blocks if b.first_seq == 2)
    assert "[Tool: Read]" in block2.text or "Read" in block2.text


def test_compress_seqs_citable():
    t = compress([_sm(1, "user", "x"), _sm(2, "assistant", "y")])
    assert t.seqs == frozenset({1, 2})


def test_compress_determinism_tool_events_key_order():
    ev1 = ({"z": 1, "name": "a"},)
    ev2 = ({"name": "a", "z": 1},)
    m1 = [_sm(1, "assistant", "x", tool_events=ev1)]
    m2 = [_sm(1, "assistant", "x", tool_events=ev2)]
    assert compress(m1).text == compress(m2).text


def test_compress_wrong_typed_does_not_raise():
    bad = SummaryMessage(seq="x", role="user", content=None)  # type: ignore[arg-type]
    t = compress([bad])
    assert t.blocks == ()
    assert t.text == ""
    assert t.seqs == frozenset()


def test_compressor_version_constant():
    assert COMPRESSOR_VERSION == 1


# --- estimate_tokens ---


def test_estimate_tokens():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(1) == 1
    assert estimate_tokens(8) == 2
    assert estimate_tokens(100) == 25


# --- split_chunks ---


def _transcript_with_blocks(*texts: str) -> CompressedTranscript:
    from lore.services.summary_input import CompressedBlock

    blks = tuple(
        CompressedBlock(
            first_seq=i + 1,
            last_seq=i + 1,
            text=t,
            is_user_turn=(i % 2 == 0),
        )
        for i, t in enumerate(texts)
    )
    full = "\n".join(texts)
    return CompressedTranscript(
        version=1,
        blocks=blks,
        seqs=frozenset(range(1, len(texts) + 1)),
        elided_seqs=frozenset(),
        native_summary_seqs=(),
        user_prompt_count=len(texts),
        char_count=len(full),
    )


def test_split_chunks_respects_max_chars():
    tr = _transcript_with_blocks("a" * 50, "b" * 50, "c" * 50)
    chunks = split_chunks(tr, max_chars=80)
    assert all(sum(len(b.text) for b in ch) <= 80 or len(ch) == 1 for ch in chunks)


def test_split_chunks_never_splits_block():
    tr = _transcript_with_blocks("x" * 500)
    chunks = split_chunks(tr, max_chars=100)
    assert len(chunks) == 1
    assert len(chunks[0]) == 1


# --- performance and ratio ---


def test_compress_50k_messages_under_3_seconds():
    msgs = [
        _sm(i, "user" if i % 3 == 0 else "assistant", f"line {i}" * 5) for i in range(1, 50_001)
    ]
    start = time.perf_counter()
    compress(msgs)
    assert time.perf_counter() - start < 3.0


def test_compress_noisy_1_5mb_under_3_percent():
    line = "OUT" * 40 + "\n"
    blob = line * 40_000 + "ERROR: fail\n" + line * 40_000
    assert len(blob) > 1_400_000
    t = compress([_sm(1, "tool", blob)])
    ratio = t.char_count / len(blob)
    assert ratio < 0.03


def test_from_unified_wrong_content_skipped():
    class BadMsg:
        role = Role.USER
        content = None
        kind = "text"
        tool_events = []

    assert from_unified([BadMsg()]) == []  # type: ignore[list-item]


def test_many_bash_headers_in_one_message_are_linear_time():
    import time

    content = "".join(f"[Tool: Bash]\n```\necho {i}\n```\n" for i in range(20_000))
    start = time.perf_counter()
    artifacts = extract_artifacts([SummaryMessage(seq=1, role="assistant", content=content)])
    assert time.perf_counter() - start < 3.0
    assert len(artifacts.commands) <= 30


def test_truncated_tool_only_assistant_lines_mark_the_seq_elided():
    long_line = "[Tool: Bash] " + "x" * 600
    msgs = [
        SummaryMessage(seq=1, role="user", content="go"),
        SummaryMessage(seq=2, role="assistant", content=long_line),
    ]
    t = compress(msgs)
    assert 2 in t.elided_seqs


def test_error_lines_kept_from_long_tool_results_are_bounded():
    huge = "error: " + "e" * 500_000
    msgs = [SummaryMessage(seq=1, role="tool", content="start\n" + huge + "\n" + "ok\n" * 400)]
    t = compress(msgs)
    assert t.char_count < 2_000


def test_duplicate_and_boolean_seqs_are_dropped_first_wins():
    msgs = [
        SummaryMessage(seq=1, role="user", content="first"),
        SummaryMessage(seq=1, role="assistant", content="second with same seq"),
        SummaryMessage(seq=True, role="user", content="bool seq"),  # type: ignore[arg-type]
    ]
    t = compress(msgs)
    assert t.seqs == frozenset({1})
    assert "second with same seq" not in t.text
    assert "bool seq" not in t.text
