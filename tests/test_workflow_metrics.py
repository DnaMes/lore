"""Evidence-backed workflow metrics (#188).

The fixture archive is hand-built so every asserted number is derived by hand:

- session ``a``: interactive, 1 prompt, 3 messages, 1 failed + 1 ok tool
  event, 10 minutes long, final assistant message says "done".
- session ``b``: exec, 1 prompt, 2 messages, no tool events, 2 hours long,
  final assistant message reports an error.
- session ``c``: origin ``unknown``, no messages at all (the missing-source
  case), inverted timestamps.
- session ``d``: subagent, 3 prompts, 4 messages, 2 failed tool events of 3,
  contradictory timestamps (``last_updated`` before ``created_at``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.services.workflow_metrics import (
    COMPUTED,
    INFERRED,
    OBSERVED,
    MetricResult,
    compute_workflow_metrics,
    format_metrics,
    inferred_outcomes,
    median_session_duration_seconds,
    messages_per_session,
    metrics_as_payload,
    prompts_per_session,
    sessions_by_origin,
    tool_call_failure_rate,
)

T0 = datetime(2026, 3, 2, 9, 0, 0)


def _message(role, content, *, events=None, kind="text"):
    return UnifiedMessage(
        role=role,
        content=content,
        timestamp=T0,
        kind=kind,
        tool_events=list(events or []),
    )


def _event(status, is_error=False):
    return {"name": "bash", "status": status, "duration_ms": 12, "is_error": is_error}


def _session(session_id, tool, created, updated, messages, *, origin, project=None):
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=created,
        last_updated=updated,
        messages=messages,
        origin=origin,
        project_path=project,
    )


def fixture_archive():
    a = _session(
        "a",
        Tool.CLAUDE_CODE,
        T0,
        T0 + timedelta(minutes=10),
        [
            _message(Role.USER, "Add a retry to the uploader"),
            _message(Role.ASSISTANT, "Editing", events=[_event("error", True), _event("success")]),
            _message(Role.ASSISTANT, "All done, tests pass."),
        ],
        origin="interactive",
        project="/repo/one",
    )
    b = _session(
        "b",
        Tool.CODEX,
        T0,
        T0 + timedelta(hours=2),
        [
            _message(Role.USER, "Run the nightly job"),
            _message(Role.ASSISTANT, "The job failed with error 42"),
        ],
        origin="exec",
        project="/repo/two",
    )
    # No messages and no usable origin: every metric that needs them must report
    # coverage 0 rather than a confident zero.
    c = _session(
        "c",
        Tool.CURSOR,
        T0 + timedelta(hours=1),
        T0,  # inverted on purpose
        [],
        origin="unknown",
    )
    d = _session(
        "d",
        Tool.CLAUDE_CODE,
        T0,
        T0 - timedelta(minutes=5),  # inverted on purpose
        [
            _message(Role.USER, "Try the migration"),
            _message(Role.USER, "Try it again"),
            _message(
                Role.ASSISTANT,
                "Still blocked on the schema",
                events=[_event("failed", True), _event("error", True), _event("completed")],
            ),
            _message(Role.SYSTEM, "context", kind="injected_context"),
        ],
        origin="subagent",
        project="/repo/one",
    )
    return [a, b, c, d]


# ---------------------------------------------------------------------------
# observed
# ---------------------------------------------------------------------------


def test_sessions_by_origin_exact_counts():
    result = sessions_by_origin(fixture_archive())

    assert result.kind == OBSERVED
    assert result.value == {"exec": 1, "interactive": 1, "subagent": 1}
    assert result.covered_sessions == 3
    assert result.total_sessions == 4
    assert result.coverage == 0.75
    assert result.source_fields == ("UnifiedSession.origin",)
    assert any("origin='unknown'" in caveat for caveat in result.caveats)


def test_sessions_by_origin_without_origin_is_not_zero():
    sessions = [
        _session("x", Tool.CURSOR, T0, T0, [_message(Role.USER, "hi")], origin="unknown"),
    ]

    result = sessions_by_origin(sessions)

    assert result.value is None  # never a silent 0
    assert result.covered_sessions == 0
    assert result.coverage == 0.0
    assert "no data:" in result.caveats[0]


def test_tool_call_failure_rate_exact():
    result = tool_call_failure_rate(fixture_archive())

    assert result.kind == OBSERVED
    # session a: 1 failed / 2 ; session d: 2 failed / 3 -> 3 of 5
    assert result.value == 0.6
    assert result.covered_sessions == 2  # a and d; c has no messages
    assert result.total_sessions == 4
    assert any("3 of 5" in caveat for caveat in result.caveats)


def test_tool_call_failure_rate_absent_status():
    sessions = [
        _session(
            "x",
            Tool.GEMINI_CLI,
            T0,
            T0,
            [_message(Role.ASSISTANT, "ran", events=[{"name": "bash", "status": "weird"}])],
            origin="interactive",
        )
    ]

    result = tool_call_failure_rate(sessions)

    assert result.value is None
    assert result.covered_sessions == 0
    assert result.coverage == 0.0
    assert any("no data:" in caveat for caveat in result.caveats)


def test_unrecognised_status_is_excluded_not_successful():
    sessions = [
        _session(
            "x",
            Tool.GEMINI_CLI,
            T0,
            T0,
            [
                _message(
                    Role.ASSISTANT,
                    "ran",
                    events=[{"name": "bash", "status": "mystery"}, _event("error", True)],
                )
            ],
            origin="interactive",
        )
    ]

    result = tool_call_failure_rate(sessions)

    assert result.value == 1.0  # 1 failed of 1 classified, the mystery one dropped
    assert any("excluded" in caveat for caveat in result.caveats)


# ---------------------------------------------------------------------------
# computed
# ---------------------------------------------------------------------------


def test_prompts_per_session_exact():
    result = prompts_per_session(fixture_archive())

    assert result.kind == COMPUTED
    # covered sessions a=1, b=1, d=2 prompts -> 4/3
    assert result.value == round(4 / 3, 4)
    assert result.covered_sessions == 3
    assert result.coverage == 0.75
    assert any("no stored messages" in caveat for caveat in result.caveats)


def test_messages_per_session_exact():
    result = messages_per_session(fixture_archive())

    assert result.kind == COMPUTED
    # a=3, b=2, d=4 messages -> 9/3
    assert result.value == 3.0
    assert result.covered_sessions == 3


def test_messages_per_session_without_messages():
    sessions = [_session("x", Tool.CURSOR, T0, T0, [], origin="interactive")]

    result = messages_per_session(sessions)

    assert result.value is None
    assert result.coverage == 0.0
    assert "no data:" in result.caveats[0]


def test_median_session_duration_excludes_inverted_pairs():
    result = median_session_duration_seconds(fixture_archive())

    assert result.kind == COMPUTED
    # usable: a = 600s, b = 7200s -> median (600 + 7200) / 2
    assert result.value == 3900.0
    assert result.covered_sessions == 2
    assert any("inverted" in caveat for caveat in result.caveats)


# ---------------------------------------------------------------------------
# inferred
# ---------------------------------------------------------------------------


def test_inferred_outcomes_labelled_and_counted():
    result = inferred_outcomes(fixture_archive())

    assert result.kind == INFERRED
    # a: "All done, tests pass." -> succeeded; b: "error" -> failed;
    # d: "blocked" -> failed; c has no assistant message at all.
    assert result.value == {"succeeded": 1, "failed": 2, "unclassified": 0}
    assert result.covered_sessions == 3
    assert any(caveat.startswith("INFERRED:") for caveat in result.caveats)


def test_inferred_metric_is_not_computed_by_default():
    names_default = {result.name for result in compute_workflow_metrics(fixture_archive())}
    names_explicit = {
        result.name for result in compute_workflow_metrics(fixture_archive(), include_inferred=True)
    }

    assert "inferred_outcomes" not in names_default
    assert "inferred_outcomes" in names_explicit
    assert names_default == names_explicit - {"inferred_outcomes"}


def test_inferred_result_cannot_be_summed_with_observed():
    observed = MetricResult(
        name="sessions_seen",
        value=10,
        kind=OBSERVED,
        source_fields=("UnifiedSession.session_id",),
        covered_sessions=10,
        total_sessions=10,
    )
    inferred = MetricResult(
        name="inferred_outcomes",
        value=3,
        kind=INFERRED,
        source_fields=("UnifiedMessage.content",),
        covered_sessions=10,
        total_sessions=10,
    )

    with pytest.raises(ValueError, match="never summed"):
        observed + inferred
    with pytest.raises(ValueError, match="never summed"):
        inferred + observed


def test_same_kind_results_combine():
    left = MetricResult("a", 2, OBSERVED, ("UnifiedSession.origin",), 4, 5, ("c1",))
    right = MetricResult("b", 3, OBSERVED, ("UnifiedSession.origin",), 5, 5)

    combined = left + right

    assert (combined.value, combined.kind) == (5, OBSERVED)
    assert combined.covered_sessions == 4  # the conservative end
    assert combined.caveats == ("c1",)


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------


def test_payload_keeps_kind_and_coverage_per_metric():
    results = compute_workflow_metrics(fixture_archive(), include_inferred=True)

    payload = metrics_as_payload(results)

    assert payload["total_sessions"] == 4
    assert payload["metric_kinds"] == [OBSERVED, COMPUTED, INFERRED]
    by_name = {metric["name"]: metric for metric in payload["metrics"]}
    assert {
        "value",
        "kind",
        "source_fields",
        "covered_sessions",
        "total_sessions",
        "coverage",
        "caveats",
    } <= set(by_name["tool_call_failure_rate"])
    assert by_name["tool_call_failure_rate"]["kind"] == OBSERVED
    assert by_name["inferred_outcomes"]["kind"] == INFERRED
    assert by_name["prompts_per_session"]["coverage"] == 0.75


def test_text_report_groups_by_kind():
    report = format_metrics(compute_workflow_metrics(fixture_archive(), include_inferred=True))

    assert "[observed]" in report
    assert "[computed]" in report
    assert "[inferred]" in report
    assert report.index("[observed]") < report.index("[inferred]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _FakeExtractor:
    def __init__(self, sessions):
        self._sessions = sessions

    def is_available(self):
        return True

    def extract_sessions(self):
        return list(self._sessions)


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_FakeExtractor(fixture_archive())])
    monkeypatch.setattr("sys.argv", ["lore", *argv])
    return lore_cli.main()


def test_cli_metrics_json(monkeypatch, capsys):
    _run_cli(
        monkeypatch,
        ["--output-dir", "/nonexistent-lore", "metrics", "--json", "--include-automated"],
    )

    payload = json.loads(capsys.readouterr().out)
    by_name = {metric["name"]: metric for metric in payload["metrics"]}
    assert payload["total_sessions"] == 4
    assert by_name["sessions_by_origin"]["value"] == {"exec": 1, "interactive": 1, "subagent": 1}
    assert "inferred_outcomes" not in by_name


def test_cli_metrics_text_and_inferred_flag(monkeypatch, capsys):
    _run_cli(
        monkeypatch,
        [
            "--output-dir",
            "/nonexistent-lore",
            "metrics",
            "--include-inferred",
            "--include-automated",
        ],
    )

    out = capsys.readouterr().out
    assert "Workflow metrics" in out
    assert "inferred_outcomes" in out
    assert "coverage 3/4" in out


def test_cli_metrics_writes_nothing(monkeypatch, tmp_path, capsys):
    before = set(tmp_path.iterdir())
    _run_cli(
        monkeypatch, ["--output-dir", str(tmp_path), "metrics", "--json", "--include-automated"]
    )
    capsys.readouterr()

    assert set(tmp_path.iterdir()) == before
