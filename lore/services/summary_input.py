"""Deterministic transcript compression and artifact extraction for summaries (#197)."""

from __future__ import annotations

import bisect
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from ..core.models import Role, UnifiedMessage

COMPRESSOR_VERSION = 1

_MAX_FILES = 200
_MAX_PATH_LEN = 300
_MAX_COMMANDS = 30
_MAX_CMD_LEN = 200
_MAX_ERRORS = 20
_MAX_ERR_LINE = 200

_USER_CAP = 3000
_USER_HEAD = 2000
_USER_TAIL = 1000
_ASST_CAP = 2000
_ASST_HEAD = 1400
_ASST_TAIL = 600
_NATIVE_CAP = 6000
_TOOL_RESULT_WHOLE = 600
_TOOL_HEAD = 300
_TOOL_TAIL = 200
_TOOL_LINE_CAP = 200
_TOOL_RUN_COLLAPSE = 5

_READ_RE = re.compile(r"\[Tool:\s*Read\]\s+(\S+)")
_MODIFIED_RES = [
    re.compile(rf"\[Tool:\s*{name}\]\s+(\S+)", re.IGNORECASE)
    for name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch")
]
_CMD_HEADER_RE = re.compile(
    r"\[Tool:\s*(?:Bash|Shell|exec_command)\]",
    re.IGNORECASE,
)
_ERROR_SCAN_RE = re.compile(r"(?i)\b(error|failed|failure|traceback|exception|denied|fatal)\b")
_ERROR_LINE_RE = re.compile(r"(?i).*\b(error|fail|traceback|exception|denied)\b.*")
_TOOL_LINE_RE = re.compile(r"^\[Tool:\s*.+\]", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class SummaryMessage:
    seq: int
    role: str
    content: str
    kind: str = "text"
    tool_events: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Artifacts:
    files_read: tuple[str, ...]
    files_modified: tuple[str, ...]
    commands: tuple[str, ...]
    errors: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class CompressedBlock:
    first_seq: int
    last_seq: int
    text: str
    is_user_turn: bool
    seqs: frozenset[int]


@dataclass(frozen=True)
class CompressedTranscript:
    version: int
    blocks: tuple[CompressedBlock, ...]
    seqs: frozenset[int]
    elided_seqs: frozenset[int]
    native_summary_seqs: tuple[int, ...]
    user_prompt_count: int
    char_count: int

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


def estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return max(1, chars // 4)


def from_unified(messages: Sequence[UnifiedMessage], *, first_seq: int = 1) -> list[SummaryMessage]:
    out: list[SummaryMessage] = []
    for idx, msg in enumerate(messages):
        content = getattr(msg, "content", None)
        if not isinstance(content, str):
            continue
        role_obj = getattr(msg, "role", None)
        if isinstance(role_obj, Role):
            role = role_obj.value
        elif isinstance(role_obj, str):
            role = role_obj
        else:
            continue
        kind = getattr(msg, "kind", "text")
        if not isinstance(kind, str):
            kind = "text"
        raw_events = getattr(msg, "tool_events", None) or []
        events: tuple[dict, ...] = tuple(e for e in raw_events if isinstance(e, dict))
        out.append(
            SummaryMessage(
                seq=first_seq + idx,
                role=role,
                content=content,
                kind=kind,
                tool_events=events,
            )
        )
    return out


def _valid_path(path: str) -> str | None:
    if not path or len(path) > _MAX_PATH_LEN:
        return None
    if _CTRL_RE.search(path):
        return None
    if not (path.startswith(("/", "~", ".")) or "/" in path):
        return None
    return path


def _add_path(paths: list[str], seen: set[str], raw: str) -> None:
    if len(paths) >= _MAX_FILES:
        return
    p = _valid_path(raw)
    if p is None or p in seen:
        return
    seen.add(p)
    paths.append(p)


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _extract_command_from_block(block: str) -> str | None:
    block = block.strip()
    if not block:
        return None
    fence = re.search(r"```[^\n]*\n(.*?)```", block, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    else:
        lines = block.splitlines()
        body = "\n".join(lines[1:]) if len(lines) > 1 else lines[0]
        body = body.strip()
    if not body:
        return None
    cmd = _collapse_ws(body)[:_MAX_CMD_LEN]
    return cmd or None


_TOOL_START_RE = re.compile(r"\n\[Tool:")
_MAX_HEADERS_PER_MESSAGE = 200
_MAX_ERROR_LINE_KEPT = 240


def extract_artifacts(messages: Sequence[SummaryMessage]) -> Artifacts:
    files_read: list[str] = []
    files_mod: list[str] = []
    commands: list[str] = []
    errors: list[tuple[int, str]] = []
    seen_read: set[str] = set()
    seen_mod: set[str] = set()
    seen_cmd: set[str] = set()

    for msg in messages:
        if not isinstance(msg.seq, int):
            continue
        if not isinstance(msg.content, str):
            continue
        content = msg.content
        for m in _READ_RE.finditer(content):
            _add_path(files_read, seen_read, m.group(1))
        for pat in _MODIFIED_RES:
            for m in pat.finditer(content):
                _add_path(files_mod, seen_mod, m.group(1))

        tool_starts = [t.start() for t in _TOOL_START_RE.finditer(content)]
        for m in itertools.islice(_CMD_HEADER_RE.finditer(content), _MAX_HEADERS_PER_MESSAGE):
            start = m.end()
            ix = bisect.bisect_left(tool_starts, start)  # next "\n[Tool:" without slicing
            end = tool_starts[ix] if ix < len(tool_starts) else len(content)
            chunk = content[start:end]
            cmd = _extract_command_from_block(chunk)
            if cmd and cmd not in seen_cmd and len(commands) < _MAX_COMMANDS:
                seen_cmd.add(cmd)
                commands.append(cmd)

        is_tool_result = msg.role == "tool" or content.startswith("[Tool Result]")
        if is_tool_result and len(errors) < _MAX_ERRORS:
            head = content[:400]
            if _ERROR_SCAN_RE.search(head):
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        errors.append((msg.seq, line[:_MAX_ERR_LINE]))
                        break

        for ev in msg.tool_events:
            if not isinstance(ev, dict):
                continue
            if ev.get("is_error") is True and len(errors) < _MAX_ERRORS:
                name = ev.get("name", "tool")
                if not isinstance(name, str):
                    name = "tool"
                errors.append((msg.seq, f"{name} failed"))

    return Artifacts(
        files_read=tuple(files_read),
        files_modified=tuple(files_mod),
        commands=tuple(commands),
        errors=tuple(errors),
    )


def _head_tail_elide(text: str, cap: int, head: int, tail: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    elided = len(text) - head - tail
    return f"{text[:head]}\n[+{elided} chars elided]\n{text[-tail:]}", True


def _is_tool_only_assistant(content: str) -> bool:
    if "[Tool:" not in content:
        return False
    lines = content.splitlines()
    i = 0
    saw_tool = False
    while i < len(lines):
        ln = lines[i]
        if _TOOL_LINE_RE.match(ln):
            saw_tool = True
            i += 1
            while i < len(lines) and not _TOOL_LINE_RE.match(lines[i]):
                if lines[i].strip().startswith("[Tool Result]"):
                    return False
                i += 1
        elif not ln.strip():
            i += 1
        else:
            return False
    return saw_tool


def _is_tool_result(role: str, content: str) -> bool:
    return role == "tool" or content.startswith("[Tool Result]")


def _compress_tool_result(content: str) -> tuple[str, bool]:
    if len(content) <= _TOOL_RESULT_WHOLE:
        return content, False
    head = content[:_TOOL_HEAD]
    tail = content[-_TOOL_TAIL:]
    err_lines: list[str] = []
    for line in content.splitlines():
        if _ERROR_LINE_RE.match(line) and len(err_lines) < 5:
            err_lines.append(line[:_MAX_ERROR_LINE_KEPT])
    middle = "\n".join(err_lines)
    elided = len(content) - _TOOL_HEAD - _TOOL_TAIL
    if elided < 0:
        elided = 0
    parts = [head]
    if middle:
        parts.append(middle)
    parts.append(f"[+{elided} chars elided]")
    parts.append(tail)
    return "\n".join(parts), True


def _format_assistant_tool_lines(content: str) -> tuple[str, bool]:
    lines_out: list[str] = []
    cut = False
    for ln in content.splitlines():
        if not ln.strip():
            continue
        if len(ln) > _TOOL_LINE_CAP:
            cut = True
        lines_out.append(ln[:_TOOL_LINE_CAP])
    return "\n".join(lines_out), cut


def _tool_name_from_line(line: str) -> str:
    m = re.match(r"\[Tool:\s*([^\]]+)\]", line, re.IGNORECASE)
    if not m:
        return "unknown"
    name = m.group(1).strip().split()[0]
    return name or "unknown"


def _collapse_tool_run(
    run: list[SummaryMessage],
) -> tuple[str, frozenset[int], frozenset[int]]:
    counts: Counter[str] = Counter()
    for msg in run:
        if msg.role == "assistant":
            for ln in msg.content.splitlines():
                if _TOOL_LINE_RE.match(ln):
                    counts[_tool_name_from_line(ln)] += 1
        else:
            counts["result"] += 1
    parts = [f"{name} x{c}" for name, c in sorted(counts.items())]
    summary = f"[{len(run)} tool calls: {', '.join(parts)}]"
    run_seqs = frozenset(m.seq for m in run)
    first = run[0].seq
    line = f"[seq={first} assistant] {summary}"
    return line, run_seqs, run_seqs


@dataclass
class _Formatted:
    seq: int
    role: str
    line: str
    elided: bool
    native: bool = False
    seqs: frozenset[int] = field(default_factory=frozenset)


def _format_message(msg: SummaryMessage) -> _Formatted | None:
    if not isinstance(msg.seq, int) or not isinstance(msg.content, str):
        return None
    if msg.kind == "injected_context":
        return None
    role = msg.role if isinstance(msg.role, str) else "assistant"
    elided = False
    body = msg.content

    if msg.kind == "compaction_summary":
        if len(body) > _NATIVE_CAP:
            body, elided = _head_tail_elide(body, _NATIVE_CAP, _NATIVE_CAP - 500, 500)
        body = f"[NATIVE-SUMMARY seq={msg.seq}]\n{body}"
        line = f"[seq={msg.seq} {role}] {body}"
        return _Formatted(msg.seq, role, line, elided, native=True, seqs=frozenset([msg.seq]))

    if role == "user":
        body, elided = _head_tail_elide(body, _USER_CAP, _USER_HEAD, _USER_TAIL)
    elif _is_tool_result(role, body):
        body, elided = _compress_tool_result(body)
    elif role == "assistant" and _is_tool_only_assistant(body):
        body, elided = _format_assistant_tool_lines(body)
    elif role == "assistant":
        body, elided = _head_tail_elide(body, _ASST_CAP, _ASST_HEAD, _ASST_TAIL)
    else:
        if len(body) > _ASST_CAP:
            body, elided = _head_tail_elide(body, _ASST_CAP, _ASST_HEAD, _ASST_TAIL)

    line = f"[seq={msg.seq} {role}] {body}"
    return _Formatted(msg.seq, role, line, elided, seqs=frozenset([msg.seq]))


def _is_run_member(msg: SummaryMessage) -> bool:
    if not isinstance(msg.content, str):
        return False
    if _is_tool_result(msg.role, msg.content):
        return True
    return msg.role == "assistant" and _is_tool_only_assistant(msg.content)


def compress(
    messages: Sequence[SummaryMessage], version: int = COMPRESSOR_VERSION
) -> CompressedTranscript:
    valid: list[SummaryMessage] = []
    seen_seqs: set[int] = set()
    for m in messages:
        if not isinstance(m, SummaryMessage):
            continue
        if isinstance(m.seq, bool) or not isinstance(m.seq, int) or m.seq in seen_seqs:
            continue  # a citation must name exactly one message: first occurrence wins
        seen_seqs.add(m.seq)
        valid.append(m)
    user_prompt_count = sum(1 for m in valid if m.role == "user" and m.kind == "text")

    formatted: list[_Formatted] = []
    elided: set[int] = set()
    cited_seqs: set[int] = set()
    native_seqs: list[int] = []
    i = 0
    while i < len(valid):
        msg = valid[i]
        if not _is_run_member(msg):
            fm = _format_message(msg)
            if fm:
                formatted.append(fm)
                cited_seqs.update(fm.seqs)
                if fm.elided:
                    elided.add(fm.seq)
                if fm.native:
                    native_seqs.append(fm.seq)
            i += 1
            continue
        run_start = i
        while i < len(valid) and _is_run_member(valid[i]):
            i += 1
        run = valid[run_start:i]
        if len(run) > _TOOL_RUN_COLLAPSE:
            line, run_elided, run_seqs = _collapse_tool_run(run)
            formatted.append(_Formatted(run[0].seq, "assistant", line, True, seqs=run_seqs))
            elided.update(run_elided)
            cited_seqs.update(run_seqs)
        else:
            for rm in run:
                fm = _format_message(rm)
                if fm:
                    formatted.append(fm)
                    cited_seqs.update(fm.seqs)
                    if fm.elided:
                        elided.add(fm.seq)
        continue

    seqs: set[int] = cited_seqs

    blocks: list[CompressedBlock] = []
    current_lines: list[str] = []
    current_first = 0
    current_last = 0
    current_seqs: set[int] = set()
    is_user_turn = False
    saw_user = False

    def flush_block() -> None:
        nonlocal current_lines, current_first, current_last, current_seqs, is_user_turn
        if not current_lines:
            return
        blocks.append(
            CompressedBlock(
                first_seq=current_first,
                last_seq=current_last,
                text="\n".join(current_lines),
                is_user_turn=is_user_turn,
                seqs=frozenset(current_seqs),
            )
        )
        current_lines = []
        current_seqs = set()

    for fm in formatted:
        if fm.role == "user":
            flush_block()
            saw_user = True
            is_user_turn = True
            current_first = fm.seq
            current_last = fm.seq
            current_seqs = set(fm.seqs)
            current_lines = [fm.line]
        else:
            if not current_lines:
                current_first = fm.seq
                current_seqs = set(fm.seqs)
                is_user_turn = False if not saw_user else is_user_turn
            else:
                current_seqs.update(fm.seqs)
            current_last = fm.seq
            current_lines.append(fm.line)
    flush_block()

    full_text = "\n".join(b.text for b in blocks)
    return CompressedTranscript(
        version=version,
        blocks=tuple(blocks),
        seqs=frozenset(seqs),
        elided_seqs=frozenset(elided),
        native_summary_seqs=tuple(native_seqs),
        user_prompt_count=user_prompt_count,
        char_count=len(full_text),
    )


def split_chunks(
    transcript: CompressedTranscript, max_chars: int
) -> list[tuple[CompressedBlock, ...]]:
    if max_chars <= 0:
        return [tuple(transcript.blocks)] if transcript.blocks else []
    chunks: list[tuple[CompressedBlock, ...]] = []
    current: list[CompressedBlock] = []
    current_len = 0
    for block in transcript.blocks:
        block_len = len(block.text)
        sep = 1 if current else 0
        if current and current_len + sep + block_len > max_chars:
            chunks.append(tuple(current))
            current = []
            current_len = 0
            sep = 0
        if block_len > max_chars and not current:
            chunks.append((block,))
            continue
        if block_len > max_chars:
            chunks.append(tuple(current))
            current = []
            current_len = 0
            chunks.append((block,))
            continue
        current.append(block)
        current_len += sep + block_len
    if current:
        chunks.append(tuple(current))
    return chunks
