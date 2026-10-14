"""Chunked summarize pipeline (B4.3b, #197)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pytest

from lore.core.models import Role
from lore.services.summarize import (
    estimate_run,
    run_summaries,
    summarize_session,
    validate_summary,
)
from lore.storage.summaries import get_summary, list_summaries
from lore.storage.writer import v2_db_path, write_sessions
from tests.test_summarize_pipeline import (
    FAKE_KEY,
    MODEL,
    FakeProvider,
    _build_store,
    _msg,
    _session,
    _valid_summary,
)


def _auto_json(prompt: str) -> str:
    seqs = [int(x) for x in re.findall(r"\[seq=(\d+)", prompt)]
    cite = seqs[0] if seqs else 1
    if "<chunk_summaries>" in prompt:
        cite = seqs[0] if seqs else 1
    return json.dumps(_valid_summary(cite=cite))


class AutoProvider(FakeProvider):
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ):
        from lore.llm.base import LLMResponse

        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        content = self.responses.pop(0) if self.responses else _auto_json(prompt)
        return LLMResponse(content=content, model="test", tokens_used=120)


def _long_session(sid: str, n_users: int = 40, pad: int = 60) -> Any:
    parts: list[str] = []
    for i in range(n_users):
        parts.append(f"User turn {i} " + ("detail " * pad))
        parts.append(f"Assistant ack {i} " + ("ok " * 25))
    return _session(sid, *parts)


@pytest.fixture
def chunk_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lore.services.summarize.SINGLE_PASS_MAX_TOKENS", 400)
    monkeypatch.setattr("lore.services.summarize_chunks.CHUNK_TARGET_TOKENS", 120)


def test_chunked_writes_chunk_rows_and_session_row(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("long"))
    provider = AutoProvider()
    assert summarize_session(provider, out, "long", model_label=MODEL) == "written"
    chunks = list_summaries(out, level="chunk", session_ids=["long"])
    assert len(chunks) >= 2
    session = get_summary(out, "session", "session:long")
    assert session is not None
    assert session["payload"]["chunks"] == len(chunks)
    assert len(provider.prompts) == len(chunks) + 1


def test_second_run_zero_provider_calls(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("long"))
    summarize_session(AutoProvider(), out, "long", model_label=MODEL)
    provider = AutoProvider()
    assert summarize_session(provider, out, "long", model_label=MODEL) == "unchanged"
    assert provider.prompts == []


def test_growth_reruns_last_chunk_and_merge(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    sess = _long_session("grow")
    _build_store(out, sess)
    first_provider = AutoProvider()
    summarize_session(first_provider, out, "grow", model_label=MODEL)
    first_calls = len(first_provider.prompts)
    chunks_before = {r["scope_key"]: r["fingerprint"] for r in list_summaries(out, level="chunk")}
    sess.messages.extend(
        [
            _msg(Role.USER, "extra user " + ("z " * 80)),
            _msg(Role.ASSISTANT, "extra assistant " + ("a " * 40)),
        ]
        * 3
    )
    write_sessions(v2_db_path(out), [sess])
    provider = AutoProvider()
    summarize_session(provider, out, "grow", model_label=MODEL)
    assert len(provider.prompts) < first_calls
    assert any("<chunk_summaries>" in p for p in provider.prompts)
    chunks_after = list_summaries(out, level="chunk", session_ids=["grow"])
    after_fps = {r["scope_key"]: r["fingerprint"] for r in chunks_after}
    reused = sum(1 for k, v in chunks_before.items() if after_fps.get(k) == v)
    assert reused >= len(chunks_before) - 1


def test_more_than_eight_chunks_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lore.services.summarize.SINGLE_PASS_MAX_TOKENS", 50)
    monkeypatch.setattr("lore.services.summarize_chunks.CHUNK_TARGET_TOKENS", 30)
    out = tmp_path / "out"
    _build_store(out, _long_session("many", n_users=30, pad=80))
    summarize_session(AutoProvider(), out, "many", model_label=MODEL)
    row = get_summary(out, "session", "session:many")
    assert row is not None
    assert row["payload"]["truncated"] is True
    assert len(list_summaries(out, level="chunk", session_ids=["many"])) == 8


def test_merged_cite_not_in_chunks_rejected(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("badmerge"))
    good = json.dumps(_valid_summary(cite=1))
    bad_merge = json.dumps(_valid_summary(cite=99999))
    provider = AutoProvider([good, good, bad_merge, bad_merge])
    assert summarize_session(provider, out, "badmerge", model_label=MODEL) == "invalid"
    assert get_summary(out, "session", "session:badmerge") is None


def test_native_compaction_post_span_and_prior(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    native = "Native compaction summary " + ("context " * 30)
    parts = [native, "Post user " + ("p " * 80), "Post assistant " + ("a " * 40)]
    for i in range(8):
        parts.extend([f"User {i} " + ("u " * 80), f"Asst {i} " + ("x " * 30)])
    sess = _session("native", *parts)
    sess.messages[0] = _msg(Role.ASSISTANT, native, kind="compaction_summary")
    _build_store(out, sess)
    provider = AutoProvider()
    summarize_session(provider, out, "native", model_label=MODEL)
    assert any("PRIOR SUMMARY" in p for p in provider.prompts)
    transcript_prompt = next(p for p in provider.prompts if "<transcript>" in p)
    assert "[seq=1 user]" not in transcript_prompt


def test_verbatim_native_copy_rejected():
    native = "x" * 80
    raw = _valid_summary()
    raw["one_liner"] = native[:60]
    data, errs = validate_summary(raw, allowed_seqs=frozenset({1}), forbidden_verbatim=native)
    assert data is None
    assert errs


def test_rate_limit_backoff(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("rate"))
    sleeps: list[float] = []

    class Flaky(AutoProvider):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        def generate(self, *args: Any, **kwargs: Any):
            from lore.llm.base import LLMResponse

            self.n += 1
            if self.n <= 2:
                raise RuntimeError("HTTP 429 rate limited")
            return LLMResponse(content=_auto_json(args[0]), model="test", tokens_used=1)

    summarize_session(Flaky(), out, "rate", model_label=MODEL, sleep=sleeps.append)
    assert sleeps[:2] == [5, 20]


def test_schema_failure_keeps_chunks_reused(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("fail", n_users=12, pad=70))
    bad = json.dumps({**_valid_summary(), "one_liner": "x" * 200})

    class FailMerge(AutoProvider):
        def generate(self, prompt: str, system_prompt: Optional[str] = None, **kwargs: Any):
            from lore.llm.base import LLMResponse

            self.prompts.append(prompt)
            self.system_prompts.append(system_prompt)
            if "<chunk_summaries>" in prompt:
                return LLMResponse(content=bad, model="test", tokens_used=120)
            return super().generate(prompt, system_prompt=system_prompt, **kwargs)

    provider = FailMerge()
    assert summarize_session(provider, out, "fail", model_label=MODEL) == "invalid"
    chunks = list_summaries(out, level="chunk", session_ids=["fail"])
    assert chunks
    assert get_summary(out, "session", "session:fail") is None
    provider2 = AutoProvider()
    summarize_session(provider2, out, "fail", model_label=MODEL)
    assert len(provider2.prompts) < len(provider.prompts)


def test_max_input_tokens_stops_between_calls(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("cap"))
    report = run_summaries(out, ["cap"], AutoProvider(), MODEL, max_input_tokens=800)
    assert report.written == 0
    assert len(list_summaries(out, level="chunk", session_ids=["cap"])) >= 1


def test_max_session_tokens_over_budget(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("ob"))
    report = run_summaries(out, ["ob"], AutoProvider(), MODEL, max_session_tokens=1)
    assert report.over_budget == 1
    assert get_summary(out, "session", "session:ob") is None
    assert not list_summaries(out, level="chunk", session_ids=["ob"])


def test_dry_run_matches_calls(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("est"))
    est = estimate_run(out, ["est"], model_label=MODEL)
    assert est[0].status == "chunked"
    provider = AutoProvider()
    summarize_session(provider, out, "est", model_label=MODEL)
    assert est[0].expected_calls == len(provider.prompts)
    est2 = estimate_run(out, ["est"], model_label=MODEL)
    assert est2[0].expected_calls == 0
    assert est2[0].status == "unchanged"


def test_outbound_prompts_redacted(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    sess = _long_session("sec")
    sess.messages[1] = _msg(Role.ASSISTANT, f"[Tool Result]\nkey={FAKE_KEY}\n")
    _build_store(out, sess)
    provider = AutoProvider()
    summarize_session(provider, out, "sec", model_label=MODEL)
    assert any("[REDACTED:" in p for p in provider.prompts)
    assert all(FAKE_KEY not in p for p in provider.prompts)


def test_tombstoned_never_sent(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("gone"))
    (out / "deleted_sessions.json").write_text(json.dumps({"session_ids": ["gone"]}))
    provider = AutoProvider()
    assert summarize_session(provider, out, "gone", model_label=MODEL) == "skipped_deleted"
    assert provider.prompts == []


def test_chunk_prompts_cite_within_seqs(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("cite"))
    provider = AutoProvider()
    summarize_session(provider, out, "cite", model_label=MODEL)
    for p in provider.prompts:
        if "<transcript>" not in p:
            continue
        seqs = {int(x) for x in re.findall(r"\[seq=(\d+)", p)}
        body = _auto_json(p)
        data, errs = validate_summary(json.loads(body), allowed_seqs=frozenset(seqs))
        assert data is not None
        assert not errs


def test_smaller_chunk_retry_keeps_both_halves(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lore/services/summarize.SINGLE_PASS_MAX_TOKENS".replace("/", "."), 400)
    monkeypatch.setattr("lore.services.summarize_chunks.CHUNK_TARGET_TOKENS", 400)
    out = tmp_path / "out"
    _build_store(out, _long_session("halves", n_users=12))
    bad = json.dumps({**_valid_summary(), "one_liner": "x" * 200})
    provider = AutoProvider([bad, bad])  # first chunk: answer and repair are invalid

    outcome = summarize_session(provider, out, "halves", model_label=MODEL, sleep=lambda _s: None)

    assert outcome == "written"
    ranges = sorted(
        tuple(int(x) for x in r["scope_key"].rsplit(":", 1)[1].split("-"))
        for r in list_summaries(out, level="chunk", session_ids=["halves"])
    )
    for (_a1, last1), (first2, _l2) in zip(ranges, ranges[1:]):
        assert first2 > last1  # disjoint
        assert first2 - last1 <= 2  # and adjacent: no span was skipped by the retry


def test_merge_fingerprint_is_order_and_artifact_sensitive():
    from lore.services.summarize_chunks import merge_fingerprint

    assert merge_fingerprint(["a", "b"], "m", 1) != merge_fingerprint(["b", "a"], "m", 1)
    assert merge_fingerprint(["a"], "m", 1, "x") != merge_fingerprint(["a"], "m", 1, "y")


def test_halved_chunk_is_reused_on_the_second_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lore.services.summarize.SINGLE_PASS_MAX_TOKENS", 400)
    monkeypatch.setattr("lore.services.summarize_chunks.CHUNK_TARGET_TOKENS", 400)
    out = tmp_path / "out"
    _build_store(out, _long_session("reuse", n_users=12))
    bad = json.dumps({**_valid_summary(), "one_liner": "x" * 200})
    first = AutoProvider([bad, bad])
    assert summarize_session(first, out, "reuse", model_label=MODEL, sleep=lambda _s: None) == (
        "written"
    )

    second = AutoProvider()
    outcome = summarize_session(second, out, "reuse", model_label=MODEL, sleep=lambda _s: None)

    assert outcome == "unchanged"
    assert second.prompts == []


def test_repair_input_tokens_are_reported_for_chunks(tmp_path: Path, chunk_limits: None):
    out = tmp_path / "out"
    _build_store(out, _long_session("repair", n_users=8))
    bad = json.dumps({**_valid_summary(), "one_liner": "x" * 200})
    provider = AutoProvider([bad])  # first chunk answer invalid, its repair succeeds
    report = run_summaries(out, ["repair"], provider, MODEL, sleep=lambda _s: None)
    plain = run_summaries(out, ["repair"], AutoProvider(), MODEL, force=True, sleep=lambda _s: None)
    assert report.written == 1
    assert report.input_tokens > plain.input_tokens  # the repair call is part of the usage


def test_unhashable_outcome_status_is_a_validation_error_not_a_crash():
    bad = _valid_summary()
    bad["outcome"] = {**bad["outcome"], "status": ["done"]}
    cleaned, errors = validate_summary(bad, allowed_seqs=frozenset({1, 2, 3}))
    assert cleaned is None and errors


def test_one_crashing_session_does_not_abort_the_batch(tmp_path: Path, monkeypatch):
    import lore.services.summarize as sm

    out = tmp_path / "out"
    _build_store(out, _long_session("a", n_users=4), _long_session("b", n_users=4))
    real = sm._process_session
    calls: list[str] = []

    def flaky(provider, output_dir, session_id, **kw):
        calls.append(session_id)
        if session_id == "a":
            raise RuntimeError("boom")
        return real(provider, output_dir, session_id, **kw)

    monkeypatch.setattr(sm, "_process_session", flaky)
    monkeypatch.setattr("lore.services.summarize.SINGLE_PASS_MAX_TOKENS", 400)
    monkeypatch.setattr("lore.services.summarize_chunks.CHUNK_TARGET_TOKENS", 400)
    report = run_summaries(out, ["a", "b"], AutoProvider(), MODEL)

    assert calls == ["a", "b"]
    assert report.failed == 1 and report.written == 1


def test_transient_error_detection_is_not_a_substring_match():
    from lore.services.summarize import _is_transient_provider_error as transient

    assert transient(RuntimeError("HTTP 429 Too Many Requests"))
    assert transient(RuntimeError("rate limit exceeded"))
    assert transient(RuntimeError("request timed out"))
    assert not transient(RuntimeError("could not generate a response"))
    assert not transient(RuntimeError("accurate migration failed"))
